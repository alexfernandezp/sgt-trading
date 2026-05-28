"""
LSTStressEstimator — anomalía de Land Surface Temperature via Landsat 8/9 en GEE.

Por qué LST y no solo NDVI:
  La caña de azúcar es perenne — sigue verde bajo estrés térmico severo. El NDVI
  apenas varía aunque CCS% (contenido azúcar) caiga 1-2 puntos por temperaturas
  sostenidas >35°C durante el período de crecimiento. LST captura ese estrés
  semanas antes de que se refleje en la molienda.

Metodología:
  1. Landsat 8/9 Collection 2 Level 2 (ST_B10 → LST en Kelvin).
     LST_C = ST_B10 × 0.00341802 + 149.0 - 273.15
  2. Cloud mask via QA_PIXEL (bits dilated_cloud, cloud, cloud_shadow, snow).
  3. LST media mensual sobre la región → media del período de estrés.
  4. Anomalía = LST_actual - baseline_5yr (°C). Positivo = más caliente = estrés.
  5. Integración: _compute_estimate() aplica penalty = k_lst × max(0, anomalía).
     k_lst se calibra contra temporadas históricas con calibrate().

Landsat 8: operativo desde abril 2013.
Landsat 9: operativo desde febrero 2022 (combinar ambos para mayor revisita efectiva).
Resolución térmica: 100m (remuestreado a 30m en C2 L2). Scale GEE: 1000m suficiente.

Nota sobre nubosidad India (jun-ago):
  El monzón (jun-ago) reduce cobertura Landsat en India. La media se calcula solo
  sobre meses con datos válidos → efectivamente Apr-May y Sep dominan la señal.
  Eso es correcto agronómicamente: el calor pre-monzón (Apr-May) es el principal
  determinante de CCS% al inicio de la siguiente temporada de crushing.
"""
import json
import logging
import os
import calendar
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

L8_COLLECTION = "LANDSAT/LC08/C02/T1_L2"
L9_COLLECTION = "LANDSAT/LC09/C02/T1_L2"

_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "gee_cache",
)
_CACHE_TTL_RECENT_DAYS    = 7
_CACHE_STABLE_THRESHOLD_D = 60


