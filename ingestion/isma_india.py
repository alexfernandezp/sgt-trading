"""
ISMA India Sugar Production — lector BD + DuckDuckGo search fallback.

ISMA (Indian Sugar & Bio-energy Manufacturers Association) publica datos
de produccion quincenal durante la temporada de molienda Oct-Abr.

Arquitectura de datos:
  1. BD local (tabla isma_release) — fuente principal
     Poblada via: py scripts/ingest_isma.py --date YYYY-MM-DD --lakh NNN.NN
  2. DuckDuckGo HTML search — fallback si BD sin dato reciente
     Busca snippets de resultados que contienen el numero directamente
     sin necesidad de visitar articulos completos

CRITICO: ISMA reporta azucar PRODUCIDA en fabricas, ya NETA de diversion
juice-to-ethanol. No aplicar factor etanol adicional sobre datos ISMA.
El dato ISMA es directamente comparable al balance mundial de azucar.

Serie 2025-26 (cargada via --seed en ingest_isma.py):
  Abr 30 2026: 275.28 lakh t = 27.528 Mt (+7% YoY) — FINAL temporada
  Abr 15 2026: 274.80 lakh t = 27.480 Mt (+8% YoY)
  Mar 31 2026: 272.31 lakh t = 27.231 Mt (+9% YoY)
  Dic 31 2025: 118.97 lakh t = 11.897 Mt (+25% YoY)
"""
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

LAKH_TO_MT = 0.1   # 1 lakh tonne = 0.1 Mt

_TIMEOUT = 20
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Progreso de temporada por mes (Oct-Abr crushing)
_SEASON_PROGRESS: dict[int, float] = {
    10: 0.05, 11: 0.18, 12: 0.36,
    1:  0.55, 2:  0.72, 3:  0.86,
    4:  0.96, 5:  1.00,
    6:  1.00, 7:  1.00, 8:  1.00, 9:  1.00,
}

# Lakh tonnes: "272.31 lakh ton", "274.8 lakh tons", "275 LMT"
_LAKH_RE = re.compile(
    r"(\d{2,4}(?:[,\.]\d+)*)\s*(?:lakh\s+(?:tonne|ton|MT|mt)s?|LMT)",
    re.IGNORECASE,
)
# Millones de toneladas: "27.23 million T", "26.21 MT", "27.5 mn tonnes"
_MILLION_RE = re.compile(
    r"(\d{1,3}(?:\.\d+)?)\s*(?:million\s+(?:tonne|ton)|mn\s+(?:tonne|ton)|MMT)\b",
    re.IGNORECASE,
)
# Fecha "as on/of DATE"
_AS_ON_RE = re.compile(
    r"as\s+(?:on|of)\s+((?:\d{1,2}\s+)?\w+\s+\d{1,2}(?:,\s*|\s+)\d{4}|\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(\d{4})|"
    r"(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})",
    re.IGNORECASE,
)
_ISO_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


@dataclass
class IsmaData:
    cumulative_mt: float
    pub_date: date
    season_year: int
    season_progress_pct: float
    estimated_full_year_mt: float
    data_source: str
    confidence: float
    raw_lakh_tonnes: float = 0.0


# ── Utilidades ─────────────────────────────────────────────────────────────────

def _season_year(d: date) -> int:
    return d.year if d.month >= 10 else d.year - 1


def _season_progress(d: date) -> float:
    return _SEASON_PROGRESS.get(d.month, 1.0)


def _project_full_year(cumulative_mt: float, progress: float) -> float:
    if progress >= 0.95:
        return round(cumulative_mt * 1.003, 2)
    if progress < 0.05:
        return cumulative_mt
    return round(min(cumulative_mt / progress, cumulative_mt * 1.15), 2)


def _parse_lakh(text: str) -> Optional[float]:
    candidates = []
    for m in _LAKH_RE.finditer(text):
        try:
            v = float(m.group(1).replace(",", ""))
            if 100 <= v <= 400:
                candidates.append(v)
        except ValueError:
            pass
    if not candidates:
        for m in _MILLION_RE.finditer(text):
            try:
                v = float(m.group(1))
                if 10 <= v <= 40:
                    candidates.append(v * 10)
            except ValueError:
                pass
    return max(candidates) if candidates else None


