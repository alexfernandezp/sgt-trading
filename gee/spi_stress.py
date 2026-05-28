"""
SPIStressEstimator — índice de precipitación estandarizado (SPI) via Open-Meteo ERA5.

Por qué SPI para India:
  La caña es perenne → NDVI plano incluso con estrés hídrico. El monzón
  (Jun-Sep) determina el tonelaje de caña del año siguiente: déficit de
  lluvia → menor biomasa → menor molienda. El LST India no captura este
  efecto; el SPI sí.

Metodología:
  1. Precipitación mensual via Open-Meteo ERA5 archive (gratis, lag ~5d).
     Media ponderada de 5 puntos en cinturón cañero India (UP/MH/KA).
  2. Total Jun-Sep (temporada monzón) para cada año.
  3. SPI = (actual - baseline_mean) / baseline_std (n_base >= baseline_years).
     SPI < 0 → déficit. SPI < -1 → moderado. SPI < -2 → severo.
  4. Integración: penalty = k_spi × max(0, -SPI) en _compute_estimate().
     k_spi se calibra cuando k_lst ya está fijo.

ERA5 cubre desde 1940. Útil para histórico y temporada en curso.
"""
import json
import logging
import os
import calendar
from datetime import date
from typing import Optional

import requests

logger = logging.getLogger(__name__)

ERA5_URL = "https://archive-api.open-meteo.com/v1/archive"

_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "gee_cache",
)
_CACHE_TTL_RECENT_DAYS    = 7
_CACHE_STABLE_THRESHOLD_D = 60

# Cinturón cañero India: UP (~45% producción), MH (~25%), KA (~10%)
_INDIA_STATIONS = [
    {"name": "up_muzaffarnagar", "lat": 29.47, "lon": 77.71, "weight": 0.25},
    {"name": "up_lucknow",       "lat": 26.85, "lon": 80.94, "weight": 0.20},
    {"name": "mh_pune",          "lat": 18.52, "lon": 73.86, "weight": 0.25},
    {"name": "mh_solapur",       "lat": 17.67, "lon": 75.91, "weight": 0.15},
    {"name": "ka_belagavi",      "lat": 15.85, "lon": 74.50, "weight": 0.15},
]

_STATIONS_BY_COUNTRY: dict[str, list] = {
    "india": _INDIA_STATIONS,
}


