"""
Scraper de precios CEPEA/ESALQ — etanol y azúcar físicos brasileños.

Fuentes:
  https://cepea.org.br/en/indicator/ethanol.aspx  → 4 series etanol
  https://cepea.org.br/en/indicator/sugar.aspx    → 2 series azúcar

Series recogidas:
  hydrous_paulinia_usd_m3   : Hidratado Paulínia (SP), US$/m³, DIARIO  ← parity signal
  hydrous_fuel_usd_liter    : Hidratado combustible SP,  US$/litro, SEMANAL
  anhydrous_usd_liter       : Anhidratado SP,            US$/litro, SEMANAL
  hydrous_other_usd_liter   : Hidratado otros usos SP,   US$/litro, SEMANAL
  crystal_sugar_usd_bag50kg : Azúcar cristal SP,  US$/bolsa 50kg, DIARIO
  crystal_sugar2_usd_bag50kg: Segunda serie azúcar (VHP/refinado)

Frecuencia recomendada: diaria (scrape tras cierre del mercado de SP ~18h BRT).
"""
import logging
import re
import time
from datetime import date, datetime
from typing import Optional

import requests
import urllib3
from bs4 import BeautifulSoup
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from models import CepeaPrice

logger = logging.getLogger(__name__)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

URLS = {
    "ethanol": "https://cepea.org.br/en/indicator/ethanol.aspx",
    "sugar":   "https://cepea.org.br/en/indicator/sugar.aspx",
}

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://cepea.org.br/en/",
}

# Mapeo: (sección de texto en la página → nombre de serie interno, unidad)
ETHANOL_SERIES = [
    ("hydrous_paulinia_usd_m3",    "US$/m3",        "daily"),
    ("hydrous_fuel_usd_liter",     "US$/liter",     "weekly"),
    ("anhydrous_usd_liter",        "Anhydrous US$/liter", "weekly"),
    ("hydrous_other_usd_liter",    "US$/liter",     "weekly"),
]
SUGAR_SERIES = [
    ("crystal_sugar_usd_bag50kg",  "US$",           "daily"),
    ("crystal_sugar2_usd_bag50kg", "US$",           "daily"),
]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _get_html(url: str) -> Optional[str]:
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30, verify=False)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception as e:
            logger.warning("CEPEA GET %s intento %d: %s", url, attempt + 1, e)
            time.sleep(2 ** attempt)
    return None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_pct(s: str) -> Optional[float]:
    try:
        return float(s.strip().replace("%", "").replace(",", "."))
    except Exception:
        return None


def _parse_price(s: str) -> Optional[float]:
    try:
        return float(s.strip().replace(",", ""))
    except Exception:
        return None


def _parse_date(s: str) -> Optional[date]:
    """Parsea MM/DD/YYYY (ethanol daily) o 'DD - MM/DD/YYYY' (weekly)."""
    s = s.strip()
    # Semanal: "18 - 05/22/2026"
    m = re.search(r"(\d{2}/\d{2}/\d{4})$", s)
    if m:
        try:
            return datetime.strptime(m.group(1), "%m/%d/%Y").date()
        except ValueError:
            pass
    # Semanal formato DD/MM/YYYY: "22/05/2026"
    m2 = re.search(r"(\d{2}/\d{2}/\d{4})$", s)
    if m2:
        for fmt in ("%m/%d/%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(m2.group(1), fmt).date()
            except ValueError:
                continue
    return None


def _parse_tables(html: str, page: str, series_defs: list) -> list[dict]:
    """
    Extrae filas de todas las tablas de la página.
    Devuelve lista de dicts con {series_name, price_date, price_usd, unit, pct_daily/weekly/monthly}.
    """
    soup   = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    records = []

    for idx, table in enumerate(tables):
        if idx >= len(series_defs):
            break
        series_name, unit, freq = series_defs[idx]

        for tr in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 2:
                continue

            price_date = _parse_date(cells[0])
            price_usd  = _parse_price(cells[1])
            if price_date is None or price_usd is None:
                continue

            rec = {
                "price_date":  price_date,
                "series_name": series_name,
                "price_usd":   price_usd,
                "unit":        unit,
                "source_page": page,
                "pct_daily":   None,
                "pct_weekly":  None,
                "pct_monthly": None,
            }

            if freq == "daily" and len(cells) >= 4:
                rec["pct_daily"]   = _parse_pct(cells[2])
                rec["pct_monthly"] = _parse_pct(cells[3])
            elif freq == "weekly" and len(cells) >= 3:
                rec["pct_weekly"]  = _parse_pct(cells[2])

            records.append(rec)

    return records


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def _upsert(session: Session, records: list[dict]):
    for rec in records:
        stmt = (
            insert(CepeaPrice)
            .values(**rec)
            .on_conflict_do_update(
                constraint="uq_cepea_date_series",
                set_={k: v for k, v in rec.items()
                      if k not in ("price_date", "series_name")},
            )
        )
        try:
            session.execute(stmt)
        except Exception as e:
            logger.warning("CEPEA upsert %s %s: %s", rec["series_name"], rec["price_date"], e)
            session.rollback()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_cepea(session: Session) -> dict:
    """
    Descarga y almacena precios CEPEA de etanol y azúcar.

    Devuelve:
      {
        "ethanol_rows": int,
        "sugar_rows":   int,
        "latest": {
          "hydrous_paulinia_usd_m3":   float,  # US$/m3
          "hydrous_fuel_usd_liter":    float,
          "anhydrous_usd_liter":       float,
          "crystal_sugar_usd_bag50kg": float,
        },
        "errors": [str],
      }
    """
    result = {"ethanol_rows": 0, "sugar_rows": 0, "latest": {}, "errors": []}

    for page, series_defs in [("ethanol", ETHANOL_SERIES), ("sugar", SUGAR_SERIES)]:
        html = _get_html(URLS[page])
        if not html:
            result["errors"].append("No se pudo descargar CEPEA %s" % page)
            continue

        records = _parse_tables(html, page, series_defs)
        _upsert(session, records)
        session.commit()

        count = len(records)
        result["%s_rows" % page] = count
        logger.info("CEPEA %s: %d filas", page, count)

        # Guardar último valor de cada serie
        for rec in records:
            sn = rec["series_name"]
            if sn not in result["latest"]:
                result["latest"][sn] = rec["price_usd"]

    return result


def get_latest_cepea(session: Session) -> dict:
    """
    Lee el último valor de cada serie CEPEA desde DB.
    Devuelve dict {series_name: price_usd} con el dato más reciente.
    """
    from sqlalchemy import text
    rows = session.execute(text("""
        SELECT DISTINCT ON (series_name)
               series_name, price_usd, price_date, unit, pct_daily, pct_weekly, pct_monthly
        FROM cepea_prices
        ORDER BY series_name, price_date DESC
    """)).fetchall()

    return {
        r[0]: {
            "price_usd":    float(r[1]) if r[1] else None,
            "price_date":   str(r[2]),
            "unit":         r[3],
            "pct_daily":    float(r[4]) if r[4] else None,
            "pct_weekly":   float(r[5]) if r[5] else None,
            "pct_monthly":  float(r[6]) if r[6] else None,
        }
        for r in rows
    }
