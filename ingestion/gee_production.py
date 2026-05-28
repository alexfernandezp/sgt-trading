"""
GEE Production — interfaz entre los estimadores GEE y el balance model.

Expone funciones con la misma firma que las fuentes primarias (ISMA, OCSB, CONAB)
para que world_balance_model.py pueda intercambiarlas sin cambios de arquitectura.

Jerarquía de prioridad en adjust_india():
  1. ISMA BD (dato confirmado por el trader)
  2. GEE NDVI integral (este modulo) ← nuevo
  3. USDA + fallback etanol conservador

Jerarquía en adjust_thailand():
  1. OCSB fresco (<= 120d desde cierre temporada)
  2. GEE NDVI integral (este modulo) ← nuevo
  3. USDA sin ajuste

Jerarquía en adjust_brazil():
  1. CONAB fresco
  2. GEE NDVI integral (este modulo) ← cross-check complementario
  3. USDA sin ajuste
"""
import logging
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

# Cache en memoria por sesion (GEE calls son caros — ~30-90s por pais)
_cache: dict[str, dict] = {}


def _get_estimator(country_key: str):
    """Instancia el estimador correcto segun el pais."""
    if country_key == "india":
        from gee.countries.india import SugarcaneEstimator_India
        return SugarcaneEstimator_India()
    elif country_key == "thailand":
        from gee.countries.thailand import SugarcaneEstimator_Thailand
        return SugarcaneEstimator_Thailand()
    elif country_key == "brazil":
        from gee.countries.brazil import SugarcaneEstimator_Brazil
        return SugarcaneEstimator_Brazil()
    else:
        raise ValueError("Pais GEE no soportado: %s" % country_key)


def gee_production_estimate(
    country_key: str,
    season_year: Optional[int] = None,
    use_cache: bool = True,
) -> tuple[Optional[float], str, float]:
    """
    Interfaz generica de estimacion GEE para cualquier pais.

    Returns:
        (estimated_mt, source_str, confidence)
        estimated_mt = None si GEE no disponible o datos insuficientes
    """
    cache_key = "%s_%s" % (country_key, season_year or "auto")

    if use_cache and cache_key in _cache:
        r = _cache[cache_key]
        logger.debug("GEE %s: usando cache de sesion", country_key)
        return r["estimated_mt"], r["source"], r["confidence"]

    try:
        estimator = _get_estimator(country_key)
        result = estimator.run(season_year)
    except Exception as e:
        logger.warning("GEE %s: error en estimador — %s", country_key, e)
        return None, "gee_error", 0.50

    if result.get("error") or result.get("estimated_mt") is None:
        logger.info("GEE %s: sin estimacion — %s", country_key, result.get("error", "?"))
        return None, "gee_unavailable", 0.50

    est_mt = result["estimated_mt"]
    conf   = result["confidence"]
    src    = "gee_ndvi_integral"

    # Penalizar si completitud < 50% (temporada muy temprana)
    comp = result.get("data_completeness_pct", 0)
    if comp < 30:
        logger.info("GEE %s: completitud %.0f%% muy baja — retornando None", country_key, comp)
        return None, "gee_insufficient_data", 0.50
    if comp < 50:
        conf = min(conf, 0.60)
        src  = "gee_ndvi_partial"

    _cache[cache_key] = {"estimated_mt": est_mt, "source": src, "confidence": conf}

    logger.info(
        "GEE %s %s: %.3f Mt  conf=%.2f  comp=%.0f%%",
        country_key, season_year or "auto", est_mt, conf, comp,
    )
    return est_mt, src, conf


# ── Interfaces especificas por pais ───────────────────────────────────────────

def india_gee_estimate(season_year: Optional[int] = None) -> tuple[Optional[float], str, float]:
    """
    Estimacion GEE produccion azucar India.
    Retorna (estimated_mt, source, confidence).
    Dato ya neto de etanol (calibrado vs ISMA que es neto).
    """
    return gee_production_estimate("india", season_year)


def thailand_gee_estimate(season_year: Optional[int] = None) -> tuple[Optional[float], str, float]:
    """
    Estimacion GEE produccion azucar Tailandia.
    Retorna (estimated_mt, source, confidence).
    """
    return gee_production_estimate("thailand", season_year)


def brazil_gee_estimate(season_year: Optional[int] = None) -> tuple[Optional[float], str, float]:
    """
    Estimacion GEE produccion azucar Brasil Centro-Sur.
    Retorna (estimated_mt, source, confidence).
    """
    return gee_production_estimate("brazil", season_year)


def clear_cache():
    """Limpia el cache de sesion (util para tests o re-ejecuciones forzadas)."""
    global _cache
    _cache = {}
