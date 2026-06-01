"""
Scraper del ship tracker del Puerto de Santos (Porto de Santos).

Fuente: https://www.portodesantos.com.br/en/ship-tracker/

Tres páginas:
  expected-arrivals : barcos en camino (próximas 1-3 semanas)
  scheduled-arrivals: confirmados para atracar (próximos días)
  berthed-ships     : actualmente atracados y cargando/descargando

Filtro: solo filas con Mercadoria/Cargo que contenga ACUCAR o SUGAR.
Navegación Long = exportación internacional (Cabo = cabotaje doméstico).

Columnas por página:
  expected (14 cols):
    0=Ship  1=Flag  2=Com/Cal  3=Nav  4=Arrival  5=Notice  6=Agency
    7=Operat  8=Goods  9=Weight  10=Voyage  11=DUV  12=P  13=Terminal

  berthed (11 cols):
    0=Terminal  1=Ship  2-5=Turnos  6=Cargo  7=Desc(unload)  8=Emb(load)  9=Type  10=Qty

  scheduled (8+ cols):
    0=Date  1=Hour  2=Local  3=Ship  4=Cargo  5=Evento  6=Voyage  7=DUV  8+=slots
"""
import logging
import re
import ssl
import time
from datetime import date, datetime
from typing import Optional

import requests
import urllib3
from bs4 import BeautifulSoup
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from models import SantosPortSnapshot
from services.data_quality import check_or_log, parse_log_warning, validate_range

logger = logging.getLogger(__name__)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PAGES = {
    "expected":  "https://www.portodesantos.com.br/en/ship-tracker/expected-arrivals/",
    "scheduled": "https://www.portodesantos.com.br/en/ship-tracker/scheduled-arrivals/",
    "berthed":   "https://www.portodesantos.com.br/en/ship-tracker/berthed-ships/",
}

SUGAR_RE = re.compile(r"AC[UÚ]CAR|SUGAR", re.IGNORECASE)
HEADERS  = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_html(url: str, retries: int = 3) -> Optional[str]:
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30, verify=False)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception as e:
            logger.warning("GET %s intento %d: %s", url, attempt + 1, e)
            time.sleep(2 ** attempt)
    return None


def _cells(tr) -> list[str]:
    """Extrae texto limpio de todos los <td> de un <tr>."""
    return [td.get_text(separator=" ", strip=True) for td in tr.find_all("td")]


def _validate_santos_tonnage(load_qty_t, weight_t, ship_name: str) -> bool:
    """
    Valida load_qty_t y weight_t contra rango §3.6 [0, 200_000] toneladas.

    Permite None (legítimo: muchos barcos no traen ambos campos). Solo
    rechaza valores explícitos fuera de rango. Returns False + WARNING si
    alguno se sale; True si ambos son válidos o None.
    """
    _, load_ok = check_or_log(
        lambda: validate_range(
            load_qty_t, min_value=0, max_value=200_000,
            source="santos_port", field=f"load_qty_t[{ship_name}]",
            allow_none=True,
        ),
        on_error="warn",
    )
    _, weight_ok = check_or_log(
        lambda: validate_range(
            weight_t, min_value=0, max_value=200_000,
            source="santos_port", field=f"weight_t[{ship_name}]",
            allow_none=True,
        ),
        on_error="warn",
    )
    return load_ok and weight_ok


def _parse_int(s: str) -> Optional[int]:
    """Parsea tonelajes como '12,345 t' o '55000 ton' a int. Skip-and-log si malformado.

    Devuelve None si el string es vacío o solo contiene caracteres no-numéricos.
    Logs WARNING para cualquier ValueError / IndexError inesperado, evitando que
    cambios estructurales del HTML del ship tracker introduzcan NULLs en masa
    sin alerta.
    """
    try:
        cleaned = re.sub(r"[^\d]", "", str(s).split()[0])
        return int(cleaned) if cleaned else None
    except (ValueError, AttributeError, TypeError, IndexError) as e:
        parse_log_warning("santos_port._parse_int", s, e)
        return None


def _parse_arrival_dt(s: str) -> Optional[datetime]:
    """Parsea 'DD/MM/YYYY HH:MM:SS' o 'DD/MM/YYYY'."""
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Page parsers
# ---------------------------------------------------------------------------

