"""
UNICADATA — preço ao produtor etanol hidratado SP (semanal, ex-mill).

UNICADATA es la fuente que Green Pool y traders del sector usan para el
precio que los molinos RECIBEN en su puerta. Es ~6% superior a CEPEA
Paulínia (que captura el precio en el terminal de distribución, ya
descontado el flete interior).

URL base: https://unicadata.com.br/preco-ao-produtor.php
Tabla:    idTabela=2487  (SP hidratado semanal)
Frecuencia: semanal (~martes/miércoles de cada semana)

Almacenamiento: cepea_prices con series_name = 'unicadata_sp_hydrous_brl_liter'
  - price_usd almacena el valor raw en BRL/L (unit = 'BRL/L')
  - La conversión a USD/m³ y c/lb ocurre en ethanol_parity.py en tiempo real
    usando BCB PTAX del día.
"""
import logging
import re
import time
from datetime import date, datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from models import CepeaPrice

logger = logging.getLogger(__name__)

SERIES_NAME = "unicadata_sp_hydrous_brl_liter"

# URLs a probar (la API puede cambiar; probar en orden)
_URLS = [
    # Vista principal — tabla renderizada en HTML
    (
        "GET",
        "https://unicadata.com.br/preco-ao-produtor.php"
        "?idMn=42&tipoHistorico=7&acao=visualizar"
        "&idTabela=2487&produto=Etanol+hidratado+combust%C3%ADvel"
        "&frequencia=Semanal&estado=S%C3%A3o+Paulo",
    ),
    # Listagem alternativa
    (
        "GET",
        "https://unicadata.com.br/listagem.php"
        "?idMn=42&acao=visualizar&idTabela=2487",
    ),
]

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Referer": "https://unicadata.com.br/",
    "Accept": "text/html,application/xhtml+xml",
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_html(url: str, method: str = "GET") -> Optional[str]:
    for attempt in range(3):
        try:
            if method == "POST":
                r = requests.post(url, headers=_HEADERS, timeout=30)
            else:
                r = requests.get(url, headers=_HEADERS, timeout=30)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception as e:
            logger.warning("UNICADATA GET %s intento %d: %s", url[:60], attempt + 1, e)
            time.sleep(2 ** attempt)
    return None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_brl_price(s: str) -> Optional[float]:
    """Parsea precio brasileño: '2,4730' → 2.473  |  '2.473' → 2.473"""
    s = s.strip().replace(" ", "")
    # Formato BR: '2,4730' (coma = decimal)
    if re.match(r"^\d+,\d+$", s):
        return float(s.replace(",", "."))
    # Formato internacional: '2.473'
    try:
        return float(s.replace(",", ""))
    except (ValueError, TypeError):
        return None


def _parse_date_br(s: str) -> Optional[date]:
    """Parsea fechas brasileñas: 'DD/MM/YYYY' o 'DD/MM/YY'"""
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _parse_table(html: str) -> list[dict]:
    """
    Extrae filas de la tabla UNICADATA.
    Columnas esperadas: [Semana/Data, SP R$/L, ...]
    Retorna lista de {price_date, price_brl_liter}.
    """
    soup = BeautifulSoup(html, "html.parser")
    records = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 3:
            continue

        # Detectar columna de fecha y SP
        header_cells = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]
        if not header_cells:
            continue

        # Buscar índice de columna SP o primer precio
        date_col = 0
        price_col = None
        for i, h in enumerate(header_cells):
            if "paulo" in h or "sp" == h or (i > 0 and price_col is None):
                price_col = i

        if price_col is None and len(header_cells) >= 2:
            price_col = 1  # segunda columna por defecto

        for tr in rows[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 2:
                continue

            price_date = _parse_date_br(cells[date_col])
            if price_date is None:
                continue

            price_brl = _parse_brl_price(cells[price_col])
            if price_brl is None or price_brl <= 0 or price_brl > 10:
                continue  # sanity: BRL/L debe ser 0.5-5.0

            records.append({"price_date": price_date, "price_brl_liter": price_brl})

    return records


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def _upsert(session: Session, records: list[dict]) -> int:
    n = 0
    for rec in records:
        stmt = (
            insert(CepeaPrice)
            .values(
                price_date  = rec["price_date"],
                series_name = SERIES_NAME,
                price_usd   = rec["price_brl_liter"],   # stored as BRL/L; unit disambiguates
                unit        = "BRL/L",
                source_page = "unicadata",
            )
            .on_conflict_do_update(
                constraint = "uq_cepea_date_series",
                set_       = {"price_usd": rec["price_brl_liter"]},
            )
        )
        try:
            session.execute(stmt)
            n += 1
        except Exception as e:
            logger.warning("UNICADATA upsert %s: %s", rec["price_date"], e)
            session.rollback()
    if n:
        session.commit()
    return n


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_unicadata(session: Session, weeks_back: int = 8) -> dict:
    """
    Descarga precios semanales UNICADATA SP hidratado y los almacena en
    cepea_prices (series_name='unicadata_sp_hydrous_brl_liter', unit='BRL/L').

    Returns:
      {
        "rows_upserted": int,
        "latest_price_brl": float | None,
        "latest_date": str | None,
        "source_url": str | None,
        "errors": [str],
      }
    """
    result = {
        "rows_upserted": 0,
        "latest_price_brl": None,
        "latest_date": None,
        "source_url": None,
        "errors": [],
    }

    html = None
    used_url = None
    for method, url in _URLS:
        html = _get_html(url, method)
        if html and len(html) > 500:
            used_url = url
            break

    if not html:
        msg = "UNICADATA: no se pudo descargar ninguna URL"
        logger.warning(msg)
        result["errors"].append(msg)
        return result

    records = _parse_table(html)
    if not records:
        msg = "UNICADATA: HTML descargado pero sin tabla parseable"
        logger.warning("%s (len=%d)", msg, len(html))
        result["errors"].append(msg)
        return result

    # Ordenar por fecha desc y limitar a weeks_back semanas
    records.sort(key=lambda r: r["price_date"], reverse=True)
    records = records[:weeks_back]

    n = _upsert(session, records)
    result["rows_upserted"] = n
    result["source_url"] = used_url

    if records:
        latest = records[0]
        result["latest_price_brl"] = latest["price_brl_liter"]
        result["latest_date"] = str(latest["price_date"])
        logger.info(
            "UNICADATA SP hidratado: %d filas  último=%s  R$%.4f/L",
            n, latest["price_date"], latest["price_brl_liter"],
        )

    return result


def get_latest_unicadata_price(session: Session) -> Optional[dict]:
    """
    Lee el precio UNICADATA más reciente de la DB (sin hacer fetch HTTP).
    Returns {price_brl_liter, price_date} o None.
    """
    from sqlalchemy import text
    row = session.execute(text(
        "SELECT price_usd, price_date FROM cepea_prices "
        "WHERE series_name = :sn ORDER BY price_date DESC LIMIT 1"
    ), {"sn": SERIES_NAME}).fetchone()
    if row:
        return {"price_brl_liter": float(row[0]), "price_date": str(row[1])}
    return None
