"""
MAPA (Ministério da Agricultura) sugarcane production scraper.

MAPA publishes "Volumes Acumulados" XLS files for each quinzena, typically
10-15 days BEFORE UNICA. Data is cumulative from season start.

De-accumulation: net_Q_n = cum_Q_n - cum_Q_{n-1}

URL pattern: arquivos-{YYYY}-{YYYY+1}/Acompanhamentodaproduo{YY}{YY+1}_{DDMMYY}.xls
where {DDMMYY} is the period-end date = UNICA position_date - 1 day (or same day).

Quinzena mapping (MAPA period_final → UNICA position_date):
  15/04 → Apr-16 (Q1)
  01/05 → May-01 (Q2)
  15/05 → May-16 (Q3)
  01/06 → Jun-01 (Q4)
  ... etc.
"""
import logging
import re
from datetime import date, timedelta
from typing import Optional
import urllib.request
import xlrd

logger = logging.getLogger(__name__)

_SAFRA_PAGE = "https://www.gov.br/agricultura/pt-br/assuntos/sustentabilidade/agroenergia/acompanhamento-da-producao-sucroalcooleira/{slug}"
_XLS_BASE   = "https://www.gov.br/agricultura/pt-br/assuntos/sustentabilidade/agroenergia/acompanhamento-da-producao-sucroalcooleira/arquivos-{slug}/{fname}"
_UA         = "Mozilla/5.0 (compatible; SGT-Trading/1.0)"

# MAPA period_final date → UNICA position_date (same quinzena)
# MAPA reports "period final = 15/04" which is Q1; UNICA calls it "Apr-16"
# We normalize to match UNICA's date convention.
# MAPA period_final date + 1 day = UNICA position_date (consistent across all quinzenas)
# MAPA: "30/04/2026" → UNICA: 2026-05-01 (Q2)
# MAPA: "15/04/2026" → UNICA: 2026-04-16 (Q1)
# MAPA: "15/05/2026" → UNICA: 2026-05-16 (Q3)
_MAPA_DATE_OFFSET = 1  # days


def _fetch_bytes(url: str, timeout: int = 30) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as exc:
        logger.warning("mapa: fetch failed %s: %s", url, exc)
        return None


def _parse_xls_cs_total(data: bytes) -> Optional[dict]:
    """Parse MAPA XLS and extract Centro-Sul cumulative totals + period dates."""
    try:
        wb = xlrd.open_workbook(file_contents=data)
        sh = wb.sheets()[0]
        in_cs = False
        period_final_date = None

        for i in range(sh.nrows):
            row_vals = [str(sh.cell_value(i, j)) for j in range(sh.ncols)]

            # Detect Centro-Sul block header row and extract period
            if any("Centro-Sul" in v for v in row_vals):
                in_cs = True
                # Parse "Período final: 30/04/2026" — full date, +1 day = UNICA date
                for v in row_vals:
                    # Match DD/MM/YYYY or DD/MM (with optional year)
                    m = re.search(
                        r"final[:\s]+(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?",
                        v, re.IGNORECASE,
                    )
                    if m:
                        d_day, d_mo = int(m.group(1)), int(m.group(2))
                        yr_raw = m.group(3)
                        if yr_raw:
                            yr = int(yr_raw) if len(yr_raw) == 4 else 2000 + int(yr_raw)
                        else:
                            yr = date.today().year
                        try:
                            mapa_dt = date(yr, d_mo, d_day)
                            period_final_date = mapa_dt + timedelta(days=_MAPA_DATE_OFFSET)
                        except ValueError:
                            pass

            if in_cs and row_vals[0].strip() == "Tot.":
                try:
                    cane_t  = float(sh.cell_value(i, 1))
                    sugar_t = float(sh.cell_value(i, 2))
                    eth_m3  = float(sh.cell_value(i, 5)) if sh.ncols > 5 else None
                    return {
                        "cum_cane_mt":  round(cane_t  / 1_000_000, 4),
                        "cum_sugar_mt": round(sugar_t / 1_000_000, 5),
                        "cum_eth_m3":   round(eth_m3  / 1_000_000, 4) if eth_m3 else None,
                        "period_final_date": period_final_date,
                    }
                except Exception:
                    pass
    except Exception as exc:
        logger.warning("mapa: xls parse error: %s", exc)
    return None


