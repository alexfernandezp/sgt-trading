"""
Scraper del Line-Up del Puerto de Paranaguá (APPA).

Fuente: https://www.appaweb.appa.pr.gov.br/appaweb/pesquisa.aspx?WCI=relLineUpRetroativo

Secciones parseadas:
  atracados   — barcos actualmente en muelle (con realizado/previsto)
  programados — confirmados para atracar próximamente
  ao_largo    — fondeados esperando berth
  esperados   — en camino (ETA conocida)
  despachados — salidos recientemente (con fechas llegada + desatraque → dwell time)

Filtro: solo barcos con Mercadoria que contenga ACUCAR o SUGAR.

Columnas clave despachados (dwell time directo):
  chegada (arrival_dt) + desatracacao (departure_dt) → dwell_days = departure - arrival
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

logger = logging.getLogger(__name__)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

URL     = "https://www.appaweb.appa.pr.gov.br/appaweb/pesquisa.aspx?WCI=relLineUpRetroativo"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
SUGAR_RE = re.compile(r"AC[UÚ]CAR|A[CÇ]UCAR|SUGAR", re.IGNORECASE)


def _get_html(retries: int = 3) -> Optional[str]:
    for attempt in range(retries):
        try:
            r = requests.get(URL, headers=HEADERS, timeout=30, verify=False)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception as e:
            logger.warning("Paranaguá GET intento %d: %s", attempt + 1, e)
            time.sleep(2 ** attempt)
    return None


def _cells(tr) -> list:
    return [td.get_text(separator=" ", strip=True) for td in tr.find_all("td")]


def _parse_float(s: str) -> Optional[float]:
    """Parsea '72.600,000 Tons.' → 72600.0"""
    try:
        cleaned = re.sub(r"[^\d,]", "", str(s)).replace(",", ".")
        # manejar miles: 72600.000 puede venir como 72.600.000 → quitar puntos menos el último
        parts = cleaned.split(".")
        if len(parts) > 2:
            cleaned = "".join(parts[:-1]) + "." + parts[-1]
        return float(cleaned) if cleaned else None
    except Exception:
        return None


def _parse_dt(s: str) -> Optional[datetime]:
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def _find_section_tables(soup) -> dict:
    """
    Mapea cada tabla de datos a su sección basándose en la primera fila de título.
    """
    section_map = {}
    label_to_key = {
        "ATRACADOS":    "atracados",
        "PROGRAMADOS":  "programados",
        "AO LARGO":     "ao_largo",
        "ESPERADOS":    "esperados",
        "DESPACHADOS":  "despachados",
    }
    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if not rows:
            continue
        title = rows[0].get_text(strip=True).upper()
        for label, key in label_to_key.items():
            if label in title:
                section_map[key] = tbl
                break
    return section_map


# ── Parsers por sección ──────────────────────────────────────────────────────

def _parse_atracados(tbl) -> list:
    """
    Columnas (índice): 4=ship, 5=IMO, 7=DWT, 8=bordo, 9=sentido,
    12=mercadoria, 13=atracação, 14=chegada, 18=previsto, 19=realizado
    """
    ships = []
    rows  = tbl.find_all("tr")[2:]   # skip título + header
    for tr in rows:
        c = _cells(tr)
        if len(c) < 15 or not SUGAR_RE.search(c[12] if len(c) > 12 else ""):
            continue
        ships.append({
            "ship":        c[4], "imo": c[5],
            "dwt":         _parse_float(c[7]),
            "cargo":       c[12],
            "terminal":    c[3],
            "sentido":     c[9],
            "arrival_dt":  _parse_dt(c[14]) if len(c) > 14 else None,
            "tonnage_prev": _parse_float(c[18]) if len(c) > 18 else None,
            "tonnage_real": _parse_float(c[19]) if len(c) > 19 else None,
        })
    return ships


def _parse_programados(tbl) -> list:
    """
    Columnas: 4=ship, 5=IMO, 7=cal.cheg, 9=DWT, 11=sentido, 14=mercadoria, 19=previsto
    """
    ships = []
    rows  = tbl.find_all("tr")[2:]
    for tr in rows:
        c = _cells(tr)
        if len(c) < 15 or not SUGAR_RE.search(c[14] if len(c) > 14 else ""):
            continue
        ships.append({
            "ship":        c[4], "imo": c[5],
            "dwt":         _parse_float(c[9]),
            "cargo":       c[14],
            "terminal":    c[3],
            "sentido":     c[11] if len(c) > 11 else "",
            "eta_dt":      _parse_dt(c[7]) if len(c) > 7 else None,
            "tonnage_prev": _parse_float(c[19]) if len(c) > 19 else None,
        })
    return ships


def _parse_ao_largo(tbl) -> list:
    """
    Columnas: 4=ship, 5=IMO, 7=DWT, 8=sentido, 11=mercadoria, 12=ETA, 16=previsto
    """
    ships = []
    rows  = tbl.find_all("tr")[2:]
    for tr in rows:
        c = _cells(tr)
        if len(c) < 12 or not SUGAR_RE.search(c[11] if len(c) > 11 else ""):
            continue
        ships.append({
            "ship":        c[4], "imo": c[5],
            "dwt":         _parse_float(c[7]),
            "cargo":       c[11],
            "terminal":    c[3],
            "sentido":     c[8] if len(c) > 8 else "",
            "eta_dt":      _parse_dt(c[12]) if len(c) > 12 else None,
            "tonnage_prev": _parse_float(c[16]) if len(c) > 16 else None,
        })
    return ships


def _parse_esperados(tbl) -> list:
    """
    Columnas: 4=ship, 5=IMO, 7=DWT, 8=sentido, 11=mercadoria, 12=ETA, 15=previsto
    """
    ships = []
    rows  = tbl.find_all("tr")[2:]
    for tr in rows:
        c = _cells(tr)
        if len(c) < 12 or not SUGAR_RE.search(c[11] if len(c) > 11 else ""):
            continue
        ships.append({
            "ship":        c[4], "imo": c[5],
            "dwt":         _parse_float(c[7]),
            "cargo":       c[11],
            "terminal":    c[3],
            "sentido":     c[8] if len(c) > 8 else "",
            "eta_dt":      _parse_dt(c[12]) if len(c) > 12 else None,
            "tonnage_prev": _parse_float(c[15]) if len(c) > 15 else None,
        })
    return ships


def _parse_despachados(tbl) -> list:
    """
    Columnas: 4=ship, 5=IMO, 7=DWT, 9=sentido, 12=mercadoria,
    13=chegada (arrival), 14=desatracacao (departure) → dwell_days calculable
    """
    ships = []
    rows  = tbl.find_all("tr")[2:]
    for tr in rows:
        c = _cells(tr)
        if len(c) < 13 or not SUGAR_RE.search(c[12] if len(c) > 12 else ""):
            continue
        arrival  = _parse_dt(c[13]) if len(c) > 13 else None
        departure = _parse_dt(c[14]) if len(c) > 14 else None
        ships.append({
            "ship":         c[4], "imo": c[5],
            "dwt":          _parse_float(c[7]),
            "cargo":        c[12],
            "terminal":     c[3],
            "sentido":      c[9] if len(c) > 9 else "",
            "arrival_dt":   arrival,
            "departure_dt": departure,
            "tonnage_prev":  _parse_float(c[17]) if len(c) > 17 else None,
        })
    return ships


# ── Upsert ───────────────────────────────────────────────────────────────────

def _upsert(session: Session, page: str, ships: list, today: date):
    from models.market_data import ParanaguaPortSnapshot
    for s in ships:
        record = {
            "snapshot_date": today,
            "page":          page,
            "ship_name":     (s.get("ship") or "DESCONOCIDO")[:100],
            "imo":           (s.get("imo") or "")[:20],
            "dwt":           s.get("dwt"),
            "cargo":         (s.get("cargo") or "")[:150],
            "terminal":      (s.get("terminal") or "")[:20],
            "sentido":       (s.get("sentido") or "")[:20],
            "arrival_dt":    s.get("arrival_dt"),
            "departure_dt":  s.get("departure_dt"),
            "eta_dt":        s.get("eta_dt"),
            "tonnage_prev":  s.get("tonnage_prev"),
            "tonnage_real":  s.get("tonnage_real"),
        }
        stmt = (
            insert(ParanaguaPortSnapshot)
            .values(**record)
            .on_conflict_do_update(
                constraint="uq_paranagua_date_page_ship",
                set_={k: v for k, v in record.items()
                      if k not in ("snapshot_date", "page", "ship_name", "terminal")},
            )
        )
        try:
            session.execute(stmt)
        except Exception as e:
            logger.warning("paranagua upsert %s %s: %s", page, s.get("ship"), e)
            session.rollback()


# ── API pública ───────────────────────────────────────────────────────────────

def fetch_paranagua_port(session: Session) -> dict:
    """
    Descarga el line-up completo de Paranaguá y almacena barcos de azúcar.

    Returns dict con métricas resumidas y listas de barcos por sección.
    """
    from models.market_data import ParanaguaPortSnapshot
    from models import Base
    from database import engine
    Base.metadata.create_all(engine, tables=[ParanaguaPortSnapshot.__table__])

    today  = date.today()
    result = {
        "atracados": [], "programados": [], "ao_largo": [],
        "esperados": [], "despachados": [],
        "n_atracados": 0, "n_programados": 0, "n_ao_largo": 0,
        "n_esperados": 0, "n_despachados": 0,
        "tonnage_atracados": 0.0, "tonnage_esperados": 0.0,
        "errors": [],
    }

    html = _get_html()
    if html is None:
        result["errors"].append("No se pudo descargar la página de Paranaguá")
        return result

    soup    = BeautifulSoup(html, "html.parser")
    tables  = _find_section_tables(soup)

    parsers = {
        "atracados":   (_parse_atracados,  tables.get("atracados")),
        "programados": (_parse_programados, tables.get("programados")),
        "ao_largo":    (_parse_ao_largo,    tables.get("ao_largo")),
        "esperados":   (_parse_esperados,   tables.get("esperados")),
        "despachados": (_parse_despachados, tables.get("despachados")),
    }

    for page, (parser, tbl) in parsers.items():
        if tbl is None:
            continue
        ships = parser(tbl)
        result[page] = ships
        _upsert(session, page, ships, today)
        logger.info("Paranaguá %s: %d barcos ACUCAR", page, len(ships))

    session.commit()

    result["n_atracados"]   = len(result["atracados"])
    result["n_programados"] = len(result["programados"])
    result["n_ao_largo"]    = len(result["ao_largo"])
    result["n_esperados"]   = len(result["esperados"])
    result["n_despachados"] = len(result["despachados"])
    result["tonnage_atracados"] = sum(
        s.get("tonnage_prev") or 0 for s in result["atracados"])
    result["tonnage_esperados"] = sum(
        s.get("tonnage_prev") or 0 for s in result["esperados"])

    return result


def get_latest_snapshot(session: Session) -> Optional[dict]:
    """Lee el último snapshot de Paranaguá desde la DB."""
    from sqlalchemy import text
    rows = session.execute(text("""
        SELECT page, ship_name, dwt, cargo, terminal, sentido,
               arrival_dt, departure_dt, eta_dt,
               tonnage_prev, tonnage_real, snapshot_date
        FROM paranagua_port_snapshot
        WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM paranagua_port_snapshot)
        ORDER BY page, ship_name
    """)).fetchall()

    if not rows:
        return None

    snap_date = str(rows[0][11])
    result    = {
        "atracados": [], "programados": [], "ao_largo": [],
        "esperados": [], "despachados": [],
        "n_atracados": 0, "n_programados": 0, "n_ao_largo": 0,
        "n_esperados": 0, "n_despachados": 0,
        "snapshot_date": snap_date,
    }
    for r in rows:
        page = r[0]
        d = {"ship": r[1], "dwt": r[2], "cargo": r[3], "terminal": r[4],
             "sentido": r[5], "arrival_dt": r[6], "departure_dt": r[7],
             "eta_dt": r[8], "tonnage_prev": r[9], "tonnage_real": r[10]}
        if page in result:
            result[page].append(d)

    for page in ("atracados", "programados", "ao_largo", "esperados", "despachados"):
        result[f"n_{page}"] = len(result[page])
    return result


def get_dwell_stats(session: Session, days_back: int = 60) -> Optional[dict]:
    """
    Calcula estadísticas de dwell time de barcos de azúcar DESPACHADOS.
    Solo disponible en Paranaguá donde tenemos llegada + salida directas.

    Returns dict con mean_dwell_days, std, n_ships, latest_date.
    """
    from sqlalchemy import text
    import statistics as _stats

    rows = session.execute(text("""
        SELECT ship_name,
               arrival_dt,
               departure_dt,
               EXTRACT(EPOCH FROM (departure_dt - arrival_dt)) / 86400.0 AS dwell_days
        FROM paranagua_port_snapshot
        WHERE page = 'despachados'
          AND arrival_dt   IS NOT NULL
          AND departure_dt IS NOT NULL
          AND departure_dt > arrival_dt
          AND snapshot_date >= CURRENT_DATE - :days * INTERVAL '1 day'
        ORDER BY departure_dt DESC
    """), {"days": days_back}).fetchall()

    if len(rows) < 3:
        return None

    dwells = [float(r[3]) for r in rows if 0.5 <= float(r[3]) <= 30.0]
    if len(dwells) < 3:
        return None

    return {
        "mean_dwell_days": round(_stats.mean(dwells), 2),
        "std_dwell_days":  round(_stats.stdev(dwells) if len(dwells) > 1 else 0.0, 2),
        "n_ships":         len(dwells),
        "latest_date":     str(rows[0][2].date()) if rows[0][2] else None,
    }
