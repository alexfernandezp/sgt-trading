import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ── CCEA procurement prices (Rs/litre) ────────────────────────────────────────
# ESY 2022-23 through 2025-26: prices unchanged across this window per CCEA releases.
JUICE_PRICE_RS_L: float = 65.61
B_HEAVY_PRICE_RS_L: float = 60.73
C_HEAVY_PRICE_RS_L: float = 57.97
GRAIN_PRICE_RS_L: float = 66.07

# Physical conversion constants per tonne of sugarcane crushed.
JUICE_LITERS_PER_TONNE_CANE: float = 84.0
SUGAR_KG_PER_TONNE_CANE: float = 115.0
MOLASSES_VALUE_RS_PER_TONNE_CANE: float = 200.0

LAKH_TO_MT: float = 0.1
MT_TO_LAKH: float = 10.0

_TIMEOUT = 20
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# ── Seed data ─────────────────────────────────────────────────────────────────
# ISMA historical actuals: sugar-equivalent diverted to ethanol, Mt.
# 2023 includes partial season impact of Dec 2023 government ban on juice/syrup route.
_DIVERSION_SEED: dict[int, float] = {
    2021: 2.0,
    2022: 3.4,
    2023: 3.8,
    2024: 3.4,
    2025: 3.1,
}

# Gross (pre-diversion) production by season year, Mt.
_GROSS_PRODUCTION_SEED: dict[int, float] = {
    2020: 32.0,
    2021: 32.5,
    2022: 39.4,
    2023: 36.6,
    2024: 29.5,
    2025: 32.4,
}

# Regex: LMT / lakh tonnes / Mt diversion values.
# Handles: "3.4 LMT", "3.8 lakh tonnes", "3.1 Mt", "34 lakh MT"
_DIVERSION_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:lakh\s+(?:tonne|ton|MT|mt)s?|LMT)",
    re.IGNORECASE,
)
_MT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:million\s+(?:tonne|ton)|MMT|Mt)\b",
    re.IGNORECASE,
)


# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class EthanolData:
    season_year: int
    diversion_mt: float
    diversion_lmt: float
    esy_year: str                   # e.g. "2025-26"
    data_type: str                  # "actual" | "estimate" | "formula"
    source: str
    confidence: float               # 0–1


# ── Formula / seed helpers ────────────────────────────────────────────────────

def _make_esy(season_year: int) -> str:
    return f"{season_year}-{str(season_year + 1)[2:]}"


def _formula_diversion_mt(season_year: int) -> float:
    if season_year in _DIVERSION_SEED:
        return _DIVERSION_SEED[season_year]

    if season_year < 2021:
        # Linear back-extrapolation anchored at 2021 = 2.0 Mt.
        # EBP was ramping up; assume ~0.3 Mt/yr decline going backwards.
        return max(0.5, 2.0 - (2021 - season_year) * 0.3)

    # Post-seed forward trend: programme matures but juice-route ban limits growth.
    base = 3.0
    drift = (season_year - 2026) * (-0.1)
    return max(1.5, min(5.0, base + drift))


# ── Parity computation ────────────────────────────────────────────────────────

def compute_parity_ratio(exmill_price_rs_kg: float, route: str = "juice") -> float:
    """
    Revenue ratio: ethanol route vs sugar route, per tonne of cane crushed.

    > 1.0 → mills economically prefer ethanol (subject to government quota).
    Breakeven for juice route at JUICE_PRICE_RS_L: ~₹47/kg exmill.
    """
    price_map = {
        "juice": JUICE_PRICE_RS_L,
        "b_heavy": B_HEAVY_PRICE_RS_L,
        "c_heavy": C_HEAVY_PRICE_RS_L,
        "grain": GRAIN_PRICE_RS_L,
    }
    ethanol_price = price_map.get(route.lower(), JUICE_PRICE_RS_L)
    revenue_ethanol = JUICE_LITERS_PER_TONNE_CANE * ethanol_price
    revenue_sugar = SUGAR_KG_PER_TONNE_CANE * exmill_price_rs_kg + MOLASSES_VALUE_RS_PER_TONNE_CANE
    return revenue_ethanol / revenue_sugar


# ── Source 1: DB ──────────────────────────────────────────────────────────────