def _parse_date(text: str) -> Optional[date]:
    for m in _DATE_RE.finditer(text):
        try:
            if m.group(1):   # DD Month YYYY
                d = date(int(m.group(3)), _MONTH_MAP[m.group(2).lower()], int(m.group(1)))
            else:             # Month DD, YYYY
                d = date(int(m.group(6)), _MONTH_MAP[m.group(4).lower()], int(m.group(5)))
            if date(2020, 1, 1) <= d <= date.today():
                return d
        except (ValueError, KeyError):
            pass
    for m in _ISO_RE.finditer(text):
        try:
            d = date.fromisoformat(m.group(1))
            if date(2020, 1, 1) <= d <= date.today():
                return d
        except ValueError:
            pass
    return None


# ── Fuente 1: BD local ─────────────────────────────────────────────────────────

def _load_from_db(session, max_age_days: int = 90) -> Optional[IsmaData]:
    """
    Lee el release ISMA mas reciente de la BD (tabla isma_release).
    Retorna None si no hay datos o si son demasiado viejos.
    """
    try:
        from models.market_data import IsmaRelease
        from sqlalchemy import select

        row = session.execute(
            select(IsmaRelease)
            .order_by(IsmaRelease.data_date.desc())
            .limit(1)
        ).scalar_one_or_none()

        if row is None:
            return None

        age_days = (date.today() - row.data_date).days
        if age_days > max_age_days:
            logger.info("ISMA BD: dato de %d dias — demasiado viejo (max %d)",
                        age_days, max_age_days)
            return None

        progress = _season_progress(row.data_date)
        conf = 0.90 if progress >= 0.90 else (0.85 if progress >= 0.70 else 0.80)

        logger.info(
            "ISMA BD: %.3f Mt (%.2f lakh t) al %s — %d dias [%s]",
            float(row.cumulative_mt), float(row.cumulative_lakh_t),
            row.data_date.isoformat(), age_days, row.source,
        )

        return IsmaData(
            cumulative_mt=float(row.cumulative_mt),
            pub_date=row.data_date,
            season_year=row.marketing_year,
            season_progress_pct=float(row.season_progress_pct or progress * 100),
            estimated_full_year_mt=float(row.estimated_full_year_mt),
            data_source=f"db_{row.source}",
            confidence=conf,
            raw_lakh_tonnes=float(row.cumulative_lakh_t),
        )

    except Exception as e:
        logger.debug("ISMA BD load: %s", e)
        return None


# ── Fuente 2: DuckDuckGo HTML search ──────────────────────────────────────────

def _duckduckgo_search(query: str) -> Optional[str]:
    """
    Busca en DuckDuckGo usando el endpoint HTML (sin API key).
    Retorna el HTML de resultados o None.
    """
    try:
        import httpx
        r = httpx.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=_HEADERS,
            timeout=_TIMEOUT,
            follow_redirects=True,
        )
        if r.status_code == 200:
            return r.text
        logger.debug("DuckDuckGo HTTP %d", r.status_code)
    except Exception as e:
        logger.debug("DuckDuckGo search: %s", e)
    return None


