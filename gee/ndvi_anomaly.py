"""
NDVI Temporal Anomaly Benchmark — vs CONAB agency reports.

Computes calendar-aware climatology baseline (5-year median per calendar month)
and compares current month NDVI against it. The resulting anomaly passes through
robust_stats (services/stats_utils) for distribution-free signal classification.

Method documented in BUSINESS_LOGIC.md §7.5 (Temporal Anomaly Comparative Method).
Data quality invariants in BUSINESS_LOGIC.md §3.6 (NDVI anomaly range bounds).

Public API:
  compute_ndvi_anomaly(country, region, ...)         -> NdviAnomalyResult
  benchmark_vs_conab_report(country, region, ...)    -> MarketBenchmark

Phase 1 scope:
  - Brazil only (5 sugarcane states via FAO/GAUL/2015/level1)
  - Bootstrap of 24-month historical anomalies handled by separate script
    (scripts/bootstrap_ndvi_anomaly_history.py, to be created)
  - Module is invokable but NOT wired into daily_pipeline yet (decided after
    shadow test results, BUSINESS_LOGIC.md §7.5 sequencing)

Architecture Contract compliance:
  - Result dataclasses are frozen (Principle 2: Inmutabilidad)
  - Public APIs raise DataQualityError on invariant violations (Principle 3: Data Quality Gates)
  - Audit logging follows BUSINESS_LOGIC.md §5 (Principle 5: Logging Standards)
  - Module is decoupled from authentication: ee.Initialize() is the caller's responsibility
"""
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import ee

from services.data_quality import DataQualityError
from services.stats_utils import robust_stats

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# Cache layout — BUSINESS_LOGIC.md §7.5
# ════════════════════════════════════════════════════════════════════════════
CACHE_ROOT          = Path("data/gee_cache")
BASELINE_CACHE_DIR  = CACHE_ROOT / "ndvi_baseline"
CURRENT_CACHE_DIR   = CACHE_ROOT / "ndvi_current"
HISTORY_CACHE_DIR   = CACHE_ROOT / "anomaly_history"

BASELINE_TTL_DAYS   = 180    # 6 months — climatology rotates ~once per season
CURRENT_TTL_DAYS    = 7      # Sentinel-2 revisits ~5 days
CACHE_SCHEMA_VERSION = 1     # bump if cache file format changes


# ════════════════════════════════════════════════════════════════════════════
# Coverage gates (tiered) — BUSINESS_LOGIC.md §7.5
# ════════════════════════════════════════════════════════════════════════════
COVERAGE_CRITICAL_THRESHOLD = 0.30   # < 0.30 → raise DataQualityError
COVERAGE_WARNING_THRESHOLD  = 0.50   # 0.30-0.50 → logger.warning, signal flagged
# coverage >= 0.50 → logger.info, no gate

MIN_HISTORICAL_COVERAGE     = 0.80   # < 0.80 of 60 months → raise via validate_count
MIN_PIXEL_COV_PER_MONTH     = 0.30   # month counts as "valid" if pixel coverage >= this


# ════════════════════════════════════════════════════════════════════════════
# Climatology + anomaly invariants — BUSINESS_LOGIC.md §3.6
# ════════════════════════════════════════════════════════════════════════════
BASELINE_YEARS_WINDOW = 5                  # 5-year climatology
ANOMALY_VALID_RANGE   = (-0.5, 0.5)        # range cap for anomaly value
NDVI_VALID_RANGE      = (-1.0, 1.0)        # physical definition

# Number of historical anomalies in rolling window used by robust_stats.
# 24 months covers 2 full seasons + buffer for early-stage signal stability.
HISTORY_WINDOW_MONTHS = 24


# ════════════════════════════════════════════════════════════════════════════
# Market signal classification thresholds — BUSINESS_LOGIC.md §7.5
# ════════════════════════════════════════════════════════════════════════════
PERCENTILE_HIGH = 70.0
PERCENTILE_LOW  = 30.0

VALID_CONAB_DIRECTIONS = ("RECOVERY", "DETERIORATION", "STABLE")


