"""
India ex-mill sugar price ingestion — ChiniMandi + DuckDuckGo fallback.

India domestic ex-mill price (Rs/quintal) drives government ethanol diversion
policy, which directly affects net sugar available for export:

  > 4700 Rs/qtl : mills prefer ethanol on parity — unconstrained diversion
                   → bearish for ICE No.11 (less exportable sugar)
  3700-4700      : govt restricts diversion to protect sugar supply
                   → neutral-bullish
  < 3500         : govt promotes ethanol to support farmer income
                   → modest bullish (more diversion = less net sugar)

Source: ChiniMandi season/month table (Oct-Sep columns).
Fallback: DuckDuckGo HTML search for ISMA/trade-press snippets.
DB table: india_sugar_price (model added separately).
"""
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

CHINIMANDI_URL = (
    "https://www.chinimandi.com/statistics/"
    "all-india-season-wise-month-wise-ex-mill-sugar-prices-rs-qtl/"
)
ETHANOL_PARITY_BREAKEVEN_RS_QTL = 4700.0
GOVT_RESTRICTION_THRESHOLD_RS_QTL = 3700.0

_PROMOTE_DIVERSION_THRESHOLD_RS_QTL = 3500.0

_TIMEOUT = 25
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Oct-Sep marketing year month order for ChiniMandi columns
_MARKETING_MONTHS = [10, 11, 12, 1, 2, 3, 4, 5, 6, 7, 8, 9]
_MONTH_ABBR = ["Oct", "Nov", "Dec", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep"]

# Season label patterns: "2024-25", "2023-24", "2024-2025"
_SEASON_RE = re.compile(r"(\d{4})-(\d{2,4})")

# Price patterns for DuckDuckGo snippets
_PRICE_RE = re.compile(
    r"(?:Rs\.?\s*|₹\s*)(\d[\d,]+)\s*(?:/\s*(?:qtl|quintal|q))?|"
    r"(\d[\d,]+)\s*(?:per\s+quintal|/\s*(?:qtl|quintal))",
    re.IGNORECASE,
)


@dataclass
class IndiaSugarPricePoint:
    price_date: date
    price_rs_qtl: float
    price_rs_kg: float
    source: str
    confidence: float


# ── Helpers ────────────────────────────────────────────────────────────────────

def _season_start_year(season_label: str) -> Optional[int]:
    """Extract starting year from '2024-25' or '2024-2025'."""
    m = _SEASON_RE.search(season_label)
    if not m:
        return None
    return int(m.group(1))


def _month_year_for_col(season_start: int, col_idx: int) -> tuple[int, int]:
    """Return (year, month) for a given column index (0=Oct, 1=Nov, … 11=Sep)."""
    month = _MARKETING_MONTHS[col_idx]
    year = season_start if month >= 10 else season_start + 1
    return year, month


def _parse_price_cell(cell: str) -> Optional[float]:
    cell = cell.strip().replace(",", "").replace("₹", "").replace("Rs", "").replace(".", "")
    try:
        v = float(cell)
        if 1000 <= v <= 10000:
            return v
    except ValueError:
        pass
    return None


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


# ── Source 1: ChiniMandi table ─────────────────────────────────────────────────

def _fetch_chinimandi_prices() -> dict[str, float]:
    """
    HTTP GET ChiniMandi ex-mill price page, parse season/month table.

    Returns {YYYY-MM: price_rs_qtl} or empty dict on any error.
    """
    try:
        resp = requests.get(CHINIMANDI_URL, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        logger.warning("ChiniMandi fetch failed: %s", exc)
        return {}

    return _parse_chinimandi_html(html)


def _parse_chinimandi_html(html: str) -> dict[str, float]:
    """Parse the season-wise month-wise table from ChiniMandi HTML."""
    results: dict[str, float] = {}

    # Locate the first <table> block
    table_m = re.search(r"<table[^>]*>(.*?)</table>", html, re.IGNORECASE | re.DOTALL)
    if not table_m:
        logger.warning("ChiniMandi: no <table> found in response")
        return results

    table_html = table_m.group(1)

    # Extract all rows
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.IGNORECASE | re.DOTALL)
    if not rows:
        return results

    header_parsed = False
    col_count = 0

    for row_html in rows:
        cells_raw = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.IGNORECASE | re.DOTALL)
        cells = [_strip_tags(c).strip() for c in cells_raw]

        if not cells:
            continue

        # Detect header row (contains month abbreviations)
        if not header_parsed:
            month_hits = sum(1 for a in _MONTH_ABBR if any(a.lower() in c.lower() for c in cells))
            if month_hits >= 6:
                col_count = len(cells) - 1  # first col is season label
                header_parsed = True
            continue

        if not cells[0]:
            continue

        season_start = _season_start_year(cells[0])
        if season_start is None:
            continue

        price_cells = cells[1:]
        for col_idx, cell in enumerate(price_cells):
            if col_idx >= len(_MARKETING_MONTHS):
                break
            price = _parse_price_cell(cell)
            if price is None:
                continue
            year, month = _month_year_for_col(season_start, col_idx)
            key = f"{year:04d}-{month:02d}"
            # Only overwrite if more recent season data exists
            if key not in results:
                results[key] = price

    logger.info("ChiniMandi: parsed %d price points", len(results))
    return results


