import logging
from datetime import date
import httpx
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from config import CFTC_API_URL, CFTC_SUGAR_MARKET
from models import CotData

logger = logging.getLogger(__name__)

CFTC_DISAGG_URL = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"

_COL_MAP = {
    "ncomm_positions_long_all":      "ncomm_long",
    "ncomm_positions_short_all":     "ncomm_short",
    "ncomm_postions_spread_all":     "ncomm_spread",
    "comm_positions_long_all_nocit": "comm_long",
    "comm_positions_short_all":      "comm_short",
    "cit_positions_long_all":        "cit_long",
    "cit_positions_short_all":       "cit_short",
    "nonrept_positions_long_all":    "nonrept_long",
    "nonrept_positions_short_all":   "nonrept_short",
    "open_interest_all":             "total_open_interest",
    "change_open_interest_all":      "change_oi",
    "change_noncomm_long_all_nocit": "change_ncomm_long",
    "change_noncomm_short_all":      "change_ncomm_short",
    "change_comm_long_all_nocit":    "change_comm_long",
    "change_comm_short_all_nocit":   "change_comm_short",
    "change_cit_long_all":           "change_cit_long",
    "change_cit_short_all":          "change_cit_short",
}

_DISAGG_COL_MAP = {
    "m_money_positions_long_all":  "mm_long",
    "m_money_positions_short_all": "mm_short",
    "m_money_positions_spread":    "mm_spread",
    "prod_merc_positions_long":    "prodmerc_long",
    "prod_merc_positions_short":   "prodmerc_short",
    "swap_positions_long_all":     "swap_long",
    "swap__positions_short_all":   "swap_short",   # double underscore in CFTC API
    "traders_m_money_long_all":    "traders_mm_long",
    "traders_m_money_short_all":   "traders_mm_short",
    "change_in_m_money_long_all":  "change_mm_long",
    "change_in_m_money_short_all": "change_mm_short",
    "open_interest_all":           "total_oi",
}


def _int(val):
    return int(float(val)) if val is not None else None


def fetch_cot(session: Session, limit: int = 156) -> int:
    """Descarga Legacy COT (non-comms) + Disaggregated (Managed Money) y los fusiona."""

    # Legacy report
    params_legacy = {
        "$where": f"caseless_eq(market_and_exchange_names, '{CFTC_SUGAR_MARKET}')",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": str(limit),
    }
    try:
        resp = httpx.get(CFTC_API_URL, params=params_legacy, timeout=30)
        resp.raise_for_status()
        legacy_records = {r["report_date_as_yyyy_mm_dd"][:10]: r for r in resp.json()}
    except Exception as exc:
        logger.error(f"Error COT legacy: {exc}")
        return 0

    # Disaggregated report
    params_disagg = {
        "$where": f"caseless_eq(market_and_exchange_names, '{CFTC_SUGAR_MARKET}')",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": str(limit),
    }
    disagg_records = {}
    try:
        resp2 = httpx.get(CFTC_DISAGG_URL, params=params_disagg, timeout=30)
        resp2.raise_for_status()
        disagg_records = {r["report_date_as_yyyy_mm_dd"][:10]: r for r in resp2.json()}
    except Exception as exc:
        logger.warning(f"COT disaggregado no disponible: {exc}")

    # Fusionar y construir filas
    rows = []
    for raw_date, rec in legacy_records.items():
        try:
            report_date = date.fromisoformat(raw_date)
        except ValueError:
            continue

        row = {"report_date": report_date, "instrument": "SUGAR_NO11_ICE", "source": "cftc_api"}

        for api_col, model_col in _COL_MAP.items():
            row[model_col] = _int(rec.get(api_col))

        row["ncomm_net"]      = (row.get("ncomm_long")   or 0) - (row.get("ncomm_short")  or 0)
        row["comm_net"]       = (row.get("comm_long")    or 0) - (row.get("comm_short")   or 0)
        row["cit_net"]        = (row.get("cit_long")     or 0) - (row.get("cit_short")    or 0)
        row["speculator_net"] = (
            (row.get("ncomm_long") or 0) + (row.get("nonrept_long")  or 0)
        ) - (
            (row.get("ncomm_short") or 0) + (row.get("nonrept_short") or 0)
        )

        disagg = disagg_records.get(raw_date, {})
        for api_col, model_col in _DISAGG_COL_MAP.items():
            row[model_col] = _int(disagg.get(api_col))

        if row.get("mm_long") is not None and row.get("mm_short") is not None:
            row["mm_net"]       = row["mm_long"] - row["mm_short"]
        if row.get("prodmerc_long") is not None and row.get("prodmerc_short") is not None:
            row["prodmerc_net"] = row["prodmerc_long"] - row["prodmerc_short"]
        if row.get("swap_long") is not None and row.get("swap_short") is not None:
            row["swap_net"]     = row["swap_long"] - row["swap_short"]

        rows.append(row)

    if not rows:
        return 0

    stmt = insert(CotData).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_cot_date_instrument",
        set_={c: getattr(stmt.excluded, c) for c in [
            "mm_long", "mm_short", "mm_spread", "mm_net",
            "prodmerc_long", "prodmerc_short", "prodmerc_net",
            "swap_long", "swap_short", "swap_net",
            "traders_mm_long", "traders_mm_short",
            "change_mm_long", "change_mm_short", "total_oi",
        ]},
    )
    session.execute(stmt)
    session.commit()
    logger.info(f"COT: {len(rows)} registros (legacy + disaggregated)")
    return len(rows)