def _parse_expected(html: str) -> list[dict]:
    """
    expected-arrivals: 14 columnas por fila.
    Filtra cargo ACUCAR/SUGAR. Incluye Long y Cabo (etiquetados).
    """
    soup  = BeautifulSoup(html, "html.parser")
    ships = []
    for tr in soup.find_all("tr"):
        row = _cells(tr)
        if len(row) < 14:
            continue
        cargo = row[8]
        if not SUGAR_RE.search(cargo):
            continue
        ships.append({
            "ship":       row[0].strip(),
            "nav_type":   row[3].strip(),    # Long | Cabo
            "arrival_dt": _parse_arrival_dt(row[4]),
            "operat":     row[7].strip(),    # EMB=loading, DESC=unloading
            "cargo":      cargo.strip(),
            "weight_t":   _parse_int(row[9]),
            "terminal":   row[13].strip(),
            "voyage":     row[10].strip(),
            "duv":        row[11].strip(),
        })
    return ships


def _parse_berthed(html: str) -> list[dict]:
    """
    berthed-ships: 11 columnas.
    col8 = Emb/Load (tonelaje cargando) — lo que nos importa para exportación.
    """
    soup  = BeautifulSoup(html, "html.parser")
    ships = []
    for tr in soup.find_all("tr"):
        row = _cells(tr)
        if len(row) < 9:
            continue
        cargo = row[6] if len(row) > 6 else ""
        if not SUGAR_RE.search(cargo):
            continue
        ships.append({
            "ship":       row[1].strip(),
            "terminal":   row[0].strip(),
            "cargo":      cargo.strip(),
            "load_qty_t": _parse_int(row[8]),   # Emb (loading)
            "desc_qty_t": _parse_int(row[7]),   # Desc (unloading)
        })
    return ships


def _parse_scheduled(html: str) -> list[dict]:
    """
    scheduled-arrivals: calendario por turnos.
    col0=Date  col1=Hour  col2=Local  col3=Ship  col4=Cargo  col5=Evento  col6=Voyage  col7=DUV
    """
    soup  = BeautifulSoup(html, "html.parser")
    ships = []
    for tr in soup.find_all("tr"):
        row = _cells(tr)
        if len(row) < 5:
            continue
        cargo = row[4] if len(row) > 4 else ""
        if not SUGAR_RE.search(cargo):
            continue
        ships.append({
            "ship":       row[3].strip() if len(row) > 3 else "",
            "terminal":   row[2].strip() if len(row) > 2 else "",
            "cargo":      cargo.strip(),
            "arrival_dt": _parse_arrival_dt(row[0]) if row[0] else None,
            "evento":     row[5].strip() if len(row) > 5 else "",
            "voyage":     row[6].strip() if len(row) > 6 else "",
            "duv":        row[7].strip() if len(row) > 7 else "",
        })
    return ships


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def _upsert_ships(session: Session, page: str, ships: list[dict], today: date):
    for s in ships:
        # Range Gate §3.6 — descarta barcos con tonelajes corruptos
        if not _validate_santos_tonnage(
            s.get("load_qty_t"), s.get("weight_t"), s.get("ship", "?")
        ):
            continue
        record = {
            "snapshot_date": today,
            "page":          page,
            "ship_name":     s.get("ship", "DESCONOCIDO"),
            "cargo":         s.get("cargo"),
            "terminal":      s.get("terminal"),
            "nav_type":      s.get("nav_type"),
            "arrival_dt":    s.get("arrival_dt"),
            "load_qty_t":    s.get("load_qty_t"),
            "weight_t":      s.get("weight_t"),
            "evento":        s.get("evento"),
            "voyage":        s.get("voyage"),
            "duv":           s.get("duv"),
        }
        stmt = (
            insert(SantosPortSnapshot)
            .values(**record)
            .on_conflict_do_update(
                constraint="uq_santos_date_page_ship_terminal",
                set_={k: v for k, v in record.items()
                      if k not in ("snapshot_date", "page", "ship_name", "terminal")},
            )
        )
        try:
            session.execute(stmt)
        except Exception as e:
            logger.warning("upsert error %s %s: %s", page, s.get("ship"), e)
            session.rollback()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_santos_port(session: Session) -> dict:
    """
    Descarga las tres páginas del ship tracker de Santos y almacena en DB
    los barcos con cargo ACUCAR/SUGAR.

    Devuelve:
      {
        "expected":  [lista de ships],
        "scheduled": [lista de ships],
        "berthed":   [lista de ships],
        "n_expected":  int,   # solo Long
        "n_scheduled": int,
        "n_berthed":   int,
        "tonnage_expected":  int,  # weight_t sumado (Long)
        "tonnage_berthed":   int,  # load_qty_t sumado
        "errors": [str],
      }
    """
    today  = date.today()
    result = {
        "expected": [], "scheduled": [], "berthed": [],
        "n_expected": 0, "n_scheduled": 0, "n_berthed": 0,
        "tonnage_expected": 0, "tonnage_berthed": 0,
        "errors": [],
    }

    parsers = {
        "expected":  _parse_expected,
        "scheduled": _parse_scheduled,
        "berthed":   _parse_berthed,
    }

    for page, parser in parsers.items():
        url  = PAGES[page]
        html = _get_html(url)
        if html is None:
            result["errors"].append("No se pudo descargar %s" % page)
            continue

        ships = parser(html)
        result[page] = ships
        _upsert_ships(session, page, ships, today)
        logger.info("Santos %s: %d barcos ACUCAR", page, len(ships))

    session.commit()

    # Métricas de resumen
    # Expected: solo Long con arrival_dt >= hoy (excluir entradas ya pasadas)
    exp_long = [
        s for s in result["expected"]
        if s.get("nav_type", "").strip() == "Long"
        and (s.get("arrival_dt") is None or s["arrival_dt"].date() >= today)
    ]
    result["n_expected"]       = len(exp_long)
    result["tonnage_expected"] = sum(s["weight_t"] or 0 for s in exp_long)

    result["n_scheduled"]      = len(result["scheduled"])

    # Berthed: todos los ACUCAR (ya filtrado por regex)
    result["n_berthed"]        = len(result["berthed"])
    result["tonnage_berthed"]  = sum(s["load_qty_t"] or 0 for s in result["berthed"])

    return result