# ── Source 2: DuckDuckGo HTML fallback ────────────────────────────────────────

def _search_latest_price() -> Optional[IndiaSugarPricePoint]:
    """
    DuckDuckGo HTML search fallback for latest India ex-mill price.
    Confidence 0.65 — snippet data, not authoritative table.
    """
    query = "India ex-mill sugar price ₹ quintal latest 2026 ISMA"
    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.debug("DuckDuckGo returned HTTP %d", resp.status_code)
            return None
        html = resp.text
    except Exception as exc:
        logger.debug("DuckDuckGo search error: %s", exc)
        return None

    snippets = _extract_ddg_snippets(html)
    return _parse_snippets_for_price(snippets)


def _extract_ddg_snippets(html: str) -> list[str]:
    snippet_re = re.compile(
        r'class="[^"]*(?:result__snippet|result__body|snippet)[^"]*"[^>]*>(.*?)</(?:a|span|div)>',
        re.IGNORECASE | re.DOTALL,
    )
    title_re = re.compile(
        r'class="[^"]*result__title[^"]*"[^>]*>.*?<a[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    clean = re.compile(r"<[^>]+>")
    snippets = []
    for m in snippet_re.finditer(html):
        text = clean.sub(" ", m.group(1)).strip()
        if len(text) > 20:
            snippets.append(text)
    for m in title_re.finditer(html):
        text = clean.sub(" ", m.group(1)).strip()
        if len(text) > 15:
            snippets.append(text)
    return snippets


def _parse_snippets_for_price(snippets: list[str]) -> Optional[IndiaSugarPricePoint]:
    """Extract the most plausible ex-mill price from DuckDuckGo snippets."""
    candidates: list[tuple[float, date]] = []
    today = date.today()

    for snippet in snippets:
        if not re.search(
            r"\b(?:ex.?mill|exmill|India.{0,20}sugar|sugar.{0,20}India)\b",
            snippet, re.IGNORECASE,
        ):
            continue

        for m in _PRICE_RE.finditer(snippet):
            raw = (m.group(1) or m.group(2) or "").replace(",", "")
            try:
                price = float(raw)
            except ValueError:
                continue
            if not (2000 <= price <= 8000):
                continue

            # Try to find a date in the snippet
            price_date = _parse_snippet_date(snippet) or today
            candidates.append((price, price_date))

    if not candidates:
        return None

    # Pick the candidate with the most recent date
    best_price, best_date = max(candidates, key=lambda x: x[1])
    logger.info(
        "DuckDuckGo ex-mill: Rs %.0f/qtl as of %s", best_price, best_date.isoformat()
    )
    return IndiaSugarPricePoint(
        price_date=best_date,
        price_rs_qtl=best_price,
        price_rs_kg=round(best_price / 100.0, 4),
        source="duckduckgo_snippet",
        confidence=0.65,
    )


_DATE_RE = re.compile(
    r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(\d{4})|"
    r"(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _parse_snippet_date(text: str) -> Optional[date]:
    for m in _DATE_RE.finditer(text):
        try:
            if m.group(1):
                d = date(int(m.group(3)), _MONTH_MAP[m.group(2).lower()], int(m.group(1)))
            else:
                d = date(int(m.group(6)), _MONTH_MAP[m.group(4).lower()], int(m.group(5)))
            if date(2020, 1, 1) <= d <= date.today():
                return d
        except (ValueError, KeyError):
            pass
    for m in _ISO_DATE_RE.finditer(text):
        try:
            d = date.fromisoformat(m.group(1))
            if date(2020, 1, 1) <= d <= date.today():
                return d
        except ValueError:
            pass
    return None


# ── Source 3: DB ───────────────────────────────────────────────────────────────

def _load_from_db(session) -> Optional[IndiaSugarPricePoint]:
    """
    Read the most recent record from india_sugar_price table.
    Returns None if no data or data older than 45 days.
    """
    try:
        from models.market_data import IndiaSugarPrice
        from sqlalchemy import select

        row = session.execute(
            select(IndiaSugarPrice)
            .order_by(IndiaSugarPrice.price_date.desc())
            .limit(1)
        ).scalar_one_or_none()

        if row is None:
            return None

        age_days = (date.today() - row.price_date).days
        if age_days > 45:
            logger.info(
                "india_sugar_price DB: dato de %d dias — demasiado viejo (max 45)", age_days
            )
            return None

        logger.info(
            "india_sugar_price DB: Rs %.0f/qtl al %s — %d dias [%s]",
            float(row.price_rs_qtl), row.price_date.isoformat(), age_days, row.source,
        )
        return IndiaSugarPricePoint(
            price_date=row.price_date,
            price_rs_qtl=float(row.price_rs_qtl),
            price_rs_kg=float(row.price_rs_kg),
            source=f"db_{row.source}",
            confidence=float(row.confidence),
        )

    except Exception as exc:
        logger.debug("india_sugar_price DB load: %s", exc)
        return None


# ── Fetch & store ──────────────────────────────────────────────────────────────

def fetch_and_store_prices(session) -> int:
    """
    Fetch latest prices from ChiniMandi and upsert into DB.

    Only stores the last 24 months. Returns count of rows upserted.
    """
    prices = _fetch_chinimandi_prices()
    if not prices:
        logger.warning("fetch_and_store_prices: no prices fetched from ChiniMandi")
        return 0

    cutoff = date.today() - timedelta(days=365 * 2)
    upserted = 0

    try:
        from models.market_data import IndiaSugarPrice
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        for ym, price_rs_qtl in prices.items():
            try:
                year, month = int(ym[:4]), int(ym[5:7])
                price_date = date(year, month, 1)
            except ValueError:
                continue

            if price_date < cutoff:
                continue

            rec = {
                "price_date":  price_date,
                "price_rs_qtl": price_rs_qtl,
                "price_rs_kg":  round(price_rs_qtl / 100.0, 4),
                "source":      "chinimandi",
                "confidence":  0.85,
            }
            stmt = (
                pg_insert(IndiaSugarPrice)
                .values(**rec)
                .on_conflict_do_update(
                    constraint="uq_india_sugar_price_date",
                    set_={k: v for k, v in rec.items() if k != "price_date"},
                )
            )
            try:
                session.execute(stmt)
                upserted += 1
            except Exception as exc:
                logger.warning("india_sugar_price upsert %s: %s", ym, exc)
                session.rollback()

        if upserted:
            session.commit()
        logger.info("india_sugar_price: %d rows upserted", upserted)

    except Exception as exc:
        logger.error("fetch_and_store_prices: %s", exc)

    return upserted


# ── Public API ─────────────────────────────────────────────────────────────────

def get_latest_exmill_price(session=None) -> Optional[IndiaSugarPricePoint]:
    """
    Return the most recent India ex-mill sugar price point.

    Priority:
      1. DB (age <= 45 days)
      2. ChiniMandi live fetch
      3. DuckDuckGo HTML search

    Returns None if all sources fail.
    """
    # 1. DB
    if session is not None:
        db_point = _load_from_db(session)
        if db_point:
            return db_point

    # 2. ChiniMandi live fetch
    prices = _fetch_chinimandi_prices()
    if prices:
        latest_ym = max(prices)
        price_rs_qtl = prices[latest_ym]
        try:
            year, month = int(latest_ym[:4]), int(latest_ym[5:7])
            price_date = date(year, month, 1)
        except ValueError:
            price_date = date.today().replace(day=1)

        logger.info(
            "ChiniMandi live: Rs %.0f/qtl at %s", price_rs_qtl, price_date.isoformat()
        )
        return IndiaSugarPricePoint(
            price_date=price_date,
            price_rs_qtl=price_rs_qtl,
            price_rs_kg=round(price_rs_qtl / 100.0, 4),
            source="chinimandi",
            confidence=0.85,
        )

    # 3. DuckDuckGo fallback
    ddg_point = _search_latest_price()
    if ddg_point:
        return ddg_point

    logger.warning("get_latest_exmill_price: all sources exhausted, returning None")
    return None


def get_price_signal(price_rs_qtl: float) -> dict:
    """
    Translate an India ex-mill price into a trading signal for ICE No.11.

    Returns:
      {
        "signal":      "parity_above_breakeven" | "restrict_diversion" |
                       "promote_diversion" | "neutral",
        "bias":        "LONG" | "SHORT" | "NEUTRAL",
        "description": str,
      }

    Rationale:
      > 4700  Mills prefer ethanol on economic parity → unconstrained diversion
               → less net sugar available → SHORT ICE No.11 (paradoxically
               bearish because mills divert away from sugar).
               NOTE: in practice govt often restricts above 3700 before 4700
               is reached, moderating this signal.
      3700-4700  Govt typically restricts diversion → net sugar supply
                  protected → NEUTRAL to mildly bullish.
      3500-3700  No strong govt intervention either way → NEUTRAL.
      < 3500  Govt promotes ethanol to support farmer income → more cane
               diverted → less net sugar → LONG ICE No.11.
    """
    if price_rs_qtl > ETHANOL_PARITY_BREAKEVEN_RS_QTL:
        return {
            "signal": "parity_above_breakeven",
            "bias": "SHORT",
            "description": (
                f"Ex-mill Rs {price_rs_qtl:.0f}/qtl exceeds ethanol parity breakeven "
                f"(Rs {ETHANOL_PARITY_BREAKEVEN_RS_QTL:.0f}). Mills maximize ethanol output; "
                "unconstrained diversion reduces exportable sugar — bearish ICE No.11."
            ),
        }

    if price_rs_qtl > GOVT_RESTRICTION_THRESHOLD_RS_QTL:
        return {
            "signal": "restrict_diversion",
            "bias": "NEUTRAL",
            "description": (
                f"Ex-mill Rs {price_rs_qtl:.0f}/qtl above govt restriction threshold "
                f"(Rs {GOVT_RESTRICTION_THRESHOLD_RS_QTL:.0f}). Government typically limits "
                "juice-to-ethanol diversion to protect domestic sugar supply — neutral-bullish."
            ),
        }

    if price_rs_qtl < _PROMOTE_DIVERSION_THRESHOLD_RS_QTL:
        return {
            "signal": "promote_diversion",
            "bias": "LONG",
            "description": (
                f"Ex-mill Rs {price_rs_qtl:.0f}/qtl below promotion threshold "
                f"(Rs {_PROMOTE_DIVERSION_THRESHOLD_RS_QTL:.0f}). Government promotes ethanol "
                "to support farmer income → increased diversion → less net sugar → "
                "modestly bullish ICE No.11."
            ),
        }

    return {
        "signal": "neutral",
        "bias": "NEUTRAL",
        "description": (
            f"Ex-mill Rs {price_rs_qtl:.0f}/qtl in neutral band "
            f"(Rs {_PROMOTE_DIVERSION_THRESHOLD_RS_QTL:.0f}–"
            f"Rs {GOVT_RESTRICTION_THRESHOLD_RS_QTL:.0f}). "
            "No strong government diversion incentive in either direction."
        ),
    }
