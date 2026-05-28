"""
CropEstimator — Stage 1 MVP: estimacion de produccion via NDVI integral + WorldCover.

Metodologia:
  1. Mascara de area: ESA WorldCover 2021 clase 40 (Croplands), sin RF.
  2. NDVI mensual: mediana Sentinel-2 por mes, filtrada a pixels cropland + umbral NDVI.
  3. Integral estacional: suma de medias NDVI mensuales sobre la temporada completa.
  4. Ratio historico: integral_actual / media_baseline_5yr — captura YoY variation.
  5. Estimacion: area_ha * base_sugar_yield_t_per_ha * ratio → Mt

El base_sugar_yield_t_per_ha es el parametro critico: NO es rendimiento agronomico puro.
Absorbe tanto el rendimiento real como la fraccion WorldCover-cropland que es el cultivo
objetivo. Se calibra con --calibrate contra produccion historica conocida.

Salida de run():
  {
    "country":               str,
    "season_year":           int,
    "area_ha":               float,      # WorldCover cropland en la region
    "ndvi_integral":         float,      # suma NDVI mensual temporada actual
    "baseline_integral":     float,      # media 5yr
    "ndvi_ratio":            float,      # actual / baseline
    "base_sugar_yield_t_ha": float,      # parametro de calibracion
    "estimated_mt":          float,      # estimacion produccion azucar
    "confidence":            float,      # 0-1 segun completitud datos
    "data_completeness_pct": float,      # % meses con datos validos
    "n_baseline_years":      int,
    "monthly_ndvi":          dict,       # {YYYY-MM: ndvi_value}
    "calibration_rmse":      float|None, # RMSE vs produccion conocida (si disponible)
    "source":                str,        # "gee_ndvi_integral"
  }
"""
import json
import logging
import os
import calendar
from datetime import date
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "gee_countries.yaml",
)

S2_COLLECTION   = "COPERNICUS/S2_SR_HARMONIZED"
WORLDCOVER_2021 = "ESA/WorldCover/v200/2021"
MAX_PIXELS      = 1e11

# ── Cache de disco ─────────────────────────────────────────────────────────────
# Meses historicos (>60d desde fin de mes): cache permanente — S2 ya no cambia.
# Meses recientes (<60d): TTL de 7 dias para capturar nuevas imagenes S2.
_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "gee_cache",
)
_CACHE_TTL_RECENT_DAYS    = 7       # meses recientes: refrescar semanal
_CACHE_STABLE_THRESHOLD_D = 60      # umbral para considerar un mes "estable"


def load_country_config(country_key: str) -> dict:
    """Lee la configuracion del pais desde gee_countries.yaml."""
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        full = yaml.safe_load(f)
    countries = full.get("countries", {})
    if country_key not in countries:
        raise KeyError("Pais '%s' no encontrado en gee_countries.yaml. "
                       "Disponibles: %s" % (country_key, list(countries.keys())))
    return countries[country_key]