class SPIStressEstimator:
    """
    SPI del monzón para estimar impacto hídrico en producción de caña.
    Usa Open-Meteo ERA5 archive (gratuito, sin API key, lag ~5 días).
    """

    def __init__(self, country_key: str, config: dict):
        self.country_key = country_key
        self.config      = config
        self._stations   = _STATIONS_BY_COUNTRY.get(country_key, [])

    # ── Cache de disco ─────────────────────────────────────────────────────────

    def _cache_path(self, year: int, month: int) -> str:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        return os.path.join(
            _CACHE_DIR, "%s_spi_%d_%02d.json" % (self.country_key, year, month)
        )

    def _month_is_stable(self, year: int, month: int) -> bool:
        last_day = calendar.monthrange(year, month)[1]
        return (date.today() - date(year, month, last_day)).days > _CACHE_STABLE_THRESHOLD_D

    def _read_cache(self, year: int, month: int) -> tuple[bool, Optional[float]]:
        path = self._cache_path(year, month)
        if not os.path.exists(path):
            return False, None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if self._month_is_stable(year, month):
                return True, data.get("precip_mm")
            cached_at = data.get("cached_at")
            if cached_at:
                age = (date.today() - date.fromisoformat(cached_at)).days
                if age <= _CACHE_TTL_RECENT_DAYS:
                    return True, data.get("precip_mm")
        except Exception:
            pass
        return False, None

    def _write_cache(self, year: int, month: int, precip_mm: Optional[float]):
        try:
            with open(self._cache_path(year, month), "w", encoding="utf-8") as f:
                json.dump(
                    {"precip_mm": precip_mm, "cached_at": date.today().isoformat()}, f
                )
        except Exception as e:
            logger.debug(
                "SPI cache write %s %d-%02d: %s", self.country_key, year, month, e
            )

    # ── ERA5 fetch ─────────────────────────────────────────────────────────────

    def _fetch_monthly_precip_station(
        self, lat: float, lon: float, year: int, month: int
    ) -> Optional[float]:
        """Precipitación total mensual (mm) en un punto via ERA5 Open-Meteo."""
        last_day  = calendar.monthrange(year, month)[1]
        start_str = "%d-%02d-01" % (year, month)
        end_str   = "%d-%02d-%02d" % (year, month, last_day)
        params = {
            "latitude":   lat,
            "longitude":  lon,
            "start_date": start_str,
            "end_date":   end_str,
            "daily":      "precipitation_sum",
            "timezone":   "Asia/Kolkata",
        }
        try:
            resp = requests.get(
                ERA5_URL, params=params, timeout=30,
                headers={"User-Agent": "SGT-Trading/1.0"},
            )
            resp.raise_for_status()
            data  = resp.json()
            daily = data.get("daily", {})
            vals  = [v for v in (daily.get("precipitation_sum") or []) if v is not None]
            return round(sum(vals), 2) if vals else None
        except Exception as e:
            logger.warning(
                "SPI ERA5 fetch (%s %d-%02d lat=%.2f lon=%.2f): %s",
                self.country_key, year, month, lat, lon, e,
            )
            return None

    # ── Precipitación mensual ponderada ────────────────────────────────────────

    def compute_monthly_precip(self, year: int, month: int) -> Optional[float]:
        """
        Precipitación mensual total (mm) media ponderada sobre el cinturón cañero.
        Cacheado en disco. Retorna None si cobertura < 30% de peso total.
        """
        hit, cached = self._read_cache(year, month)
        if hit:
            return cached

        if not self._stations:
            logger.warning("SPI: no hay estaciones para %s", self.country_key)
            return None

        total_weight = 0.0
        weighted_sum = 0.0
        n_ok = 0
        for st in self._stations:
            val = self._fetch_monthly_precip_station(st["lat"], st["lon"], year, month)
            if val is not None:
                weighted_sum += val * st["weight"]
                total_weight += st["weight"]
                n_ok += 1

        result = None
        if total_weight >= 0.3:
            result = round(weighted_sum / total_weight, 2)

        self._write_cache(year, month, result)
        logger.info(
            "SPI %s %d-%02d: %.1f mm  (n=%d/%d estaciones)",
            self.country_key, year, month,
            result if result is not None else -9999,
            n_ok, len(self._stations),
        )
        return result

    # ── Período monzón ─────────────────────────────────────────────────────────

    def _get_spi_month_pairs(self, season_year: int) -> list[tuple[int, int]]:
        """Devuelve (year, month) para los meses SPI de config."""
        sp     = self.config.get("stress_periods", {})
        months = sp.get("spi_months", [])
        spans  = sp.get("stress_spans_year", False)
        if not spans:
            return [(season_year, m) for m in sorted(months)]
        start = sp.get("stress_start_month", min(months) if months else 1)
        end   = sp.get("stress_end_month",   max(months) if months else 12)
        pairs  = [(season_year, m) for m in range(start, 13)]
        pairs += [(season_year + 1, m) for m in range(1, end + 1)]
        return pairs

    def compute_season_total_precip(self, season_year: int) -> dict:
        """Precipitación total (mm) del período monzón para la temporada dada."""
        today = date.today()
        pairs = self._get_spi_month_pairs(season_year)
        monthly: dict[str, Optional[float]] = {}

        for yr, mo in pairs:
            key = "%d-%02d" % (yr, mo)
            if date(yr, mo, 1) > today:
                monthly[key] = None
                continue
            monthly[key] = self.compute_monthly_precip(yr, mo)

        valid        = [v for v in monthly.values() if v is not None]
        total        = round(sum(valid), 2) if valid else None
        completeness = len(valid) / len(pairs) if pairs else 0.0

        return {
            "total_mm":         total,
            "monthly":          monthly,
            "completeness_pct": round(completeness * 100, 1),
            "n_valid":          len(valid),
            "n_months":         len(pairs),
        }

    # ── SPI ────────────────────────────────────────────────────────────────────

    def compute_spi(self, season_year: int, baseline_years: int = 5) -> Optional[float]:
        """
        SPI del monzón para la temporada dada.
        SPI = (actual_total_mm - baseline_mean_mm) / baseline_std_mm
        Negativo = déficit hídrico. Positivo = excedente.

        Requiere completitud >= 50% y baseline de >= baseline_years años.
        Retorna None si datos insuficientes.
        """
        current = self.compute_season_total_precip(season_year)
        if current["total_mm"] is None or current["completeness_pct"] < 50:
            logger.info(
                "SPI %s %d: datos insuficientes (comp=%.0f%%)",
                self.country_key, season_year, current["completeness_pct"],
            )
            return None

        baseline_totals: list[float] = []
        for offset in range(1, baseline_years + 3):
            if len(baseline_totals) >= baseline_years:
                break
            yr  = season_year - offset
            per = self.compute_season_total_precip(yr)
            if per["completeness_pct"] >= 50 and per["total_mm"] is not None:
                baseline_totals.append(per["total_mm"])

        if len(baseline_totals) < 2:
            logger.info(
                "SPI %s %d: baseline insuficiente (%d años)",
                self.country_key, season_year, len(baseline_totals),
            )
            return None

        n         = len(baseline_totals)
        mean_base = sum(baseline_totals) / n
        variance  = sum((v - mean_base) ** 2 for v in baseline_totals) / (n - 1)
        std_base  = variance ** 0.5

        if std_base < 1.0:
            logger.info(
                "SPI %s %d: std baseline trivial (%.1f mm)",
                self.country_key, season_year, std_base,
            )
            return None

        spi        = round((current["total_mm"] - mean_base) / std_base, 3)
        anomaly_mm = round(current["total_mm"] - mean_base, 1)

        logger.info(
            "SPI %s %d: actual=%.0fmm  baseline=%.0fmm  anomalía=%+.0fmm  SPI=%+.3f  (n_base=%d)",
            self.country_key, season_year,
            current["total_mm"], mean_base, anomaly_mm, spi, len(baseline_totals),
        )
        return spi