class LSTStressEstimator:
    """
    Calcula anomalía de LST para un país/temporada dados.
    Puede compartir geometría con CropEstimator para evitar duplicar la inicialización.
    """

    def __init__(self, country_key: str, config: dict, geometry=None):
        self.country_key = country_key
        self.config      = config
        self._ee_ok      = False
        self._geometry   = geometry     # si se pasa desde CropEstimator, no recalcula

    # ── Cache de disco ─────────────────────────────────────────────────────────

    def _lst_cache_path(self, year: int, month: int) -> str:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        return os.path.join(_CACHE_DIR, "%s_lst_%d_%02d.json" % (self.country_key, year, month))

    def _month_is_stable(self, year: int, month: int) -> bool:
        last_day = calendar.monthrange(year, month)[1]
        return (date.today() - date(year, month, last_day)).days > _CACHE_STABLE_THRESHOLD_D

    def _read_lst_cache(self, year: int, month: int) -> tuple[bool, Optional[float]]:
        path = self._lst_cache_path(year, month)
        if not os.path.exists(path):
            return False, None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if self._month_is_stable(year, month):
                return True, data.get("lst_c")
            cached_at = data.get("cached_at")
            if cached_at:
                age = (date.today() - date.fromisoformat(cached_at)).days
                if age <= _CACHE_TTL_RECENT_DAYS:
                    return True, data.get("lst_c")
        except Exception:
            pass
        return False, None

    def _write_lst_cache(self, year: int, month: int, lst_c: Optional[float]):
        try:
            with open(self._lst_cache_path(year, month), "w", encoding="utf-8") as f:
                json.dump({"lst_c": lst_c, "cached_at": date.today().isoformat()}, f)
        except Exception as e:
            logger.debug("LST cache write %s %d-%02d: %s", self.country_key, year, month, e)

    # ── GEE init ───────────────────────────────────────────────────────────────

    def _init_ee(self) -> bool:
        if self._ee_ok:
            return True
        try:
            import ee
            from config import GEE_PROJECT_ID
            try:
                ee.data.getInfo("")
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
            logger.warning("GEE LST init failed: %s", e)
            return False

    # ── Geometría ──────────────────────────────────────────────────────────────

    def _get_geometry(self):
        if self._geometry is not None:
            return self._geometry
        import ee
        boundary = self.config["boundary"]
        self._geometry = ee.Geometry.Polygon(boundary["coords"])
        return self._geometry

    # ── Landsat pipeline ──────────────────────────────────────────────────────

    @staticmethod
    def _mask_clouds(img):
        """QA_PIXEL: elimina dilated cloud (bit1), cloud (bit3), shadow (bit4), snow (bit5)."""
        import ee
        qa   = img.select("QA_PIXEL")
        mask = qa.bitwiseAnd((1 << 1) | (1 << 3) | (1 << 4) | (1 << 5)).eq(0)
        return img.updateMask(mask)

    @staticmethod
    def _add_lst_celsius(img):
        """ST_B10 → LST en Celsius según Landsat Collection 2 Level 2 spec."""
        import ee
        lst_c = (
            img.select("ST_B10")
            .multiply(0.00341802)
            .add(149.0 - 273.15)
            .rename("LST_C")
        )
        return img.addBands(lst_c)

    # ── LST mensual ────────────────────────────────────────────────────────────

    def compute_monthly_lst(self, year: int, month: int) -> Optional[float]:
        """
        LST media mensual sobre la región (°C), pixeles claros únicamente.
        Combina Landsat 8 y 9 para maximizar cobertura (revisita efectiva ~8d).
        Retorna None si no hay imágenes o nubosidad total.
        """
        hit, cached = self._read_lst_cache(year, month)
        if hit:
            logger.debug("LST %s %d-%02d: cache hit (%s°C)",
                         self.country_key, year, month, cached)
            return cached

        if not self._init_ee():
            return None

        import ee
        start = ee.Date.fromYMD(year, month, 1)
        end   = start.advance(1, "month")
        geom  = self._get_geometry()
        scale = self.config.get("scale_m", 1000)

        def _col(col_id):
            return (
                ee.ImageCollection(col_id)
                .filterDate(start, end)
                .filterBounds(geom)
                .map(LSTStressEstimator._mask_clouds)
                .map(LSTStressEstimator._add_lst_celsius)
                .select("LST_C")
            )

        combined = _col(L8_COLLECTION).merge(_col(L9_COLLECTION))
        n_imgs = combined.size().getInfo()
        if n_imgs == 0:
            logger.debug("LST %s %d-%02d: sin imágenes Landsat", self.country_key, year, month)
            self._write_lst_cache(year, month, None)
            return None

        stats = combined.mean().reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=geom,
            scale=scale,
            maxPixels=1e11,
            bestEffort=True,
        )
        val = stats.get("LST_C").getInfo()
        if val is None:
            self._write_lst_cache(year, month, None)
            return None

        result = round(float(val), 3)
        self._write_lst_cache(year, month, result)
        logger.info("LST %s %d-%02d: %.2f°C  (n=%d imgs)", self.country_key, year, month, result, n_imgs)
        return result

    # ── Período de estrés ──────────────────────────────────────────────────────

    def _get_stress_month_pairs(self, season_year: int) -> list[tuple[int, int]]:
        """
        Devuelve lista (year, month) para el período de estrés de la temporada.
        Soporta períodos que no cruzan el 1 de enero (stress_spans_year: false)
        y períodos que sí lo cruzan (stress_spans_year: true, India=false).
        """
        sp     = self.config.get("stress_periods", {})
        months = sp.get("lst_months", [])
        spans  = sp.get("stress_spans_year", False)

        if not spans:
            return [(season_year, m) for m in sorted(months)]

        start = sp.get("stress_start_month", min(months) if months else 1)
        end   = sp.get("stress_end_month",   max(months) if months else 12)
        pairs = [(season_year, m) for m in range(start, 13)]
        pairs += [(season_year + 1, m) for m in range(1, end + 1)]
        return pairs

    def compute_period_lst(self, season_year: int) -> dict:
        """
        LST media sobre todos los meses de estrés de una temporada.
        Meses con nubosidad total (None) quedan excluidos de la media.
        """
        today        = date.today()
        month_pairs  = self._get_stress_month_pairs(season_year)
        monthly: dict[str, Optional[float]] = {}

        for yr, mo in month_pairs:
            key = "%d-%02d" % (yr, mo)
            if date(yr, mo, 1) > today:
                monthly[key] = None
                continue
            monthly[key] = self.compute_monthly_lst(yr, mo)

        valid = [v for v in monthly.values() if v is not None]
        mean_lst     = sum(valid) / len(valid) if valid else None
        completeness = len(valid) / len(month_pairs) if month_pairs else 0.0

        return {
            "mean_lst_c":       round(mean_lst, 3) if mean_lst is not None else None,
            "monthly_lst":      monthly,
            "completeness_pct": round(completeness * 100, 1),
            "n_valid":          len(valid),
            "n_months":         len(month_pairs),
        }

    # ── Anomalía ───────────────────────────────────────────────────────────────

    def compute_lst_anomaly(self, season_year: int, baseline_years: int = 5) -> Optional[float]:
        """
        Anomalía de LST vs media histórica (°C).
        Positivo = más caliente que el histórico = estrés térmico → reduce CCS%.

        Baseline: media de los `baseline_years` años anteriores con completitud >= 50%.
        Landsat 8 disponible desde 2013 → baseline válido desde temporadas 2019+ aprox.
        """
        current = self.compute_period_lst(season_year)
        if current["mean_lst_c"] is None or current["completeness_pct"] < 30:
            logger.info("LST %s %d: datos insuficientes (comp=%.0f%%)",
                        self.country_key, season_year, current["completeness_pct"])
            return None

        baseline_vals = []
        for offset in range(1, baseline_years + 3):
            if len(baseline_vals) >= baseline_years:
                break
            yr     = season_year - offset
            period = self.compute_period_lst(yr)
            if period["completeness_pct"] >= 50 and period["mean_lst_c"] is not None:
                baseline_vals.append(period["mean_lst_c"])

        if not baseline_vals:
            logger.info("LST %s %d: sin baseline suficiente", self.country_key, season_year)
            return None

        baseline_mean = sum(baseline_vals) / len(baseline_vals)
        anomaly = round(current["mean_lst_c"] - baseline_mean, 3)

        logger.info(
            "LST %s %d: actual=%.2f°C  baseline=%.2f°C  anomalía=%+.2f°C  (n_base=%d)",
            self.country_key, season_year,
            current["mean_lst_c"], baseline_mean, anomaly, len(baseline_vals),
        )
        return anomaly