def get_latest_snapshot(session: Session) -> Optional[dict]:
    """
    Lee el último snapshot de DB (hoy o más reciente disponible).
    Devuelve el mismo dict de métricas que fetch_santos_port sin hacer HTTP.
    """
    from sqlalchemy import text
    rows = session.execute(text("""
        SELECT page, ship_name, cargo, terminal, nav_type,
               arrival_dt, load_qty_t, weight_t, evento, voyage, snapshot_date
        FROM santos_port_snapshot
        WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM santos_port_snapshot)
        ORDER BY page, ship_name
    """)).fetchall()

    if not rows:
        return None

    snap_date = str(rows[0][10])
    result    = {"expected": [], "scheduled": [], "berthed": [],
                 "n_expected": 0, "n_scheduled": 0, "n_berthed": 0,
                 "tonnage_expected": 0, "tonnage_berthed": 0,
                 "snapshot_date": snap_date, "errors": []}

    for r in rows:
        page, ship, cargo, terminal, nav, arr, load_q, weight, evento, voyage, _ = r
        d = {
            "ship": ship, "cargo": cargo, "terminal": terminal,
            "nav_type": nav, "arrival_dt": arr,
            "load_qty_t": load_q, "weight_t": weight,
            "evento": evento, "voyage": voyage,
        }
        result[page].append(d)

    today    = date.today()
    exp_long = [
        s for s in result["expected"]
        if (s.get("nav_type") or "").strip() == "Long"
        and (s.get("arrival_dt") is None or
             (hasattr(s["arrival_dt"], "date") and s["arrival_dt"].date() >= today))
    ]
    result["n_expected"]       = len(exp_long)
    result["tonnage_expected"] = sum((s["weight_t"] or 0) for s in exp_long)
    result["n_scheduled"]      = len(result["scheduled"])
    result["n_berthed"]        = len(result["berthed"])
    result["tonnage_berthed"]  = sum((s["load_qty_t"] or 0) for s in result["berthed"])

    return result
