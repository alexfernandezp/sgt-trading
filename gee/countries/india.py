"""
SugarcaneEstimator_India — estimacion produccion azucar India via GEE.

Region: UP (~45% produccion) + Maharashtra (~25%) + Karnataka (~10%)
Temporada: Oct-Abr (crushing season)
Calibracion: vs ISMA historical (datos ya netos de etanol)

Particularidades India:
  - Caña plurianual: NDVI persistente incluso bajo estrés → señal NDVI plana (4.55-4.72)
  - El CCS% (contenido azúcar) cae con calor sostenido >35°C en período de crecimiento (Apr-Sep)
    → LST Landsat captura este estrés; integrado en _get_season_stress_factors()
  - Meses Jun-Ago: monzón, nubosidad alta → LST solo disponible en Apr-May y Sep
  - Los meses de pico NDVI (Ene-Mar) tienen peso 1.5x en la integral estacional
  - El dato ISMA ya es neto de diversion etanol — no aplicar factor adicional

Arquitectura stress:
  _get_season_stress_factors() → llama LSTStressEstimator → anomalía °C vs baseline
  _compute_estimate()          → aplica penalty = k_lst × max(0, anomalía)
  k_lst en config stress_coefficients.lst  (null hasta calibración)
"""
from typing import Optional
from gee.engine import CropEstimator, load_country_config


class SugarcaneEstimator_India(CropEstimator):
    """
    Estimador caña India con integral NDVI ponderada + stress LST (Landsat).
    Los meses de peak (Ene-Mar) tienen peso 1.5x — mayor correlacion con produccion.
    El stress térmico (Apr-Sep) penaliza la estimación cuando k_lst está calibrado.
    """

    def __init__(self):
        super().__init__("india")
        self._month_weights = {
            10: 1.0, 11: 1.0, 12: 1.0,
            1:  1.5, 2:  1.5, 3:  1.5,
            4:  1.2,
        }
        self._lst_estimator  = None   # lazy init
        self._spi_estimator  = None   # lazy init
        self._price_checked  = False  # fetch once per instance

    # ── LST stress ────────────────────────────────────────────────────────────

    def _get_lst_estimator(self):
        if self._lst_estimator is None:
            from gee.lst_stress import LSTStressEstimator
            self._lst_estimator = LSTStressEstimator(
                "india", self.config, geometry=self._geometry
            )
        return self._lst_estimator

    # ── SPI stress ────────────────────────────────────────────────────────────

    def _get_spi_estimator(self):
        if self._spi_estimator is None:
            from gee.spi_stress import SPIStressEstimator
            self._spi_estimator = SPIStressEstimator("india", self.config)
        return self._spi_estimator

    # ── Stress combinado ──────────────────────────────────────────────────────

    def _get_season_stress_factors(self, season_year: int) -> dict:
        """
        Recoge anomalía LST (Sep-Nov) y SPI monzón (Jun-Sep) para la temporada.
        Retorna {} si ambos fallan. Valores None = variable sin calibrar aún.
        """
        import logging
        _log = logging.getLogger(__name__)
        result: dict = {}

        try:
            lst_est = self._get_lst_estimator()
            anomaly = lst_est.compute_lst_anomaly(season_year)
            if anomaly is not None:
                result["lst_anomaly_c"] = anomaly
        except Exception as e:
            _log.warning("India LST stress %d: %s", season_year, e)

        try:
            spi_est = self._get_spi_estimator()
            spi = spi_est.compute_spi(season_year)
            if spi is not None:
                result["spi"] = spi
        except Exception as e:
            _log.warning("India SPI stress %d: %s", season_year, e)

        try:
            from ingestion.india_sugar_price import get_latest_exmill_price
            from ingestion.india_ethanol import compute_parity_ratio
            price_pt = get_latest_exmill_price()
            if price_pt is not None:
                ratio = compute_parity_ratio(price_pt.price_rs_kg)
                result["parity_ratio"] = round(ratio, 3)
                result["exmill_price_rs_kg"] = round(price_pt.price_rs_kg, 2)
                _log.info(
                    "India parity %d: exmill=₹%.1f/kg  ratio=%.3f  (%s)",
                    season_year, price_pt.price_rs_kg, ratio,
                    "ethanol preferred" if ratio > 1.0 else "sugar preferred",
                )
        except Exception as e:
            _log.debug("India parity ratio %d: %s", season_year, e)

        return result

    def _compute_estimate(self, area_ha: float, ndvi_ratio: float,
                          stress: Optional[dict] = None) -> float:
        """
        Estimación base + penalizaciones LST y SPI (cuando k_lst/k_spi calibrados).
        Cada penalty individual tiene su cap; el total nunca supera 35%.
        """
        base_yield = self.config["base_sugar_yield_t_per_ha"]
        est = (area_ha * base_yield * ndvi_ratio) / 1_000_000

        if not stress:
            return est

        coefs   = self.config.get("stress_coefficients") or {}
        penalty = 0.0

        k_lst = coefs.get("lst")
        if k_lst is not None:
            anomaly = stress.get("lst_anomaly_c", 0.0) or 0.0
            penalty += min(0.20, k_lst * max(0.0, anomaly))   # calor → reduce CCS%

        k_spi = coefs.get("spi")
        if k_spi is not None:
            spi = stress.get("spi", 0.0) or 0.0
            penalty += min(0.25, k_spi * max(0.0, -spi))      # déficit → reduce tonelaje

        # parity_ratio: cuando > 1.0 mills prefieren etanol — señal de mayor desvío
        # k_parity pendiente calibración vs closing_stock y diversion real
        k_parity = coefs.get("parity")
        if k_parity is not None:
            ratio = stress.get("parity_ratio", 1.0) or 1.0
            penalty += min(0.15, k_parity * max(0.0, ratio - 1.0))

        total_penalty = min(0.35, penalty)
        return round(est * (1.0 - total_penalty), 4)

    # ── NDVI integral ponderada ───────────────────────────────────────────────

    def compute_seasonal_integral(self, season_year: int) -> dict:
        """
        Integral NDVI ponderada por mes.
        Los meses de cosecha (Ene-Mar) tienen peso mayor.
        """
        from datetime import date

        today = date.today()
        season_months = self._get_season_months(season_year)
        monthly = {}
        weighted_integral = 0.0
        weight_sum = 0.0

        for year, month in season_months:
            if date(year, month, 1) > today:
                monthly["%d-%02d" % (year, month)] = None
                continue

            ndvi = self.compute_monthly_ndvi(year, month)
            monthly["%d-%02d" % (year, month)] = ndvi

            if ndvi is not None:
                w = self._month_weights.get(month, 1.0)
                weighted_integral += ndvi * w
                weight_sum += w

        # Normalizar por suma de pesos para comparabilidad con baseline
        # (baseline tambien se computa con esta misma funcion)
        valid = [v for v in monthly.values() if v is not None]
        completeness = len(valid) / len(season_months) if season_months else 0.0

        import logging
        logger = logging.getLogger(__name__)
        logger.info(
            "GEE India %d: integral_ponderada=%.3f  completitud=%.0f%%",
            season_year, weighted_integral, completeness * 100,
        )

        return {
            "integral":          weighted_integral,
            "monthly_ndvi":      monthly,
            "completeness_pct":  round(completeness * 100, 1),
            "n_months":          len(season_months),
            "n_valid":           len(valid),
        }