def _load_from_db(session, season_year: int) -> Optional[EthanolData]:
    try:
        from models.market_data import IndiaEthanolDiversion
        from sqlalchemy import select

        row = session.execute(
            select(IndiaEthanolDiversion)
            .where(IndiaEthanolDiversion.season_year == season_year)
            .order_by(IndiaEthanolDiversion.updated_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        if row is None:
            return None

        diversion_mt = float(row.diversion_mt)
        logger.info(
            "india_ethanol DB: season %d → %.2f Mt [%s]",
            season_year, diversion_mt, row.data_type,
        )
        return EthanolData(
            season_year=season_year,
            diversion_mt=diversion_mt,
            diversion_lmt=round(diversion_mt * MT_TO_LAKH, 2),
            esy_year=_make_esy(season_year),
            data_type=str(row.data_type),
            source=f"db_{row.source}",
            confidence=float(row.confidence),
        )

    except Exception as e:
        logger.debug("india_ethanol DB load: %s", e)
        return None


# ── Source 2: DuckDuckGo search ───────────────────────────────────────────────

def _duckduckgo_search(query: str) -> Optional[str]:
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


def _extract_snippets(html: str) -> list[str]:
    snippet_re = re.compile(
        r'class="[^"]*(?:result__snippet|result__body|snippet)[^"]*"[^>]*>(.*?)</(?:a|span|div)>',
        re.IGNORECASE | re.DOTALL,
    )
    clean = re.compile(r"<[^>]+>")
    snippets = []
    for m in snippet_re.finditer(html):
        text = clean.sub(" ", m.group(1)).strip()
        if len(text) > 20:
            snippets.append(text)
    return snippets


def _parse_diversion_from_snippets(snippets: list[str], season_year: int) -> Optional[float]:
    """
    Extract a plausible diversion figure (Mt) from DuckDuckGo snippets.
    Filters snippets to those explicitly mentioning ethanol diversion context.
    """
    candidates: list[float] = []
    for snippet in snippets:
        if not re.search(
            r"\b(?:ethanol|diversion|EBP|juice.{0,10}syrup|cane.{0,10}ethanol)\b",
            snippet, re.IGNORECASE,
        ):
            continue

        for m in _DIVERSION_RE.finditer(snippet):
            try:
                v = float(m.group(1))
                # Plausible LMT range: 5–60 LMT = 0.5–6.0 Mt
                if 5.0 <= v <= 60.0:
                    candidates.append(v * LAKH_TO_MT)
            except ValueError:
                pass

        for m in _MT_RE.finditer(snippet):
            try:
                v = float(m.group(1))
                if 0.5 <= v <= 6.0:
                    candidates.append(v)
            except ValueError:
                pass

    return round(max(candidates), 2) if candidates else None


def _search_isma_diversion(season_year: int) -> Optional[EthanolData]:
    esy = _make_esy(season_year)
    queries = [
        f"ISMA India ethanol diversion sugar equivalent {season_year} lakh tonnes",
        f"India sugar ethanol diversion ESY {esy} actual",
    ]

    for query in queries:
        html = _duckduckgo_search(query)
        if not html:
            continue
        snippets = _extract_snippets(html)
        if not snippets:
            continue
        diversion_mt = _parse_diversion_from_snippets(snippets, season_year)
        if diversion_mt is not None:
            logger.info(
                "india_ethanol search: season %d → %.2f Mt (DuckDuckGo)",
                season_year, diversion_mt,
            )
            return EthanolData(
                season_year=season_year,
                diversion_mt=diversion_mt,
                diversion_lmt=round(diversion_mt * MT_TO_LAKH, 2),
                esy_year=esy,
                data_type="estimate",
                source="duckduckgo_snippet",
                confidence=0.70,
            )

    logger.info("india_ethanol search: no diversion data found for season %d", season_year)
    return None


# ── Public interface ──────────────────────────────────────────────────────────

def get_season_diversion(season_year: int, session=None) -> EthanolData:
    """
    Priority: DB → DuckDuckGo search → formula seed/trend.
    """
    if session is not None:
        db_data = _load_from_db(session, season_year)
        if db_data is not None:
            return db_data

    search_data = _search_isma_diversion(season_year)
    if search_data is not None:
        return search_data

    diversion_mt = _formula_diversion_mt(season_year)
    data_type = "actual" if season_year in _DIVERSION_SEED else "formula"
    conf = 0.85 if data_type == "actual" else 0.55

    logger.info(
        "india_ethanol formula: season %d → %.2f Mt [%s]",
        season_year, diversion_mt, data_type,
    )
    return EthanolData(
        season_year=season_year,
        diversion_mt=diversion_mt,
        diversion_lmt=round(diversion_mt * MT_TO_LAKH, 2),
        esy_year=_make_esy(season_year),
        data_type=data_type,
        source="seed" if data_type == "actual" else "formula",
        confidence=conf,
    )


def get_gross_production(season_year: int, net_mt: float, session=None) -> float:
    diversion = get_season_diversion(season_year, session=session)
    gross = net_mt + diversion.diversion_mt
    logger.info(
        "India season %d: net=%.2fMt + diversion=%.2fMt = gross=%.2fMt",
        season_year, net_mt, diversion.diversion_mt, gross,
    )
    return round(gross, 3)


def estimate_closing_stock_lmt(
    season_year: int,
    gross_mt: float,
    opening_stock_lmt: float = 80.0,
    consumption_lmt: float = 280.0,
    exports_lmt: float = 0.0,
    session=None,
) -> float:
    """
    Simple sugar balance in LMT.
    closing = opening + gross_lmt - consumption_lmt - exports_lmt - diversion_lmt

    Diversion is subtracted separately because gross_mt is pre-diversion;
    the net sugar available to the balance is gross minus diversion.
    """
    diversion = get_season_diversion(season_year, session=session)
    gross_lmt = round(gross_mt * MT_TO_LAKH, 2)
    closing = opening_stock_lmt + gross_lmt - consumption_lmt - exports_lmt - diversion.diversion_lmt
    return round(closing, 2)
