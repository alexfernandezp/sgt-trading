"""
SugarcaneEstimator_Brazil — estimacion produccion azucar Brasil Centro-Sur via GEE.

Region: SP, MG (Triangulo Mineiro), GO, MS, MT (Centro-Sur)
Temporada cosecha: Abr-Nov
Calibracion: vs CONAB (informes historicos)

Particularidades Brasil Centro-Sur:
  - El ciclo de cosecha es inverso al nórdico (Abr-Nov vs Oct-Abr India/TH)
  - Dos tipos de caña: cana de ano (12 meses) y cana de ano e meio (18 meses)
    → el NDVI refleja caña recien plantada + caña en ratoon mezcladas
  - Meses Ago-Sep: NDVI cae por cosecha activa → no confundir con estrés
    → en la integral, Ago-Sep tienen peso reducido (cosecha != menor produccion)
  - La señal CONAB es muy robusta y frecuente (mensual) → GEE es complemento
    para las primeras semanas de temporada antes del primer informe CONAB

Nota: el estimador Brasil es el menos critico del sistema porque CONAB
cubre bien la temporada actual. Util principalmente para cross-check anticipado.
"""
from gee.engine import CropEstimator


class SugarcaneEstimator_Brazil(CropEstimator):
    """
    Estimador caña Brasil Centro-Sur.
    Pesos reducidos en meses de cosecha activa (Ago-Sep) donde NDVI cae
    por extraccion de biomasa, no por menor produccion.
    """

    def __init__(self):
        super().__init__("brazil")
        # Durante cosecha activa (Ago-Sep) el NDVI baja — no interpretar como menor yield
        self._month_weights = {
            4:  1.0,   # Abril: inicio temporada
            5:  1.2,   # Mayo: crecimiento
            6:  1.3,   # Junio: pico vegetativo antes cosecha masiva
            7:  1.2,   # Julio: pico cosecha empieza
            8:  0.7,   # Agosto: cosecha masiva → NDVI bajo ≠ baja produccion
            9:  0.7,   # Septiembre: idem
            10: 0.9,   # Octubre: cosecha final
            11: 0.8,   # Noviembre: cierre temporada
        }

    def compute_seasonal_integral(self, season_year: int) -> dict:
        """Integral NDVI ponderada con pesos reducidos en meses de cosecha activa."""
        from datetime import date
        import logging
        logger = logging.getLogger(__name__)

        today = date.today()
        season_months = self._get_season_months(season_year)
        monthly = {}
        weighted_integral = 0.0

        for year, month in season_months:
            if date(year, month, 1) > today:
                monthly["%d-%02d" % (year, month)] = None
                continue

            ndvi = self.compute_monthly_ndvi(year, month)
            monthly["%d-%02d" % (year, month)] = ndvi

            if ndvi is not None:
                w = self._month_weights.get(month, 1.0)
                weighted_integral += ndvi * w

        valid = [v for v in monthly.values() if v is not None]
        completeness = len(valid) / len(season_months) if season_months else 0.0

        logger.info(
            "GEE Brasil %d: integral_ponderada=%.3f  completitud=%.0f%%",
            season_year, weighted_integral, completeness * 100,
        )

        return {
            "integral":         weighted_integral,
            "monthly_ndvi":     monthly,
            "completeness_pct": round(completeness * 100, 1),
            "n_months":         len(season_months),
            "n_valid":          len(valid),
        }