# ════════════════════════════════════════════════════════════════════════════
# CONAB region mapping — Brazil sugarcane states via FAO/GAUL/2015/level1
#
# gaul_admin1_id values to be resolved during Step C (requires GEE auth context).
# Region IDs follow ISO-style: BR_<state_postal_code>.
# ════════════════════════════════════════════════════════════════════════════
CONAB_REGIONS: dict[str, dict] = {
    "BR_SP": {"name": "São Paulo",          "gaul_admin1_id": None},
    "BR_GO": {"name": "Goiás",              "gaul_admin1_id": None},
    "BR_MG": {"name": "Minas Gerais",       "gaul_admin1_id": None},
    "BR_MS": {"name": "Mato Grosso do Sul", "gaul_admin1_id": None},
    "BR_PR": {"name": "Paraná",             "gaul_admin1_id": None},
}


# ════════════════════════════════════════════════════════════════════════════
# Result dataclasses (frozen=True — Contract Principle 2: Inmutabilidad)
#
# Note on warnings field: tuple, not list, to enforce true immutability.
# Producer pattern: accumulate in list, freeze with tuple(...) at construction.
# ════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class NdviAnomalyResult:
    """
    Resultado inmutable del cómputo de anomalía NDVI vs climatología 5yr.

    Campos:
      country, region          : identificadores (region ∈ CONAB_REGIONS)
      year, month              : período medido
      anomaly_value            : NDVI_current − NDVI_baseline (típicamente [-0.3, +0.3])
      current_ndvi             : NDVI medio mes actual sobre la región
      baseline_ndvi            : NDVI medio baseline (5yr median, mismo mes calendario)
      pixel_coverage_pct       : % píxeles válidos en mes actual (post cloud-mask)
      historical_coverage_pct  : % meses históricos con cobertura suficiente (de 60)
      percentile_rank          : robust_stats percentile rank (None si insuficientes datos)
      modified_z               : robust_stats MAD-based Z-score (None si insuficientes)
      conviction               : "HIGH" | "MEDIUM" | "LOW" | "INSUFFICIENT_DATA"
      is_extreme_high          : robust_stats AND gate (pct>80 AND mZ>2)
      is_extreme_low           : robust_stats AND gate (pct<20 AND mZ<-2)
      n_historical_obs         : tamaño efectivo de la ventana de history usada
      computed_at              : timestamp del cómputo
      warnings                 : mensajes no-críticos acumulados (tuple inmutable)
    """
    country: str
    region: str
    year: int
    month: int
    anomaly_value: float
    current_ndvi: float
    baseline_ndvi: float
    pixel_coverage_pct: float
    historical_coverage_pct: float
    percentile_rank: Optional[float]
    modified_z: Optional[float]
    conviction: str
    is_extreme_high: bool
    is_extreme_low: bool
    n_historical_obs: int
    computed_at: datetime
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarketBenchmark:
    """
    Comparación inmutable de anomalía NDVI vs reporte direccional CONAB.

    Generated by benchmark_vs_conab_report().

    Campos:
      anomaly_result    : NdviAnomalyResult subyacente
      conab_direction   : "RECOVERY" | "DETERIORATION" | "STABLE"
      conab_inferred    : True si se infirió de DB, False si caller la pasó explícita
      market_signal     : "CONFIRMATION" | "DIVERGENCE_BEARISH" | "DIVERGENCE_BULLISH" | "NEUTRAL"
      audit_log_lines   : strings formateados estilo "Benchmark GEE vs CONAB - ..."
    """
    anomaly_result: NdviAnomalyResult
    conab_direction: str
    conab_inferred: bool
    market_signal: str
    audit_log_lines: tuple[str, ...]


# ════════════════════════════════════════════════════════════════════════════
# Cache path builders — pure functions, no I/O. Safe to implement in skeleton.
# ════════════════════════════════════════════════════════════════════════════

def _baseline_cache_path(country: str, region: str, month: int,
                         year_start: int, year_end: int) -> Path:
    """
    Path al archivo de caché de baseline.

    Pure function: construye Path, no toca disco. Schema versionado para invalidación
    automática si cambiamos el formato del JSON (ver CACHE_SCHEMA_VERSION).
    """
    fname = (
        f"{country}_{region}_m{month:02d}"
        f"_{year_start}-{year_end}_v{CACHE_SCHEMA_VERSION}.json"
    )
    return BASELINE_CACHE_DIR / fname


def _current_cache_path(country: str, region: str, year: int, month: int) -> Path:
    """Path al cache file de NDVI mes actual. Pure function."""
    fname = f"{country}_{region}_{year}_m{month:02d}_v{CACHE_SCHEMA_VERSION}.json"
    return CURRENT_CACHE_DIR / fname


