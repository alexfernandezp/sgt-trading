"""
calibration_data.py — Bridge between live ingestion and GEE OLS calibration.

Returns gross production series per country for use in CropEstimator.calibrate().
India: GROSS (net ISMA + ethanol diversion added back).
Brazil/Thailand: net sugar production (no ethanol adjustment needed).
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

from ingestion.isma_india import india_full_year_estimate, fetch_isma_latest
from ingestion.india_ethanol import get_gross_production, get_season_diversion, _GROSS_PRODUCTION_SEED

try:
    from ingestion.conab_cana import get_latest_conab as _conab_latest
    _CONAB_AVAILABLE = True
except ImportError:
    _CONAB_AVAILABLE = False

try:
    from ingestion.ocsb import fetch_ocsb_latest as _ocsb_latest
    _OCSB_AVAILABLE = True
except ImportError:
    _OCSB_AVAILABLE = False


# ── Helpers ────────────────────────────────────────────────────────────────────

def _merge(live: dict[int, float], seed: dict) -> dict[int, float]:
    """Live data wins over seed. Normalises all keys to int."""
    result: dict[int, float] = {int(k): float(v) for k, v in seed.items()}
    result.update({int(k): float(v) for k, v in live.items()})
    return result


def _isma_net_for_season(season_year: int, session) -> Optional[float]:
    """
    Reads the most representative net production figure for a given marketing_year
    from the isma_release table.

    Uses estimated_full_year_mt when season_progress_pct > 90 (late/closed season),
    otherwise falls back to cumulative_mt (partial — less reliable but still useful).
    Returns None if no row found.
    """
    try:
        from models.market_data import IsmaRelease
        from sqlalchemy import select

        rows = session.execute(
            select(IsmaRelease)
            .where(IsmaRelease.marketing_year == season_year)
            .order_by(IsmaRelease.data_date.desc())
            .limit(1)
        ).scalar_one_or_none()

        if rows is None:
            return None

        progress = float(rows.season_progress_pct or 0)
        if progress > 90 and rows.estimated_full_year_mt is not None:
            return float(rows.estimated_full_year_mt)
        if rows.cumulative_mt is not None:
            return float(rows.cumulative_mt)
        return None

    except Exception as e:
        logger.debug("IsmaRelease DB query season %d: %s", season_year, e)
        return None


# ── Country series builders ────────────────────────────────────────────────────

def _get_india_series(seasons: list[int], session=None) -> dict[int, float]:
    """
    Returns {season_year: gross_mt} for India.
    Priority per season:
      1. DB (IsmaRelease net) + diversion add-back → gross
      2. _GROSS_PRODUCTION_SEED (already gross)
    """
    result: dict[int, float] = {}

    for sy in seasons:
        if session is not None:
            net_mt = _isma_net_for_season(sy, session)
            if net_mt is not None:
                gross = get_gross_production(sy, net_mt, session)
                logger.debug("India %d: DB net=%.3f Mt → gross=%.3f Mt", sy, net_mt, gross)
                result[sy] = gross
                continue

        if sy in _GROSS_PRODUCTION_SEED:
            result[sy] = _GROSS_PRODUCTION_SEED[sy]
            logger.debug("India %d: seed gross=%.3f Mt", sy, _GROSS_PRODUCTION_SEED[sy])

    return result


def _get_brazil_series(seasons: list[int], session=None) -> dict[int, float]:
    """
    Returns {season_year: sugar_mt} for Brazil using CONAB data.
    CONAB reports sugar production separately — no ethanol adjustment needed.
    Falls back to empty dict silently.

    ConabCanaLevantamento.season is "YYYY/YY" — season_year is the start year.
    """
    if not _CONAB_AVAILABLE or session is None:
        return {}

    result: dict[int, float] = {}

    try:
        from models.market_data import ConabCanaLevantamento
        from sqlalchemy import select

        for sy in seasons:
            season_str = "%d/%s" % (sy, str(sy + 1)[2:])
            row = session.execute(
                select(ConabCanaLevantamento)
                .where(ConabCanaLevantamento.season == season_str)
                .order_by(ConabCanaLevantamento.levantamento.desc())
                .limit(1)
            ).scalar_one_or_none()

            if row is not None and row.sugar_total_mt is not None:
                result[sy] = float(row.sugar_total_mt)
                logger.debug("Brazil %d: CONAB sugar=%.3f Mt", sy, result[sy])

    except Exception as e:
        logger.debug("Brazil CONAB series: %s", e)

    return result


def _get_thailand_series(seasons: list[int], session=None) -> dict[int, float]:
    """
    Returns {season_year: sugar_mt} for Thailand using OCSB data.
    Falls back to empty dict silently.
    """
    if not _OCSB_AVAILABLE:
        return {}

    result: dict[int, float] = {}

    try:
        for sy in seasons:
            ocsb = _ocsb_latest(target_ce_year=sy)
            if ocsb is not None and ocsb.season_year == sy:
                result[sy] = ocsb.total_production_mt
                logger.debug("Thailand %d: OCSB sugar=%.3f Mt", sy, ocsb.total_production_mt)
    except Exception as e:
        logger.debug("Thailand OCSB series: %s", e)

    return result


# ── Main entry point ───────────────────────────────────────────────────────────

def get_calibration_series(
    country_key: str,
    seasons: list[int],
    config: dict,
    session=None,
) -> dict[int, float]:
    """
    Returns {season_year: production_mt} for GEE OLS calibration.

    Production is GROSS for India (pre-ethanol diversion), net for others.
    Live DB data takes priority; _calibration_seed fills gaps.

    Args:
        country_key:  "india" | "brazil" | "thailand" | other
        seasons:      list of season start years to retrieve
        config:       country config dict from gee_countries.yaml
        session:      SQLAlchemy session (optional — enables DB lookups)
    """
    seed_fallback = config.get(
        "_calibration_seed",
        config.get("known_production_mt", {}),
    )

    if country_key == "india":
        seed = config.get("_calibration_seed", {})
        live = _get_india_series(seasons, session)
    elif country_key == "brazil":
        seed = seed_fallback
        live = _get_brazil_series(seasons, session)
    elif country_key == "thailand":
        seed = seed_fallback
        live = _get_thailand_series(seasons, session)
    else:
        return {int(k): float(v) for k, v in seed_fallback.items()}

    merged = _merge(live, seed)

    n_live = sum(1 for sy in seasons if sy in live)
    n_seed = sum(1 for sy in seasons if sy not in live and sy in {int(k) for k in seed})
    logger.info(
        "calibration_data [%s]: %d seasons from live DB, %d from seed fallback",
        country_key, n_live, n_seed,
    )

    return merged