def _extract_ddg_snippets(html: str) -> list[str]:
    """
    Extrae snippets de texto de resultados DuckDuckGo HTML.
    Los snippets frecuentemente contienen el valor en lakh tonnes directamente.
    """
    # DuckDuckGo HTML tiene resultados en <a class="result__snippet"> o similar
    snippet_re = re.compile(
        r'class="[^"]*(?:result__snippet|result__body|snippet)[^"]*"[^>]*>(.*?)</(?:a|span|div)>',
        re.IGNORECASE | re.DOTALL,
    )
    title_re = re.compile(
        r'class="[^"]*result__title[^"]*"[^>]*>.*?<a[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )

    # Limpiar HTML tags del texto
    clean = re.compile(r'<[^>]+>')

    snippets = []
    for m in snippet_re.finditer(html):
        text = clean.sub(' ', m.group(1)).strip()
        if len(text) > 20:
            snippets.append(text)

    # Tambien extraer titulos (suelen tener "274.8 lakh tons: ISMA")
    for m in title_re.finditer(html):
        text = clean.sub(' ', m.group(1)).strip()
        if len(text) > 15:
            snippets.append(text)

    return snippets


def _parse_snippets_for_isma(snippets: list[str]) -> Optional[IsmaData]:
    """
    Extrae el dato ISMA mas reciente de una lista de snippets.
    Busca la combinacion: lakh value + fecha "as on DATE".
    """
    candidates = []

    for snippet in snippets:
        # Solo procesar snippets que mencionen ISMA o India sugar
        if not re.search(r"\b(?:ISMA|India.{0,10}sugar|sugar.{0,10}India)\b",
                         snippet, re.IGNORECASE):
            continue

        lakh_val = _parse_lakh(snippet)
        if lakh_val is None:
            continue

        # Buscar "as on/of DATE" primero
        data_date = None
        m_as_on = _AS_ON_RE.search(snippet)
        if m_as_on:
            data_date = _parse_date(m_as_on.group(1))

        if data_date is None:
            data_date = _parse_date(snippet)

        if data_date is None:
            continue

        cumulative_mt = round(lakh_val * LAKH_TO_MT, 3)
        progress = _season_progress(data_date)
        full_yr = _project_full_year(cumulative_mt, progress)
        conf = 0.88 if progress >= 0.90 else (0.83 if progress >= 0.70 else 0.78)

        candidates.append(IsmaData(
            cumulative_mt=cumulative_mt,
            pub_date=data_date,
            season_year=_season_year(data_date),
            season_progress_pct=round(progress * 100, 1),
            estimated_full_year_mt=full_yr,
            data_source="duckduckgo_snippet",
            confidence=conf,
            raw_lakh_tonnes=lakh_val,
        ))

    if not candidates:
        return None

    best = max(candidates, key=lambda x: x.pub_date)
    logger.info(
        "ISMA DuckDuckGo: %.3f Mt (%.2f lakh t) al %s [prog=%.0f%%] → est. %.2f Mt",
        best.cumulative_mt, best.raw_lakh_tonnes, best.pub_date.isoformat(),
        best.season_progress_pct, best.estimated_full_year_mt,
    )
    return best


def fetch_isma_via_search() -> Optional[IsmaData]:
    """
    Obtiene datos ISMA via DuckDuckGo search.
    El snippet de resultado suele contener el numero directamente.
    """
    # Año de temporada actual para afinar la busqueda
    today = date.today()
    season_yr = today.year if today.month >= 10 else today.year - 1
    season_str = f"{season_yr}-{str(season_yr+1)[2:]}"  # "2025-26"

    queries = [
        f"ISMA India sugar production lakh tons {season_str} season latest",
        f"India sugar production {today.year} lakh tonnes ISMA as on",
    ]

    for q in queries:
        html = _duckduckgo_search(q)
        if not html:
            continue

        snippets = _extract_ddg_snippets(html)
        if not snippets:
            continue

        result = _parse_snippets_for_isma(snippets)
        if result:
            return result

    logger.info("ISMA DuckDuckGo: sin datos en snippets")
    return None


# ── Interfaz publica ───────────────────────────────────────────────────────────

def fetch_isma_latest(session=None) -> Optional[IsmaData]:
    """
    Obtiene los datos ISMA mas recientes.

    Orden:
      1. BD local (isma_release) — dato confirmado, max 90 dias
      2. DuckDuckGo HTML search — snippet con lakh tonnes

    El dato retornado es NETO (ya sin diversion etanol).
    No aplicar factor etanol adicional en el modelo.

    Retorna None si no hay datos → modelo usa fallback etanol conservador.
    """
    # 1. BD local (prioritaria — dato confirmado por el trader)
    if session is not None:
        db_data = _load_from_db(session, max_age_days=90)
        if db_data:
            return db_data

    # 2. DuckDuckGo search
    search_data = fetch_isma_via_search()
    if search_data:
        return search_data

    logger.warning("ISMA India: sin datos disponibles (BD vacia y search sin resultado)")
    return None


def india_full_year_estimate(session=None) -> tuple[Optional[float], str, float]:
    """
    Devuelve estimacion full-year de produccion India.

    Returns:
        (estimated_mt, source_str, confidence)
        estimated_mt = None → modelo aplica fallback etanol conservador

    Freshness:
      BD dato confirmado <= 45d   : conf original (0.90)
      BD dato fin temporada 45-90d: conf 0.82 (dato definitivo pero viejo)
      DuckDuckGo fresco <= 30d   : conf ligeramente reducida (0.83-0.88)
      > 90d o sin datos          : None → fallback
    """
    isma = fetch_isma_latest(session)

    if isma is None:
        return None, "usda_baseline", 0.80

    age_days = (date.today() - isma.pub_date).days

    if age_days <= 45:
        return isma.estimated_full_year_mt, isma.data_source, isma.confidence
    elif age_days <= 90:
        return isma.estimated_full_year_mt, f"{isma.data_source}_stale", 0.82
    else:
        return None, "isma_too_old", 0.55
