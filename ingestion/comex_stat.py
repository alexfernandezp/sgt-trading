"""
Exportaciones anuales de azúcar de Brasil — MAPA/CGDA (Ministerio de Agricultura).

Fuente: https://www.gov.br/agricultura/pt-br/assuntos/sustentabilidade/agroenergia/acucar-comercio-exterior-brasileiro

La página publica mensualmente PDFs con:
  001 — Exportações Anuais: series anuales 2015-present + YTD año actual vs anterior
  004 — Por Local de Embarque: desglose mensual por puerto (Santos, Paranaguá…)

La señal clave es la comparativa YTD (e.g. Jan-Abr 2026 vs Jan-Abr 2025):
  YTD < −5% YoY → menos azúcar saliendo de Brasil → alcista  → +1 LONG
  YTD > +5% YoY → más oferta en mercado           → bajista  → -1 SHORT
  Entre ±5%      → neutral → 0

Nota: lag ~12 días (datos disponibles alrededor del día 13 del mes siguiente).
"""
import io
import logging
import re
from datetime import date, datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup
from sqlalchemy.dialects.postgresql import insert

logger = logging.getLogger(__name__)

MAPA_URL = (
    "https://www.gov.br/agricultura/pt-br/assuntos/sustentabilidade/"
    "agroenergia/acucar-comercio-exterior-brasileiro"
)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

# NCM aggregado que usamos para almacenar datos del PDF (series anuales)
NCM_MAPA = "1701"
NCM_DESC  = "Açúcar — exportações brasileiras (MAPA/CGDA)"


# ── Scraping de la página MAPA ────────────────────────────────────────────────

def _find_pdf_url(html: str, pattern_words: tuple = ("ANUAIS",)) -> Optional[str]:
    """
    Busca en el HTML de la página MAPA el link al PDF de exportaciones anuales.
    Devuelve la URL absoluta o None.
    """
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True).upper()
        if not href.lower().endswith(".pdf"):
            continue
        if all(w in text for w in pattern_words) and "EXPORT" in text:
            return href
    return None