def scrape_mapa_page(safra_slug: str) -> list:
    """
    Scrape the MAPA season page and return list of XLS URLs with dates.
    safra_slug: e.g. "2026-2027"
    Returns: [{url, date_label, suffix}]
    """
    page_url = _SAFRA_PAGE.format(slug=safra_slug)
    data = _fetch_bytes(page_url)
    if not data:
        return []

    html = data.decode("utf-8", errors="replace")
    # Find all XLS links
    pattern = re.compile(
        r'href="([^"]*arquivos-[^"]*\.xls)"',
        re.IGNORECASE,
    )
    links = []
    for m in pattern.finditer(html):
        href = m.group(1)
        if not href.startswith("http"):
            href = "https://www.gov.br" + href
        # Extract suffix from filename
        fn_m = re.search(r"(\d{6})\.xls", href, re.IGNORECASE)
        suffix = fn_m.group(1) if fn_m else None
        links.append({"url": href, "suffix": suffix})
    return links


def fetch_mapa_cumulative_cs(
    safra_slug: str = "2026-2027",
) -> list:
    """
    Download all available MAPA XLS for the given safra, parse CS totals,
    de-accumulate to get net per quinzena.

    Returns list of dicts ordered by quinzena:
      {position_date, cum_cane_mt, cum_sugar_mt, net_cane_mt, net_sugar_mt, q_num}
    """
    links = scrape_mapa_page(safra_slug)
    if not links:
        logger.warning("mapa: no links found for safra %s", safra_slug)
        return []

    records = []
    for lnk in links:
        raw = _fetch_bytes(lnk["url"])
        if not raw:
            continue
        parsed = _parse_xls_cs_total(raw)
        if parsed and parsed.get("period_final_date"):
            records.append({**parsed, "url": lnk["url"]})

    if not records:
        return []

    # Sort by period date
    records.sort(key=lambda x: x["period_final_date"])

    # De-accumulate
    result = []
    prev_cane = prev_sugar = 0.0
    for qi, rec in enumerate(records):
        net_cane  = round(rec["cum_cane_mt"]  - prev_cane,  4)
        net_sugar = round(rec["cum_sugar_mt"] - prev_sugar, 5)
        result.append({
            "q_num":         qi + 1,
            "position_date": rec["period_final_date"],
            "cum_cane_mt":   rec["cum_cane_mt"],
            "cum_sugar_mt":  rec["cum_sugar_mt"],
            "net_cane_mt":   net_cane,
            "net_sugar_mt":  net_sugar,
        })
        prev_cane  = rec["cum_cane_mt"]
        prev_sugar = rec["cum_sugar_mt"]

    return result


def save_mapa_to_db(session, safra: str, safra_slug: str) -> int:
    """
    Fetch MAPA data and upsert into mapa_estimates table.
    Returns number of rows inserted/updated.
    """
    from sqlalchemy import text

    records = fetch_mapa_cumulative_cs(safra_slug)
    if not records:
        return 0

    count = 0
    for rec in records:
        try:
            session.execute(text("""
                INSERT INTO mapa_estimates
                    (safra, position_date, q_num, cum_cane_mt, cum_sugar_mt,
                     net_cane_mt, net_sugar_mt)
                VALUES
                    (:safra, :pos, :q, :cc, :cs, :nc, :ns)
                ON CONFLICT (safra, position_date) DO UPDATE SET
                    q_num         = EXCLUDED.q_num,
                    cum_cane_mt   = EXCLUDED.cum_cane_mt,
                    cum_sugar_mt  = EXCLUDED.cum_sugar_mt,
                    net_cane_mt   = EXCLUDED.net_cane_mt,
                    net_sugar_mt  = EXCLUDED.net_sugar_mt,
                    fetched_at    = NOW()
            """), {
                "safra": safra,
                "pos":   rec["position_date"],
                "q":     rec["q_num"],
                "cc":    rec["cum_cane_mt"],
                "cs":    rec["cum_sugar_mt"],
                "nc":    rec["net_cane_mt"],
                "ns":    rec["net_sugar_mt"],
            })
            count += 1
        except Exception as exc:
            logger.error("mapa save_to_db row %s: %s", rec.get("position_date"), exc)

    try:
        session.commit()
    except Exception as exc:
        logger.error("mapa save_to_db commit: %s", exc)
        session.rollback()

    return count
