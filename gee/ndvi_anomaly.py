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

# reduceRegion config para reducciones a nivel estado (BUSINESS_LOGIC §7.5.4):
#   scale=250m: estándar MODIS para NDVI estatal. SP=~248k km² @100m son 25M
#     pixels (excede GEE memory); @250m son ~4M (cómodo). Granularidad apta para
#     agregación estado (campos típicos caña: 50-500 ha = 500m-2km).
#   tileScale=4: divide reducción en sub-tiles → memory headroom server-side.
REDUCE_SCALE_METERS = 250
REDUCE_TILE_SCALE   = 4
REDUCE_MAX_PIXELS   = int(1e10)

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
# Convención GAUL verificada 2026-06-01 (inspección scripts/_inspect_gaul_brazil.py):
#   - ADM0_NAME = "Brazil" (literal, no "Brasil")
#   - ADM1_NAME = sin acentos + Title Case completo (ej. "Mato Grosso Do Sul" con
#     'Do' capitalizado, no 'do'; "Sao Paulo" sin tilde; "Goias" sin tilde)
#
# `display_name` es la forma legible humana para logs/UI; `gaul_name` es el
# string EXACTO con el que GAUL guarda el estado (case + accents sensitive).
# ════════════════════════════════════════════════════════════════════════════
CONAB_REGIONS: dict[str, dict] = {
    "BR_SP": {"display_name": "São Paulo",          "gaul_name": "Sao Paulo"},
    "BR_GO": {"display_name": "Goiás",              "gaul_name": "Goias"},
    "BR_MG": {"display_name": "Minas Gerais",       "gaul_name": "Minas Gerais"},
    "BR_MS": {"display_name": "Mato Grosso do Sul", "gaul_name": "Mato Grosso Do Sul"},
    "BR_PR": {"display_name": "Paraná",             "gaul_name": "Parana"},
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

def _atomic_replace(src: Path, dst: Path, max_retries: int = 5) -> None:
    """
    Path.replace() con retry sobre PermissionError transient.

    Windows puede dar WinError 5 (Access Denied) si antivirus/indexer toca el
    archivo target en el momento del rename. La operación es por naturaleza
    atomic; el retry absorbe la flap.
    """
    for attempt in range(max_retries):
        try:
            src.replace(dst)
            return
        except PermissionError:
            if attempt == max_retries - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


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
    gaul_name = CONAB_REGIONS[region]["gaul_name"]
    gaul = ee.FeatureCollection("FAO/GAUL/2015/level1")
    feature = gaul.filter(
        ee.Filter.And(
            ee.Filter.eq("ADM0_NAME", "Brazil"),
            ee.Filter.eq("ADM1_NAME", gaul_name),
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
        scale=REDUCE_SCALE_METERS,
        maxPixels=REDUCE_MAX_PIXELS,
        bestEffort=True,
        tileScale=REDUCE_TILE_SCALE,
    )
    reduced = _ee_call_with_retry(lambda: reduced_dict.getInfo())

    # Reduce 2: total de píxeles en la geometry (Image.constant unmasked)
    total_dict = ee.Image.constant(1).reduceRegion(
        reducer=ee.Reducer.count(),
        geometry=geometry,
        scale=REDUCE_SCALE_METERS,
        maxPixels=REDUCE_MAX_PIXELS,
        bestEffort=True,
        tileScale=REDUCE_TILE_SCALE,
    )
    total_info = _ee_call_with_retry(lambda: total_dict.getInfo())

    mean_value   = reduced.get(f"{band}_mean") if isinstance(reduced, dict) else None
    valid_pixels = reduced.get(f"{band}_count", 0) if isinstance(reduced, dict) else 0
    total_pixels = total_info.get("constant", 0) if isinstance(total_info, dict) else 0

    raw_coverage = (valid_pixels / total_pixels) if total_pixels > 0 else 0.0
    # Clamp [0, 1]: dos reducciones independientes (NDVI image vs constant) pueden
    # diferir ligeramente por proyección/grid alignment con bestEffort=True. La
    # discrepancia típica es <1% y semánticamente "cobertura completa" — clampear
    # es correcto. Log DEBUG si excede para detectar drift estructural mayor.
    coverage_pct = max(0.0, min(1.0, raw_coverage))
    if raw_coverage > 1.005:
        logger.debug(
            "coverage clamp: raw=%.4f -> 1.0 (projection grid mismatch valid=%d total=%d)",
            raw_coverage, valid_pixels, total_pixels,
        )

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
    _atomic_replace(tmp_path, path)
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
    _atomic_replace(tmp_path, path)
    logger.debug(
        "history APPEND %s_%s y=%d m=%d a=%.4f cov=%.2f",
        country, region, year, month, anomaly, coverage,
    )


def _compute_baseline(country: str, region: str, month: int,
                     target_year: Optional[int] = None) -> dict:
    """
    Orquesta cache lookup → cómputo GEE (si miss) → save cache.

    Args:
      target_year: año contra el cual se mide el anomaly. El baseline = los 5 años
                  PREVIOS a target_year (excluyente). Default: año actual.

                  CRÍTICO para evitar data leakage en backtesting/bootstrap:
                  si computamos anomaly para year=2024, el baseline debe ser
                  2019-2023 (no 2021-2025). Sin este parámetro, target_year
                  estaría incluido en su propio baseline (bias hacia zero).
                  Ver BUSINESS_LOGIC §7.5.2.

                  Para live production (target = current year), comportamiento
                  idéntico al previo: baseline = current_year-5 a current_year-1.

    Returns dict con baseline_ndvi + historical_coverage_pct + metadata.

    Nota sobre historical_coverage_pct (BUSINESS_LOGIC §7.5.3): se usa el % de
    píxeles válidos en la imagen baseline (mediana 5yr) como proxy del "% de
    meses históricos con cobertura suficiente". Justificación: si el median
    5yr tiene alta cobertura de píxeles, la mayoría de los meses fuente
    aportaron datos. Trade-off: más rápido (1 reduce vs 5), aceptable para
    Fase 1; refinable post shadow test (Step F) si se observan divergencias.
    """
    anchor_year = target_year if target_year is not None else datetime.now().year
    year_start = anchor_year - BASELINE_YEARS_WINDOW
    year_end   = anchor_year - 1
    path = _baseline_cache_path(country, region, month, year_start, year_end)
    cached = _load_cache(path, BASELINE_TTL_DAYS)
    if cached is not None:
        return cached
    geometry = _build_region_geometry(country, region)
    climatology_img = _month_climatology_ee(geometry, month, year_start, year_end)
    metrics = _reduce_to_region_mean(climatology_img, geometry, "NDVI")
    result = {
        "country": country,
        "region": region,
        "month": month,
        "baseline_start_year": year_start,
        "baseline_end_year": year_end,
        "baseline_ndvi": metrics["mean"],
        "historical_coverage_pct": metrics["pixel_coverage_pct"],
        "valid_pixel_count": metrics["valid_pixel_count"],
        "pixel_count": metrics["pixel_count"],
    }
    _save_cache(path, result)
    return result


def _compute_current(country: str, region: str, year: int, month: int) -> dict:
    """
    Orquesta cache → cómputo GEE (si miss) → save cache.

    Returns dict con current_ndvi + pixel_coverage_pct + metadata.
    """
    path = _current_cache_path(country, region, year, month)
    cached = _load_cache(path, CURRENT_TTL_DAYS)
    if cached is not None:
        return cached
    geometry = _build_region_geometry(country, region)
    current_img = _current_month_ndvi_ee(geometry, year, month)
    metrics = _reduce_to_region_mean(current_img, geometry, "NDVI")
    result = {
        "country": country,
        "region": region,
        "year": year,
        "month": month,
        "current_ndvi": metrics["mean"],
        "pixel_coverage_pct": metrics["pixel_coverage_pct"],
        "valid_pixel_count": metrics["valid_pixel_count"],
        "pixel_count": metrics["pixel_count"],
    }
    _save_cache(path, result)
    return result


def _prior_season(season: str) -> Optional[str]:
    """
    Devuelve la zafra inmediatamente anterior en formato CONAB ('YYYY/YY').
    Ej.: _prior_season('2026/27') -> '2025/26'.
    None si el formato no es parseable (defensa contra datos corruptos).
    """
    try:
        start_str, _end_short = season.split("/")
        start = int(start_str)
        return f"{start - 1}/{str(start)[-2:]}"
    except (AttributeError, ValueError):
        return None


def _infer_conab_direction(country: str, region: str) -> tuple[str, bool]:
    """
    Deduce dirección CONAB (RECOVERY/DETERIORATION/STABLE) leyendo los
    levantamentos más recientes de conab_cana_levantamento.

    Schema real (models/market_data.py:431):
      season VARCHAR, levantamento INT, pub_date DATE,
      sugar_total_mt NUMERIC, revision_sugar_pct NUMERIC.

    Lógica jerárquica (BUSINESS_LOGIC §7.5.8):

      1) PRIMARY — revision_sugar_pct presente:
         Δ pre-computado por CONAB con metodología oficial. Se usa directo.

      2) APERTURA DE ZAFRA — revision_sugar_pct=NULL y levantamento==1:
         CONAB no emite revision en el primer levantamento de cada zafra
         (no hay levantamento previo de la misma temporada). Comparar lev=1
         vs lev=4 de la zafra anterior es phase-mixing (forecast pre-cosecha
         vs cierre retrospectivo); las metodologías y confianza difieren.
         Solución: YoY same-phase delta = compare lev=1 actual contra lev=1
         de la zafra inmediatamente anterior. Same horizon, same method,
         captura shock macroeconómico real.

      3) FALLBACK STABLE — todos los demás casos (DB error, datos NULL,
         stale > 130d, lev != 1 con revision NULL, prior lev=1 no en DB).

    Threshold |Δ| >= 3% para RECOVERY/DETERIORATION (estable bajo ese umbral).

    Robustez: no lanza excepción. Retorna ("STABLE", True) en todos los
    failure modes, loguendo WARNING con contexto. Ver §7.5.8 para detalles.

    Returns:
      (direction, inferred=True) donde direction ∈ VALID_CONAB_DIRECTIONS.
    """
    try:
        from database import SessionLocal
        from sqlalchemy import text
        from sqlalchemy.exc import SQLAlchemyError
    except ImportError as e:
        logger.warning(
            "CONAB imports failed (%s) — fallback STABLE for %s", e, region,
        )
        return "STABLE", True
    try:
        with SessionLocal() as session:
            rows = session.execute(text("""
                SELECT season, levantamento, pub_date,
                       sugar_total_mt, revision_sugar_pct
                FROM conab_cana_levantamento
                ORDER BY pub_date DESC NULLS LAST
                LIMIT 1
            """)).fetchall()
    except SQLAlchemyError as e:
        logger.warning(
            "CONAB DB error (fallback STABLE for %s): %s", region, e,
        )
        return "STABLE", True
    if len(rows) < 1:
        logger.warning(
            "CONAB: no levantamentos disponibles — fallback STABLE for %s",
            region,
        )
        return "STABLE", True
    latest = rows[0]
    latest_season, latest_lev, latest_pub_date, latest_sugar, latest_revision = latest
    if latest_pub_date is None:
        logger.warning(
            "CONAB latest has no pub_date — fallback STABLE for %s", region,
        )
        return "STABLE", True
    from datetime import date as _date
    days_old = (_date.today() - latest_pub_date).days
    if days_old > 130:
        logger.warning(
            "CONAB stale (%dd > 130d) — fallback STABLE for %s",
            days_old, region,
        )
        return "STABLE", True

    # PRIMARY: revision_sugar_pct pre-computado por CONAB
    if latest_revision is not None:
        revision = float(latest_revision)
        source = "revision_pct"
    # APERTURA DE ZAFRA: lev=1 sin revision → YoY same-phase delta
    elif int(latest_lev or 0) == 1:
        prior = _prior_season(latest_season)
        if prior is None:
            logger.warning(
                "CONAB Apertura: cannot parse season=%r — fallback STABLE for %s",
                latest_season, region,
            )
            return "STABLE", True
        try:
            with SessionLocal() as session:
                prior_row = session.execute(text("""
                    SELECT sugar_total_mt
                    FROM conab_cana_levantamento
                    WHERE season = :ps AND levantamento = 1
                    LIMIT 1
                """), {"ps": prior}).fetchone()
        except SQLAlchemyError as e:
            logger.warning(
                "CONAB Apertura DB error (fallback STABLE for %s): %s",
                region, e,
            )
            return "STABLE", True
        if prior_row is None or prior_row[0] is None:
            logger.warning(
                "CONAB Apertura: prior season %s lev=1 not in DB — "
                "fallback STABLE for %s", prior, region,
            )
            return "STABLE", True
        prior_sugar = float(prior_row[0])
        if prior_sugar == 0 or latest_sugar is None:
            logger.warning(
                "CONAB Apertura: sugar_total_mt missing/zero — "
                "fallback STABLE for %s", region,
            )
            return "STABLE", True
        revision = (float(latest_sugar) - prior_sugar) / prior_sugar * 100
        source = f"yoy_apertura (vs {prior} lev=1: {prior_sugar:.2f}Mt)"
    # ANOMALÍA: revision NULL en lev != 1 → no debería ocurrir, fallback STABLE
    else:
        logger.warning(
            "CONAB revision_pct=NULL on non-opening levantamento (lev=%s) — "
            "data anomaly, fallback STABLE for %s", latest_lev, region,
        )
        return "STABLE", True

    if revision >= 3.0:
        direction = "RECOVERY"
    elif revision <= -3.0:
        direction = "DETERIORATION"
    else:
        direction = "STABLE"
    logger.info(
        "CONAB inference for %s: season=%s lev=%s Δ=%+.2f%% [%s] -> %s",
        region, latest_season, latest_lev, revision, source, direction,
    )
    return direction, True


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
    if percentile is None:
        return "NEUTRAL"
    if conab_direction not in VALID_CONAB_DIRECTIONS or conab_direction == "STABLE":
        return "NEUTRAL"
    if percentile >= PERCENTILE_HIGH:
        if conab_direction == "RECOVERY":
            return "CONFIRMATION"
        if conab_direction == "DETERIORATION":
            return "DIVERGENCE_BULLISH"
    if percentile <= PERCENTILE_LOW:
        if conab_direction == "DETERIORATION":
            return "CONFIRMATION"
        if conab_direction == "RECOVERY":
            return "DIVERGENCE_BEARISH"
    return "NEUTRAL"


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
    from datetime import date as _date, timedelta as _timedelta
    from services.data_quality import validate_range

    # 1. Resolver defaults: mes anterior completo
    today = _date.today()
    if year is None or month is None:
        prev_month_last_day = today.replace(day=1) - _timedelta(days=1)
        if year is None:
            year = prev_month_last_day.year
        if month is None:
            month = prev_month_last_day.month

    warnings_acc: list[str] = []

    # 2. Baseline 5yr (cache TTL 180d) — target_year explícito para evitar
    # data leakage en backtesting (BUSINESS_LOGIC §7.5.2)
    baseline = _compute_baseline(country, region, month, target_year=year)
    baseline_ndvi = baseline.get("baseline_ndvi")
    historical_coverage = float(baseline.get("historical_coverage_pct") or 0.0)

    # 3. Gate histórico (80% — sin tiers, crítico siempre)
    validate_range(
        historical_coverage,
        min_value=MIN_HISTORICAL_COVERAGE, max_value=1.0,
        source="gee_ndvi_anomaly", field="historical_coverage_pct",
    )

    # 4. Current month NDVI (cache TTL 7d)
    current = _compute_current(country, region, year, month)
    current_ndvi = current.get("current_ndvi")
    pixel_coverage = float(current.get("pixel_coverage_pct") or 0.0)

    # 5. Coverage gate tiered (raise <30%, warning 30-50%)
    _validate_coverage_gate(
        pixel_coverage,
        source="gee_ndvi_anomaly", field="pixel_coverage_pct",
    )
    if pixel_coverage < COVERAGE_WARNING_THRESHOLD:
        warnings_acc.append(
            f"pixel_coverage_degraded={pixel_coverage:.1%}"
        )

    # 6. Validar NDVIs no-None
    if baseline_ndvi is None or current_ndvi is None:
        raise DataQualityError(
            "NDVI value is None — likely GEE returned empty result",
            source="gee_ndvi_anomaly", field="anomaly_value",
            value=f"baseline={baseline_ndvi}, current={current_ndvi}",
            expected="float NDVI values from GEE",
        )

    # 7. Calcular anomalía + validar rango
    anomaly = float(current_ndvi) - float(baseline_ndvi)
    validate_range(
        anomaly,
        min_value=ANOMALY_VALID_RANGE[0], max_value=ANOMALY_VALID_RANGE[1],
        source="gee_ndvi_anomaly", field="anomaly_value",
    )

    # 8. History → robust_stats (excluyendo current — todavía no appendado)
    history = _load_anomaly_history(country, region)
    stats = robust_stats(history, anomaly)

    # 9. Append a history (después de stats: current no se incluye en su propio rank)
    _append_anomaly_to_history(country, region, year, month, anomaly, pixel_coverage)

    # 10. Construir result frozen
    result = NdviAnomalyResult(
        country=country, region=region, year=year, month=month,
        anomaly_value=round(anomaly, 4),
        current_ndvi=round(float(current_ndvi), 4),
        baseline_ndvi=round(float(baseline_ndvi), 4),
        pixel_coverage_pct=round(pixel_coverage, 3),
        historical_coverage_pct=round(historical_coverage, 3),
        percentile_rank=stats.get("percentile_rank"),
        modified_z=stats.get("modified_z"),
        conviction=stats.get("conviction", "INSUFFICIENT_DATA"),
        is_extreme_high=bool(stats.get("is_extreme_high", False)),
        is_extreme_low=bool(stats.get("is_extreme_low", False)),
        n_historical_obs=len(history),
        computed_at=datetime.now(),
        warnings=tuple(warnings_acc),
    )

    # 11. Audit log INFO (BUSINESS_LOGIC §5)
    pct_s = (f"{result.percentile_rank:.1f}"
             if result.percentile_rank is not None else "N/A")
    mz_s  = (f"{result.modified_z:+.2f}"
             if result.modified_z is not None else "N/A")
    logger.info(
        "ndvi_anomaly | %s_%s | y=%d m=%d | anomaly=%+.4f cur=%.4f base=%.4f "
        "| pct=%s mZ=%s conviction=%s | coverage=%.1f%% n_hist=%d",
        country, region, year, month,
        anomaly, current_ndvi, baseline_ndvi,
        pct_s, mz_s, result.conviction,
        pixel_coverage * 100, len(history),
    )

    return result


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
      Todas las excepciones de compute_ndvi_anomaly (propagan transparentemente)

    Nota: _infer_conab_direction NO lanza excepción cuando no puede inferir;
    retorna ("STABLE", True) con WARNING log. Esto produce market_signal=NEUTRAL
    como degradación elegante. Ver BUSINESS_LOGIC §7.5.8.
    """
    # 1. Validación de direction explícita
    if conab_direction is not None and conab_direction not in VALID_CONAB_DIRECTIONS:
        raise ValueError(
            f"conab_direction must be one of {VALID_CONAB_DIRECTIONS}, "
            f"got {conab_direction!r}"
        )

    # 2. Compute anomaly (propaga DataQualityError si gate falla)
    anomaly_result = compute_ndvi_anomaly(country, region, year=year, month=month)

    # 3. Resolver direction (inferir si no se pasó explícita)
    if conab_direction is None:
        direction, inferred = _infer_conab_direction(country, region)
    else:
        direction, inferred = conab_direction, False

    # 4. Clasificar
    signal = _classify_market_signal(anomaly_result.percentile_rank, direction)

    # 5. Construir audit log lines (formato BUSINESS_LOGIC §5)
    pct_s = (f"{anomaly_result.percentile_rank:.1f}"
             if anomaly_result.percentile_rank is not None else "N/A")
    mz_s  = (f"{anomaly_result.modified_z:+.2f}"
             if anomaly_result.modified_z is not None else "N/A")
    line1 = (
        f"Benchmark GEE vs CONAB | Region={region} | "
        f"Percentile={pct_s} | mZ={mz_s}"
    )
    logger.info("%s", line1)

    # Audit segunda línea según signal
    if signal == "CONFIRMATION":
        anchor = (
            f"P={pct_s} >= {PERCENTILE_HIGH:.0f}"
            if direction == "RECOVERY"
            else f"P={pct_s} <= {PERCENTILE_LOW:.0f}"
        )
        line2 = f"-> Market Confirmation | direction={direction}, {anchor}"
        logger.info("%s", line2)
    elif signal == "DIVERGENCE_BULLISH":
        line2 = (
            f"-> Market DIVERGENCE_BULLISH | CONAB=DETERIORATION, "
            f"satellite P={pct_s} (above baseline)"
        )
        logger.warning("%s", line2)
    elif signal == "DIVERGENCE_BEARISH":
        line2 = (
            f"-> Market DIVERGENCE_BEARISH | CONAB=RECOVERY, "
            f"satellite P={pct_s} (below baseline)"
        )
        logger.warning("%s", line2)
    else:  # NEUTRAL
        line2 = (
            f"-> Market NEUTRAL | direction={direction}, P={pct_s} "
            f"(no actionable divergence)"
        )
        logger.info("%s", line2)

    # 6. Propagar warnings del anomaly_result al audit log si hay alguno
    audit_lines: list[str] = [line1, line2]
    for w in anomaly_result.warnings:
        warn_line = f"-> WARNING propagated: {w}"
        logger.warning("%s", warn_line)
        audit_lines.append(warn_line)

    return MarketBenchmark(
        anomaly_result=anomaly_result,
        conab_direction=direction,
        conab_inferred=inferred,
        market_signal=signal,
        audit_log_lines=tuple(audit_lines),
    )