def _get_page_html() -> Optional[str]:
    try:
        r = requests.get(MAPA_URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.warning("comex_stat: error descargando página MAPA: %s", e)
        return None


def _download_pdf(url: str) -> Optional[bytes]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return r.content
    except Exception as e:
        logger.warning("comex_stat: error descargando PDF %s: %s", url, e)
        return None


# ── Parsing del PDF de exportaciones anuales ─────────────────────────────────

# Patrón fila anual: "2025 14.109 33.774.040 417,74"
_RE_ANNUAL = re.compile(
    r"^(\d{4})\s+([\d\.]+)\s+([\d\.]+)\s+([\d,]+)",
    re.MULTILINE,
)
# Patrón fila YTD: "2025 - Jan-Abr 3.462 7.263.542 476,56"
# o:               "2026* - Jan-Abr 2.648 7.221.296 366,66"
_RE_YTD = re.compile(
    r"^(\d{4})\s*\*?\s*[-–]\s*(Jan[-\w]+)\s+([\d\.]+)\s+([\d\.]+)",
    re.MULTILINE,
)


def _parse_number(s: str) -> float:
    """'7.263.542' → 7263542.0  |  '417,74' → 417.74"""
    s = s.strip()
    # Formato brasileiro: punto = separador miles, coma = decimal
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(".", "")
    return float(s) if s else 0.0


def _parse_anual_pdf(pdf_bytes: bytes) -> dict:
    """
    Extrae del PDF de exportaciones anuales:
      annual: {year: {"usd_m": float, "tonnes": float, "avg_price": float}}
      ytd:    {year: {"period": str, "usd_m": float, "tonnes": float}}

    Devuelve {"annual": {...}, "ytd": {...}, "latest_period": str | None}
    """
    try:
        import pdfplumber
    except ImportError:
        logger.error("comex_stat: pdfplumber no instalado — pip install pdfplumber")
        return {}

    annual = {}
    ytd    = {}
    latest_period = None

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception as e:
        logger.warning("comex_stat: error leyendo PDF: %s", e)
        return {}

    # Filas anuales (evitar capturar líneas YTD que también empiezan por año)
    for m in _RE_ANNUAL.finditer(full_text):
        year = int(m.group(1))
        if year < 2010 or year > date.today().year + 1:
            continue
        # Si la línea siguiente tiene "Jan-" es la fila YTD, no anual
        rest = full_text[m.start():]
        first_line = rest.split("\n")[0]
        if re.search(r"Jan[-–]", first_line):
            continue
        annual[year] = {
            "usd_m":     _parse_number(m.group(2)),
            "tonnes":    _parse_number(m.group(3)),
            "avg_price": _parse_number(m.group(4)),
        }

    # Filas YTD
    for m in _RE_YTD.finditer(full_text):
        year   = int(m.group(1))
        period = m.group(2).strip()
        ytd[year] = {
            "period": period,
            "usd_m":  _parse_number(m.group(3)),
            "tonnes": _parse_number(m.group(4)),
        }
        latest_period = period

    return {"annual": annual, "ytd": ytd, "latest_period": latest_period}


# ── Upsert ────────────────────────────────────────────────────────────────────

def _upsert_annual(session, parsed: dict) -> int:
    from models.market_data import ComexStatExport
    from models import Base
    from database import engine
    Base.metadata.create_all(engine, tables=[ComexStatExport.__table__])

    count = 0
    annual = parsed.get("annual", {})
    for year, d in annual.items():
        ref_date = date(year, 1, 1)
        total_kg  = int(d["tonnes"] * 1_000)   # toneladas → kg
        total_usd = int(d["usd_m"] * 1_000_000)
        record = {
            "ref_date":      ref_date,
            "ncm_code":      NCM_MAPA,
            "ncm_desc":      NCM_DESC,
            "total_kg":      total_kg,
            "total_usd_fob": total_usd,
            "source":        "mapa_cgda_pdf",
        }
        stmt = (
            insert(ComexStatExport)
            .values(**record)
            .on_conflict_do_update(
                constraint="uq_comex_date_ncm",
                set_={"total_kg": total_kg, "total_usd_fob": total_usd},
            )
        )
        try:
            session.execute(stmt)
            count += 1
        except Exception as e:
            logger.warning("comex_stat upsert year=%d: %s", year, e)
            session.rollback()

    session.commit()
    return count


# ── API pública ───────────────────────────────────────────────────────────────

def fetch_comex_stat(session) -> dict:
    """
    Descarga el PDF de exportaciones anuales MAPA y almacena en DB.

    Returns dict con:
      rows_upserted  : int
      latest_period  : str ("Jan-Abr", etc.)
      ytd_curr_t     : toneladas YTD año actual
      ytd_prev_t     : toneladas YTD año anterior (mismo periodo)
      yoy_change_pct : % cambio YoY
      pdf_url        : URL del PDF descargado
      errors         : list[str]
    """
    result = {
        "rows_upserted": 0,
        "latest_period": None,
        "ytd_curr_t": None,
        "ytd_prev_t": None,
        "yoy_change_pct": None,
        "pdf_url": None,
        "errors": [],
    }

    html = _get_page_html()
    if html is None:
        result["errors"].append("No se pudo acceder a la página MAPA")
        return result

    pdf_url = _find_pdf_url(html, pattern_words=("ANUAIS",))
    if pdf_url is None:
        result["errors"].append("No se encontró link al PDF de exportaciones anuales")
        return result

    result["pdf_url"] = pdf_url
    logger.info("comex_stat: descargando PDF %s", pdf_url)

    pdf_bytes = _download_pdf(pdf_url)
    if pdf_bytes is None:
        result["errors"].append(f"No se pudo descargar el PDF: {pdf_url}")
        return result

    parsed = _parse_anual_pdf(pdf_bytes)
    if not parsed:
        result["errors"].append("No se pudieron parsear datos del PDF")
        return result

    count = _upsert_annual(session, parsed)
    result["rows_upserted"] = count
    result["latest_period"] = parsed.get("latest_period")

    # YTD comparison directo desde el PDF
    ytd      = parsed.get("ytd", {})
    curr_yr  = date.today().year
    prev_yr  = curr_yr - 1
    ytd_curr = ytd.get(curr_yr, {})
    ytd_prev = ytd.get(prev_yr, {})

    if ytd_curr and ytd_prev:
        tc = ytd_curr.get("tonnes", 0)
        tp = ytd_prev.get("tonnes", 0)
        result["ytd_curr_t"]     = tc
        result["ytd_prev_t"]     = tp
        result["yoy_change_pct"] = round((tc - tp) / tp * 100, 2) if tp else None

    logger.info(
        "Comex Stat MAPA: %d años almacenados | YTD %s %s: %.1f t vs %.1f t (YoY %s%%)",
        count,
        result["latest_period"] or "?",
        curr_yr,
        result["ytd_curr_t"] or 0,
        result["ytd_prev_t"] or 0,
        result["yoy_change_pct"] or "N/D",
    )
    return result


def get_comex_ytd(session, ncm: str = NCM_MAPA) -> Optional[dict]:
    """
    Devuelve comparativa YTD desde DB (series anuales almacenadas).

    Returns dict con:
      ytd_kg_current, ytd_kg_prior, yoy_change_pct,
      months_available, latest_month, ncm_code
    """
    from sqlalchemy import text

    rows = session.execute(text("""
        SELECT ref_date, total_kg
        FROM comex_stat_export
        WHERE ncm_code = :ncm
        ORDER BY ref_date
    """), {"ncm": ncm}).fetchall()

    if not rows:
        return None

    today    = date.today()
    curr_yr  = today.year
    prior_yr = curr_yr - 1

    curr  = next((int(r[1]) for r in rows if r[0].year == curr_yr),  None)
    prior = next((int(r[1]) for r in rows if r[0].year == prior_yr), None)

    if curr is None or prior is None:
        return None

    yoy_pct = round((curr - prior) / prior * 100, 2) if prior else 0.0

    return {
        "ytd_kg_current":   curr,
        "ytd_kg_prior":     prior,
        "yoy_change_pct":   yoy_pct,
        "months_available": None,  # datos anuales, no mensuales
        "latest_month":     str(date(curr_yr, 1, 1))[:7],
        "ncm_code":         ncm,
    }