def _history_cache_path(country: str, region: str) -> Path:
    """Path al JSON append-only de history de anomalías. Pure function."""
    fname = f"anomaly_history_{country}_{region}_v{CACHE_SCHEMA_VERSION}.json"
    return HISTORY_CACHE_DIR / fname


# ════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ════════════════════════════════════════════════════════════════════════════

def _ee_call_with_retry(
    callable_fn: Callable,
    *,
    max_retries: int = 3,
    base_delay: float = 1.5,
):
    """
    Wrapper de retry con backoff exponencial para llamadas a Earth Engine que
    pueden fallar de forma transient (429 rate limit, 5xx timeouts bajo carga).

    Usage:
      result = _ee_call_with_retry(lambda: reduced.getInfo())

    Raises:
      DataQualityError(field="gee_call"): tras max_retries fallos consecutivos.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            return callable_fn()
        except ee.EEException as e:
            last_exc = e
            if attempt == max_retries - 1:
                break
            wait = base_delay * (2 ** attempt)
            logger.warning(
                "GEE call failed (attempt %d/%d): %s — retrying in %.1fs",
                attempt + 1, max_retries, e, wait,
            )
            time.sleep(wait)
    raise DataQualityError(
        f"GEE call failed after {max_retries} retries",
        source="gee_ndvi_anomaly", field="gee_call",
        value=str(last_exc), expected="successful GEE response",
    )


def _build_region_geometry(country: str, region: str) -> "ee.Geometry":
    """
    Construye ee.Geometry para la región usando FAO/GAUL/2015/level1.

    Raises:
      KeyError: si region no existe en CONAB_REGIONS
               (mensaje incluye lista de regiones válidas).
    """
    if region not in CONAB_REGIONS:
        valid = sorted(CONAB_REGIONS.keys())
        raise KeyError(
            f"region {region!r} not in CONAB_REGIONS. Valid: {valid}"
        )
    state_name = CONAB_REGIONS[region]["name"]
    gaul = ee.FeatureCollection("FAO/GAUL/2015/level1")
    feature = gaul.filter(
        ee.Filter.And(
            ee.Filter.eq("ADM0_NAME", "Brazil"),
            ee.Filter.eq("ADM1_NAME", state_name),
        )
    ).first()
    return feature.geometry()


def _add_ndvi_band(image: "ee.Image") -> "ee.Image":
    """
    Añade banda NDVI a imagen S2 + máscara nubes vía SCL.

    NDVI = (B8 - B4) / (B8 + B4), donde B8=NIR, B4=Red.

    SCL band classes (Sentinel-2 L2A):
      0=no_data, 1=saturated, 2=dark, 3=cloud_shadow, 4=veg, 5=bare, 6=water,
      7=unclassified, 8=cloud_medium, 9=cloud_high, 10=cirrus, 11=snow_ice
    Mask out (invalid for NDVI computation): {0, 1, 3, 8, 9, 10, 11}.
    """
    ndvi = image.normalizedDifference(["B8", "B4"]).rename("NDVI")
    scl = image.select("SCL")
    bad = scl.eq(0).Or(scl.eq(1)).Or(scl.eq(3)).Or(scl.gte(8))
    return image.addBands(ndvi).updateMask(bad.Not())


def _month_climatology_ee(geometry: "ee.Geometry", month: int,
                          year_start: int, year_end: int) -> "ee.Image":
    """
    Climatología calendar-aware (server-side):
      mediana de todos los píxeles NDVI para el mes calendario `month`
      a lo largo del rango [year_start, year_end].
    """
    coll = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(geometry)
        .filterDate(f"{year_start}-01-01", f"{year_end}-12-31")
        .filter(ee.Filter.calendarRange(month, month, "month"))
        .map(_add_ndvi_band)
        .select("NDVI")
    )
    return coll.median()


def _current_month_ndvi_ee(geometry: "ee.Geometry", year: int, month: int) -> "ee.Image":
    """NDVI medio del mes calendario para year-month."""
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1
    coll = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(geometry)
        .filterDate(f"{year}-{month:02d}-01", f"{next_year}-{next_month:02d}-01")
        .map(_add_ndvi_band)
        .select("NDVI")
    )
    return coll.median()


def _reduce_to_region_mean(image: "ee.Image", geometry: "ee.Geometry",
                           band: str = "NDVI") -> dict:
    """
    Server-side reduceRegion para extraer mean NDVI + coverage metrics.

    Coverage = valid_pixel_count / total_pixel_count.
    valid_pixel_count viene del count del band ya mascarado por _add_ndvi_band.
    total_pixel_count viene de count sobre ee.Image.constant(1) en la misma geometry.

    Returns dict con keys:
      mean, pixel_count, valid_pixel_count, pixel_coverage_pct.

    Raises:
      DataQualityError(field="gee_call"): si Earth Engine falla tras retries.
    """
    # Reduce 1: mean + count del band ya mascarado (píxeles válidos)
    reduced_dict = image.reduceRegion(
        reducer=ee.Reducer.mean().combine(ee.Reducer.count(), sharedInputs=True),
        geometry=geometry,
        scale=100,
        maxPixels=int(1e10),
        bestEffort=True,
    )
    reduced = _ee_call_with_retry(lambda: reduced_dict.getInfo())

    # Reduce 2: total de píxeles en la geometry (Image.constant unmasked)
    total_dict = ee.Image.constant(1).reduceRegion(
        reducer=ee.Reducer.count(),
        geometry=geometry,
        scale=100,
        maxPixels=int(1e10),
        bestEffort=True,
    )
    total_info = _ee_call_with_retry(lambda: total_dict.getInfo())

    mean_value   = reduced.get(f"{band}_mean") if isinstance(reduced, dict) else None
    valid_pixels = reduced.get(f"{band}_count", 0) if isinstance(reduced, dict) else 0
    total_pixels = total_info.get("constant", 0) if isinstance(total_info, dict) else 0

    coverage_pct = (valid_pixels / total_pixels) if total_pixels > 0 else 0.0

    return {
        "mean": mean_value,
        "valid_pixel_count": int(valid_pixels),
        "pixel_count": int(total_pixels),
        "pixel_coverage_pct": coverage_pct,
    }


def _validate_coverage_gate(coverage_pct: float, *,
                            source: str, field: str) -> None:
    """
    Tiered coverage gate (BUSINESS_LOGIC.md §7.5):

      coverage < 0.30                 → raise DataQualityError (critical)
      0.30 <= coverage < 0.50         → logger.warning, señal flagged como degraded
      coverage >= 0.50                → logger.info, gate pasa silencioso

    Args:
      coverage_pct : fracción [0, 1]
      source, field: contexto de logging y error

    Raises:
      DataQualityError(field=field): si coverage_pct < COVERAGE_CRITICAL_THRESHOLD.
    """
    if coverage_pct < COVERAGE_CRITICAL_THRESHOLD:
        raise DataQualityError(
            f"pixel coverage critically low ({coverage_pct:.1%}) — signal suppressed",
            source=source, field=field,
            value=round(coverage_pct, 4),
            expected=f">= {COVERAGE_CRITICAL_THRESHOLD:.0%}",
        )
    if coverage_pct < COVERAGE_WARNING_THRESHOLD:
        logger.warning(
            "%s.%s coverage degraded (%.1f%%) — signal computed but flagged",
            source, field, coverage_pct * 100,
        )
        return
    logger.info(
        "%s.%s coverage OK (%.1f%%)",
        source, field, coverage_pct * 100,
    )


def _load_cache(path: Path, ttl_days: int) -> Optional[dict]:
    """
    Lee JSON cache. Returns None si:
      - archivo no existe
      - JSON corrupto (loguea WARNING)
      - computed_at más antiguo que ttl_days
      - cache_version distinto a CACHE_SCHEMA_VERSION
    """
    if not path.exists():
        logger.debug("cache MISS (not found): %s", path.name)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        logger.warning("cache corrupt, treating as miss: %s (%s)", path.name, e)
        return None
    if data.get("cache_version") != CACHE_SCHEMA_VERSION:
        logger.debug(
            "cache MISS (schema v%s != v%s): %s",
            data.get("cache_version"), CACHE_SCHEMA_VERSION, path.name,
        )
        return None
    computed_at_str = data.get("computed_at")
    if not computed_at_str:
        logger.warning("cache missing computed_at: %s", path.name)
        return None
    try:
        computed_at = datetime.fromisoformat(computed_at_str)
    except ValueError:
        logger.warning("cache invalid computed_at: %s — %s", computed_at_str, path.name)
        return None
    age_days = (datetime.now() - computed_at).days
    if age_days > ttl_days:
        logger.debug(
            "cache MISS (stale %dd > ttl %dd): %s",
            age_days, ttl_days, path.name,
        )
        return None
    logger.debug("cache HIT (age %dd): %s", age_days, path.name)
    return data


def _save_cache(path: Path, data: dict) -> None:
    """Atomic write: write a .tmp y rename. Crea directorios padre si no existen."""
    enriched = {
        **data,
        "cache_version": CACHE_SCHEMA_VERSION,
        "computed_at": data.get("computed_at") or datetime.now().isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(enriched, indent=2, default=str), encoding="utf-8")
    tmp_path.replace(path)
    logger.debug("cache SAVED: %s", path.name)


def _load_anomaly_history(country: str, region: str) -> list[float]:
    """
    Lee history append-only para esta región.
    Retorna lista ordenada cronológicamente (antiguo → reciente), truncada a
    las últimas HISTORY_WINDOW_MONTHS observaciones (rolling window §7.5.4).
    Lista vacía si no hay history (primera corrida sin bootstrap).
    """
    path = _history_cache_path(country, region)
    if not path.exists():
        logger.debug("history empty for %s_%s (no file)", country, region)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        logger.warning(
            "history corrupt, treating as empty: %s (%s)", path.name, e,
        )
        return []
    entries = data.get("anomalies", [])
    # Ordenar cronológicamente (oldest → newest) y truncar a ventana deslizante
    sorted_entries = sorted(
        entries, key=lambda e: (e.get("year", 0), e.get("month", 0)),
    )
    window = sorted_entries[-HISTORY_WINDOW_MONTHS:]
    anomalies = [
        float(e["anomaly"]) for e in window if e.get("anomaly") is not None
    ]
    logger.debug(
        "history loaded for %s_%s: %d entries (window=%d)",
        country, region, len(anomalies), HISTORY_WINDOW_MONTHS,
    )
    return anomalies


def _append_anomaly_to_history(country: str, region: str,
                                year: int, month: int,
                                anomaly: float, coverage: float) -> None:
    """
    Append entry al JSON history. Deduplica por (year, month) — re-cómputo sobrescribe.
    """
    path = _history_cache_path(country, region)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            logger.warning(
                "history corrupt during append, reinitializing: %s (%s)",
                path.name, e,
            )
            data = None
    else:
        data = None
    if data is None:
        data = {
            "country": country, "region": region,
            "cache_version": CACHE_SCHEMA_VERSION, "anomalies": [],
        }
    # Dedup by (year, month) — la nueva entry reemplaza
    filtered = [
        e for e in data.get("anomalies", [])
        if not (e.get("year") == year and e.get("month") == month)
    ]
    filtered.append({
        "year": year,
        "month": month,
        "anomaly": round(float(anomaly), 4),
        "coverage": round(float(coverage), 3),
        "appended_at": datetime.now().isoformat(),
    })
    data["anomalies"] = filtered
    data["cache_version"] = CACHE_SCHEMA_VERSION
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp_path.replace(path)
    logger.debug(
        "history APPEND %s_%s y=%d m=%d a=%.4f cov=%.2f",
        country, region, year, month, anomaly, coverage,
    )


def _compute_baseline(country: str, region: str, month: int) -> dict:
    """
    Orquesta cache lookup → cómputo GEE (si miss) → save cache.

    Returns dict con baseline_ndvi + historical_coverage_pct + metadata.
    """
    raise NotImplementedError("Step C — baseline orchestration")


def _compute_current(country: str, region: str, year: int, month: int) -> dict:
    """
    Orquesta cache → cómputo GEE (si miss) → save cache.

    Returns dict con current_ndvi + pixel_coverage_pct + metadata.
    """
    raise NotImplementedError("Step C — current month orchestration")


def _infer_conab_direction(country: str, region: str) -> tuple[str, bool]:
    """
    Lee los 2 últimos levantamentos de conab_cana_levantamento para esta región
    y deduce dirección (RECOVERY/DETERIORATION/STABLE) por delta de producción
    o área.

    Returns: (direction, inferred=True).

    Raises:
      DataQualityError(field="conab_inference"):
        - Si hay menos de 2 levantamentos disponibles para la región
        - Si el último levantamento es demasiado antiguo (>130 días — ver §4 freshness)
    """
    raise NotImplementedError("Step C — CONAB direction inference from DB")


def _classify_market_signal(percentile: Optional[float],
                            conab_direction: str) -> str:
    """
    Clasifica market_signal según matriz BUSINESS_LOGIC.md §7.5:

      P >= 70 AND direction = RECOVERY        → CONFIRMATION
      P <= 30 AND direction = DETERIORATION   → CONFIRMATION (caída confirmada)
      P <= 30 AND direction = RECOVERY        → DIVERGENCE_BEARISH
      P >= 70 AND direction = DETERIORATION   → DIVERGENCE_BULLISH
      otherwise (incluye STABLE o percentile=None) → NEUTRAL
    """
    raise NotImplementedError("Step D — market signal classification")


# ════════════════════════════════════════════════════════════════════════════
# Public API
# ════════════════════════════════════════════════════════════════════════════

def compute_ndvi_anomaly(
    country: str,
    region: str,
    *,
    year: Optional[int] = None,
    month: Optional[int] = None,
    use_cache: bool = True,
) -> NdviAnomalyResult:
    """
    Cómputo principal: anomalía NDVI vs climatología 5yr para una región.

    Pipeline interno:
      1. Resolver geometry (GAUL admin level 1)
      2. Cargar/computar baseline 5yr (cache TTL 180d)
      3. Cargar/computar NDVI del mes objetivo (cache TTL 7d)
      4. Aplicar coverage gate per-pixel (tiered: 30/50)
      5. Aplicar historical coverage gate (80% de 60 meses)
      6. anomaly = current_ndvi − baseline_ndvi
      7. Validar anomaly_value contra ANOMALY_VALID_RANGE
      8. Cargar history → robust_stats → percentile + modified_z + conviction
      9. Append anomaly al history JSON
     10. Loguear audit (BUSINESS_LOGIC.md §5)

    Args:
      country     : código país (Phase 1: "BR")
      region      : ID de region en CONAB_REGIONS (ej. "BR_SP")
      year        : año del mes a medir. Default: año actual
      month       : mes calendario [1, 12]. Default: mes anterior completo
      use_cache   : si False, fuerza recómputo GEE (bypass cache)

    Returns:
      NdviAnomalyResult (frozen) con anomaly + stats robustas + metadata.

    Raises:
      KeyError                                          : region no existe
      DataQualityError(field="pixel_coverage_pct")      : cobertura mes actual < 30%
      DataQualityError(field="historical_coverage_pct") : baseline tiene < 80% meses válidos
      DataQualityError(field="anomaly_value")           : anomaly fuera de [-0.5, +0.5]
                                                          (típico: bug GEE / región mal definida)
      DataQualityError(field="gee_call")                : fallo en request a Earth Engine
    """
    raise NotImplementedError("Step D — main compute_ndvi_anomaly")


def benchmark_vs_conab_report(
    country: str,
    region: str,
    *,
    conab_direction: Optional[str] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
) -> MarketBenchmark:
    """
    Compara anomalía NDVI con reporte direccional CONAB y genera audit log.

    Si conab_direction es None, se infiere automáticamente desde la tabla
    conab_cana_levantamento (delta entre los 2 últimos levantamentos).

    Audit log generado (formato BUSINESS_LOGIC.md §5):
      INFO  Benchmark GEE vs CONAB - Region: [X] - Percentile: [Y] - mZ: [Z]
      INFO  -> Market Confirmation: <descripción>
      OR
      WARNING -> Market DIVERGENCE: <descripción>

    Args:
      country         : código país
      region          : ID region en CONAB_REGIONS
      conab_direction : si None, infiere de DB. Si pasa explícita, debe estar en
                        VALID_CONAB_DIRECTIONS (RECOVERY / DETERIORATION / STABLE)
      year, month     : como en compute_ndvi_anomaly

    Returns:
      MarketBenchmark (frozen) con anomaly_result + market_signal + audit log.

    Raises:
      ValueError                                : conab_direction inválido (no en VALID_CONAB_DIRECTIONS)
      DataQualityError(field="conab_inference") : direction=None y no se pudo inferir
      Todas las excepciones de compute_ndvi_anomaly (propagan transparentemente)
    """
    raise NotImplementedError("Step D — benchmark_vs_conab_report wrapper")