class CropEstimator:
    """
    Clase base para estimacion de produccion de cultivos via GEE.

    Subclases hijas (SugarcaneEstimator_India, etc.) pueden sobreescribir:
      - _get_season_months(): logica de temporada especifica
      - _extra_masks(): mascaras adicionales (altitud, pendiente, etc.)
      - _post_process(): ajuste post-estimacion especifico del cultivo
    """

    def __init__(self, country_key: str, config: Optional[dict] = None):
        self.country_key = country_key
        self.config = config or load_country_config(country_key)
        self._ee_ok = False
        self._geometry = None
        self._worldcover_mask = None
        self._area_ha: Optional[float] = None   # cache

    # ── GEE init ───────────────────────────────────────────────────────────────

    def _init_ee(self) -> bool:
        """Inicializa Earth Engine. Reutiliza sesion si ya esta activa."""
        if self._ee_ok:
            return True
        try:
            import ee
            from config import GEE_PROJECT_ID
            try:
                ee.data.getInfo("")   # ping rapido
                self._ee_ok = True
                return True
            except Exception:
                pass
            if GEE_PROJECT_ID:
                ee.Initialize(project=GEE_PROJECT_ID)
            else:
                ee.Initialize()
            self._ee_ok = True
            return True
        except Exception as e:
            logger.warning("GEE init failed: %s", e)
            return False

    # ── Cache de disco ─────────────────────────────────────────────────────────

    def _ndvi_cache_path(self, year: int, month: int) -> str:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        return os.path.join(_CACHE_DIR, "%s_%d_%02d.json" % (self.country_key, year, month))

    def _area_cache_path(self) -> str:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        return os.path.join(_CACHE_DIR, "%s_area.json" % self.country_key)

    def _month_is_stable(self, year: int, month: int) -> bool:
        """True si el mes terminó hace >60 dias — S2 ya no añade imágenes nuevas."""
        last_day = calendar.monthrange(year, month)[1]
        return (date.today() - date(year, month, last_day)).days > _CACHE_STABLE_THRESHOLD_D

    def _read_ndvi_cache(self, year: int, month: int) -> tuple[bool, Optional[float]]:
        """
        Retorna (cache_hit, ndvi).
        cache_hit=False → hay que calcular via GEE.
        ndvi puede ser None (mes con nubosidad total — también se cachea).
        """
        path = self._ndvi_cache_path(year, month)
        if not os.path.exists(path):
            return False, None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if self._month_is_stable(year, month):
                return True, data.get("ndvi")   # permanente
            cached_at = data.get("cached_at")
            if cached_at:
                age = (date.today() - date.fromisoformat(cached_at)).days
                if age <= _CACHE_TTL_RECENT_DAYS:
                    return True, data.get("ndvi")
        except Exception:
            pass
        return False, None

    def _write_ndvi_cache(self, year: int, month: int, ndvi: Optional[float]):
        try:
            with open(self._ndvi_cache_path(year, month), "w", encoding="utf-8") as f:
                json.dump({"ndvi": ndvi, "cached_at": date.today().isoformat()}, f)
        except Exception as e:
            logger.debug("GEE cache write %s %d-%02d: %s", self.country_key, year, month, e)

    def _read_area_cache(self) -> Optional[float]:
        path = self._area_cache_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return float(json.load(f)["area_ha"])
        except Exception:
            return None

    def _write_area_cache(self, area_ha: float):
        try:
            with open(self._area_cache_path(), "w", encoding="utf-8") as f:
                json.dump({"area_ha": area_ha, "cached_at": date.today().isoformat()}, f)
        except Exception as e:
            logger.debug("GEE area cache write %s: %s", self.country_key, e)

    # ── Geometria ──────────────────────────────────────────────────────────────

    def _get_geometry(self):
        """Construye ee.Geometry desde config boundary."""
        if self._geometry is not None:
            return self._geometry
        import ee
        boundary = self.config["boundary"]
        coords = boundary["coords"]
        if boundary["type"] in ("polygon", "bbox"):
            self._geometry = ee.Geometry.Polygon(coords)
        else:
            raise ValueError("boundary.type '%s' no soportado" % boundary["type"])
        return self._geometry

    # ── WorldCover mask ────────────────────────────────────────────────────────

    def _get_worldcover_mask(self):
        """ESA WorldCover 2021 — mascara de clases configuradas (default: 40=Croplands)."""
        if self._worldcover_mask is not None:
            return self._worldcover_mask
        import ee
        wc = ee.Image(WORLDCOVER_2021)
        classes = self.config.get("worldcover_classes", [40])
        mask = wc.eq(classes[0])
        for cls in classes[1:]:
            mask = mask.Or(wc.eq(cls))
        self._worldcover_mask = mask
        return self._worldcover_mask

    def compute_crop_area_ha(self) -> float:
        """
        Area WorldCover-cropland en la region (hectareas).
        Cache en memoria + disco (WorldCover 2021 es estatico — no cambia).
        """
        if self._area_ha is not None:
            return self._area_ha

        cached = self._read_area_cache()
        if cached:
            self._area_ha = cached
            logger.debug("GEE %s: area desde cache = %.0f ha", self.country_key, cached)
            return self._area_ha

        if not self._init_ee():
            return 0.0

        import ee
        area_image = ee.Image.pixelArea().updateMask(self._get_worldcover_mask())
        stats = area_image.reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=self._get_geometry(),
            scale=100,
            maxPixels=MAX_PIXELS,
            bestEffort=True,
        )
        area_m2 = stats.get("area").getInfo()
        self._area_ha = float(area_m2) / 10_000 if area_m2 else 0.0
        logger.info("GEE %s: area WorldCover-cropland = %.0f ha",
                    self.country_key, self._area_ha)
        self._write_area_cache(self._area_ha)
        return self._area_ha

    # ── Sentinel-2 pipeline ────────────────────────────────────────────────────

    @staticmethod
    def _s2_mask_clouds(img):
        """SCL-based cloud mask (excluye shadow, cloud, cirrus, nieve)."""
        import ee
        scl = img.select("SCL")
        # 3=cloud shadow, 8=medium cloud prob, 9=high cloud prob, 10=thin cirrus, 11=snow
        bad = [3, 8, 9, 10, 11]
        good = scl.neq(bad[0])
        for cls in bad[1:]:
            good = good.And(scl.neq(cls))
        return img.updateMask(good)

    @staticmethod
    def _s2_add_ndvi(img):
        """Agrega banda NDVI = (B8-B4)/(B8+B4)."""
        ndvi = img.normalizedDifference(["B8", "B4"]).rename("NDVI")
        return img.addBands(ndvi)

    def _extra_masks(self, ndvi_image):
        """
        Hook para mascaras adicionales en subclases.
        Por defecto no hace nada.
        """
        return ndvi_image

    def compute_monthly_ndvi(self, year: int, month: int) -> Optional[float]:
        """
        NDVI medio para un mes especifico, filtrado a pixels cropland
        con NDVI > ndvi_threshold.

        Busca en cache de disco primero. Solo llama a GEE si no hay cache valido.
        Retorna None si no hay imagenes disponibles o todos los pixels estan nublados.
        """
        hit, cached_val = self._read_ndvi_cache(year, month)
        if hit:
            logger.debug("GEE %s %d-%02d: cache hit (ndvi=%s)",
                         self.country_key, year, month, cached_val)
            return cached_val

        if not self._init_ee():
            return None

        import ee

        start = ee.Date.fromYMD(year, month, 1)
        end   = start.advance(1, "month")

        collection = (
            ee.ImageCollection(S2_COLLECTION)
            .filterDate(start, end)
            .filterBounds(self._get_geometry())
            .map(CropEstimator._s2_mask_clouds)
            .map(CropEstimator._s2_add_ndvi)
        )

        n_images = collection.size().getInfo()
        if n_images == 0:
            logger.debug("GEE %s %d-%02d: sin imagenes S2", self.country_key, year, month)
            self._write_ndvi_cache(year, month, None)
            return None

        ndvi_median = collection.select("NDVI").median()
        ndvi_masked = ndvi_median.updateMask(self._get_worldcover_mask())

        threshold = self.config.get("ndvi_threshold", 0.30)
        ndvi_masked = ndvi_masked.updateMask(ndvi_masked.gt(threshold))
        ndvi_masked = self._extra_masks(ndvi_masked)

        stats = ndvi_masked.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=self._get_geometry(),
            scale=self.config.get("scale_m", 500),
            maxPixels=MAX_PIXELS,
            bestEffort=True,
        )

        val = stats.get("NDVI").getInfo()
        if val is None:
            logger.debug("GEE %s %d-%02d: NDVI = None (nubosidad total?)",
                         self.country_key, year, month)
            self._write_ndvi_cache(year, month, None)
            return None

        result = float(val)
        self._write_ndvi_cache(year, month, result)
        return result

    # ── Temporada ──────────────────────────────────────────────────────────────

    def _get_season_months(self, season_year: int) -> list[tuple[int, int]]:
        """
        Devuelve lista de (year, month) para toda la temporada.

        Para temporadas que cruzan el 1 de enero (season_spans_year=True):
          - Oct-Dic del season_year
          - Ene-Abr del season_year+1
        """
        cfg = self.config["growing_season"]
        start = cfg["start_month"]
        end   = cfg["end_month"]
        spans = cfg.get("season_spans_year", False)

        months = []
        if spans:
            for m in range(start, 13):
                months.append((season_year, m))
            for m in range(1, end + 1):
                months.append((season_year + 1, m))
        else:
            for m in range(start, end + 1):
                months.append((season_year, m))

        return months

    # ── Integral estacional ────────────────────────────────────────────────────

    def compute_seasonal_integral(self, season_year: int) -> dict:
        """
        Calcula la integral NDVI de la temporada completa.

        Para temporadas en curso (algunos meses futuros), solo computa
        los meses con datos disponibles (hasta hoy).

        Retorna:
          {
            "integral": float,
            "monthly_ndvi": {"YYYY-MM": float|None, ...},
            "completeness_pct": float,
            "n_months": int,
            "n_valid": int,
          }
        """
        today = date.today()
        season_months = self._get_season_months(season_year)
        monthly = {}

        for year, month in season_months:
            # No computar meses futuros (datos incompletos)
            if date(year, month, 1) > today:
                monthly["%d-%02d" % (year, month)] = None
                continue

            ndvi = self.compute_monthly_ndvi(year, month)
            monthly["%d-%02d" % (year, month)] = ndvi

        valid = [v for v in monthly.values() if v is not None]
        integral = sum(valid)
        completeness = len(valid) / len(season_months) if season_months else 0.0

        logger.info(
            "GEE %s %d: integral=%.3f  completitud=%.0f%%  (%d/%d meses)",
            self.country_key, season_year, integral, completeness * 100,
            len(valid), len(season_months),
        )

        return {
            "integral": integral,
            "monthly_ndvi": monthly,
            "completeness_pct": round(completeness * 100, 1),
            "n_months": len(season_months),
            "n_valid": len(valid),
        }

    # ── Baseline historico ─────────────────────────────────────────────────────

    def compute_baseline_integral(self, current_season: int) -> dict:
        """
        Calcula la media de integrales NDVI de los ultimos N años.
        Solo incluye años con completitud >= 60%.

        Retorna {mean, std, values, years_used}
        """
        n_years = self.config.get("baseline_years", 5)
        baseline_years = [current_season - i for i in range(1, n_years + 2)]

        values = []
        years_used = []

        for yr in baseline_years:
            if len(years_used) >= n_years:
                break
            result = self.compute_seasonal_integral(yr)
            if result["completeness_pct"] >= 60.0:
                values.append(result["integral"])
                years_used.append(yr)
            else:
                logger.debug("GEE %s baseline %d: completitud %.0f%% — omitido",
                             self.country_key, yr, result["completeness_pct"])

        if not values:
            return {"mean": None, "std": None, "values": [], "years_used": []}

        mean = sum(values) / len(values)
        std  = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5

        logger.info("GEE %s baseline: mean=%.3f  std=%.3f  años=%s",
                    self.country_key, mean, std, years_used)

        return {"mean": mean, "std": std, "values": values, "years_used": years_used}

    # ── Calibracion OLS ───────────────────────────────────────────────────────

    def calibrate(self) -> dict:
        """
        Calibra base_sugar_yield_t_per_ha via OLS (mínimos cuadrados sin intercepto)
        contra producción histórica conocida.

        Fórmula: estimated_i = x_i * yield,  x_i = area_ha * ratio_i / 1e6
        OLS exacto: yield_opt = Σ(actual_i * x_i) / Σ(x_i²)
        Esto minimiza RMSE, no solo el bias — a diferencia de avg_actual/avg_estimated.

        Cuando las subclases implementen _get_season_stress_factors(), los factores
        de estrés (LST, SPI) quedan recogidos en seasons[*]["stress"] listos para
        extender a calibración multi-variable via numpy.linalg.lstsq.

        Retorna {rmse_mt, rmse_ols_mt, yield_ols, ols_factor, seasons, ...}
        """
        from gee.calibration_data import get_calibration_series
        calibration_seasons = self.config.get("calibration_seasons", [])
        known = get_calibration_series(
            self.country_key, calibration_seasons, self.config, session=None
        )
        if not known:
            return {"error": "No hay produccion conocida para calibrar"}

        area_ha = self.compute_crop_area_ha()
        base_yield = self.config["base_sugar_yield_t_per_ha"]

        # — Recolectar datos por temporada ————————————————————————————
        records = []
        for season_yr_str, actual_mt in sorted(known.items()):
            season_yr = int(season_yr_str)
            current = self.compute_seasonal_integral(season_yr)
            if current["completeness_pct"] < 40:
                continue

            baseline_yrs = [season_yr - i for i in range(1, 6)]
            baseline_integrals = []
            for byr in baseline_yrs:
                b = self.compute_seasonal_integral(byr)
                if b["completeness_pct"] >= 60:
                    baseline_integrals.append(b["integral"])
            if not baseline_integrals:
                continue

            baseline_mean = sum(baseline_integrals) / len(baseline_integrals)
            ratio = current["integral"] / baseline_mean if baseline_mean > 0 else 1.0
            records.append({
                "season": season_yr,
                "actual": float(actual_mt),
                "ratio":  ratio,
                "stress": self._get_season_stress_factors(season_yr),
            })

        if not records:
            return {"error": "Sin datos suficientes para calibrar"}

        # — OLS single-variable: yield_opt = Σ(actual·x) / Σ(x²) ———
        xs     = [area_ha * r["ratio"] / 1_000_000 for r in records]
        ys     = [r["actual"] for r in records]
        dot_xy = sum(xi * yi for xi, yi in zip(xs, ys))
        dot_xx = sum(xi * xi for xi in xs)
        yield_ols = dot_xy / dot_xx if dot_xx > 0 else base_yield

        # — Métricas con yield config y con yield OLS —————————————————
        details = []
        err_cfg, err_ols = [], []
        for rec, xi in zip(records, xs):
            e_cfg = xi * base_yield - rec["actual"]
            e_ols = xi * yield_ols  - rec["actual"]
            err_cfg.append(e_cfg)
            err_ols.append(e_ols)
            details.append({
                "season":           rec["season"],
                "actual_mt":        round(rec["actual"], 3),
                "estimated_mt":     round(xi * base_yield, 3),
                "estimated_ols_mt": round(xi * yield_ols, 3),
                "error_mt":         round(e_cfg, 3),
                "error_ols_mt":     round(e_ols, 3),
                "ratio":            round(rec["ratio"], 3),
                "stress":           rec["stress"],
            })

        def _rmse(e): return (sum(v ** 2 for v in e) / len(e)) ** 0.5
        def _mae(e):  return sum(abs(v) for v in e) / len(e)
        def _bias(e): return sum(e) / len(e)

        return {
            "rmse_mt":     round(_rmse(err_cfg), 3),
            "mae_mt":      round(_mae(err_cfg),  3),
            "bias_mt":     round(_bias(err_cfg), 3),
            "rmse_ols_mt": round(_rmse(err_ols), 3),
            "mae_ols_mt":  round(_mae(err_ols),  3),
            "n_seasons":   len(records),
            "seasons":     details,
            "area_ha":     round(area_ha),
            "base_yield":  base_yield,
            "yield_ols":   round(yield_ols, 4),
            "ols_factor":  round(yield_ols / base_yield, 3) if base_yield > 0 else None,
        }

    # ── Entry point ────────────────────────────────────────────────────────────

    def _get_season_stress_factors(self, season_year: int) -> dict:
        """
        Hook para variables de estrés por temporada (LST anomalía, SPI acumulado...).
        Override en subclases cuando los módulos LST/SPI estén integrados.
        Retorna {} mientras no estén disponibles → sin penalización de estrés.
        """
        return {}

    def _compute_estimate(self, area_ha: float, ndvi_ratio: float,
                          stress: Optional[dict] = None) -> float:
        """
        Producción estimada (Mt) desde área, ratio NDVI y yield configurado.
        Override en subclases para aplicar factores de estrés (LST, SPI).
        stress: dict de factores precomputados por _get_season_stress_factors().
        """
        base_yield = self.config["base_sugar_yield_t_per_ha"]
        return (area_ha * base_yield * ndvi_ratio) / 1_000_000

    def _post_process(self, result: dict) -> dict:
        """Hook para ajustes post-estimacion en subclases."""
        return result

    def run(self, season_year: Optional[int] = None) -> dict:
        """
        Ejecuta el pipeline completo para la temporada indicada.

        Si season_year es None, usa la temporada actual:
          - mes >= 10: season_year = año actual
          - mes < 10:  season_year = año anterior
        """
        if season_year is None:
            today = date.today()
            season_year = today.year if today.month >= 10 else today.year - 1

        logger.info("GEE %s: iniciando estimacion para temporada %d/%02d",
                    self.country_key, season_year, (season_year + 1) % 100)

        if not self._init_ee():
            return {"error": "GEE no disponible", "country": self.country_key,
                    "season_year": season_year, "estimated_mt": None}

        # 1. Area cropland
        area_ha = self.compute_crop_area_ha()
        if area_ha == 0:
            return {"error": "Area cropland = 0", "country": self.country_key,
                    "season_year": season_year, "estimated_mt": None}

        # 2. Integral estacional actual
        current = self.compute_seasonal_integral(season_year)

        # 3. Baseline historico
        baseline = self.compute_baseline_integral(season_year)

        if baseline["mean"] is None or baseline["mean"] == 0:
            # Sin baseline: usar integral absoluta con factor conservador
            logger.warning("GEE %s: sin baseline disponible — estimacion no calibrada",
                           self.country_key)
            ndvi_ratio = 1.0
            calibration_rmse = None
        else:
            ndvi_ratio = current["integral"] / baseline["mean"]
            calibration_rmse = None  # se calcula en calibrate()

        # 4. Estimacion produccion (stress precomputado para que _compute_estimate lo aplique)
        stress       = self._get_season_stress_factors(season_year)
        estimated_mt = self._compute_estimate(area_ha, ndvi_ratio, stress)

        # 5. Confidence
        data_completeness = current["completeness_pct"] / 100
        baseline_quality  = min(len(baseline.get("years_used", [])) / 4.0, 1.0)
        # Penalizar si la temporada esta muy temprana (< 40% completada)
        season_progress_penalty = max(0, data_completeness - 0.4) / 0.6 if data_completeness < 0.4 else 1.0
        confidence = round(
            0.45 * data_completeness
            + 0.35 * baseline_quality
            + 0.20 * season_progress_penalty,
            3
        )
        confidence = max(0.30, min(0.88, confidence))

        result = {
            "country":               self.config["name"],
            "country_key":           self.country_key,
            "season_year":           season_year,
            "area_ha":               round(area_ha),
            "ndvi_integral":         round(current["integral"], 4),
            "baseline_integral":     round(baseline["mean"], 4) if baseline["mean"] else None,
            "ndvi_ratio":            round(ndvi_ratio, 4),
            "base_sugar_yield_t_ha": base_yield,
            "estimated_mt":          round(estimated_mt, 3),
            "confidence":            confidence,
            "data_completeness_pct": current["completeness_pct"],
            "n_baseline_years":      len(baseline.get("years_used", [])),
            "baseline_years_used":   baseline.get("years_used", []),
            "monthly_ndvi":          current["monthly_ndvi"],
            "calibration_rmse":      calibration_rmse,
            "stress_factors":        stress,
            "source":                "gee_ndvi_integral",
        }

        return self._post_process(result)
