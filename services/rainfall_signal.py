"""
Señal de anomalía de lluvia (SPI-90) via CHIRPS GEE.

SPI-90 (Standardized Precipitation Index, 90 días):
  z-score de la precipitación acumulada últimos 90 días vs baseline 5 años
  para el mismo período del año.

  SPI-90 < -1.0 → sequía moderada/severa → estrés hídrico caña → alcista SB
  SPI-90 > +2.0 → exceso hídrico → podredumbre raíces / retrasos logísticos
                   para azúcar esto es ambiguo — usamos neutral
  -1.0 ≤ SPI ≤ +2.0 → condiciones normales → neutral

Señal final: promedio ponderado de POIs con datos.
  Si India en sequía severa durante Jun-Sep (monzón) → señal más relevante.
"""
import logging
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

SPI_DROUGHT_THRESHOLD = -1.0   # sequía moderada o peor
SPI_WET_THRESHOLD     = +2.0   # exceso hídrico (umbral alto — ambiguo para caña)
SPI_STRONG_DROUGHT    = -1.5   # sequía severa → señal fuerte

_POI_WEIGHTS = {
    "br_sp_sugarcane":   1.0,
    "th_sugarcane_belt": 0.7,
    "in_up_maharashtra": 0.5,
}

# Meses críticos de lluvia por región (cuando la señal tiene más peso)
_CRITICAL_RAIN_MONTHS = {
    "br_sp_sugarcane":   [11, 12, 1, 2, 3],    # temporada húmeda SP
    "th_sugarcane_belt": [5, 6, 7, 8, 9, 10],  # monzón tailandés
    "in_up_maharashtra": [6, 7, 8, 9],          # monzón indio
}


def compute_rainfall_signal(session) -> dict:
    """
    Señal de anomalía de lluvia SPI-90 para BR + TH + IN.

    Returns dict:
      signal        : -1 / 0 / +1
      bias          : str
      score_weighted: float
      pois          : dict {poi_id: {spi90_z, precip_mm, signal, in_critical}}
      description   : str
    """
    base = {
        "signal": 0, "bias": "NEUTRAL",
        "score_weighted": 0.0,
        "pois": {},
        "description": "Rainfall SPI: sin datos GEE (ejecutar run_gee_crops.py)",
    }

    if session is None:
        return base

    try:
        from ingestion.gee_crops import get_latest_gee_metrics
        all_metrics = get_latest_gee_metrics(
            session, poi_ids=list(_POI_WEIGHTS.keys())
        )
    except Exception as e:
        logger.warning("rainfall_signal: %s", e)
        base["description"] = f"Rainfall SPI: error ({e})"
        return base

    if not all_metrics:
        return base

    today         = date.today()
    current_month = today.month
    weighted_sum  = 0.0
    total_weight  = 0.0
    poi_details   = {}
    active_pois   = []

    for poi_id, base_weight in _POI_WEIGHTS.items():
        poi_data = all_metrics.get(poi_id, {})
        spi_data = poi_data.get("spi90")
        if spi_data is None or spi_data.get("z_score") is None:
            continue

        spi_z      = spi_data["z_score"]
        precip_mm  = spi_data.get("value")
        in_critical = current_month in _CRITICAL_RAIN_MONTHS.get(poi_id, [])

        # Durante meses críticos de lluvia, el peso se amplifica
        weight = base_weight * (1.3 if in_critical else 1.0)

        if spi_z < SPI_DROUGHT_THRESHOLD:
            sig = +1   # sequía → menos producción → alcista
        else:
            sig = 0    # neutral (exceso de agua no genera señal clara)

        poi_details[poi_id] = {
            "signal":      sig,
            "spi90_z":     spi_z,
            "precip_mm":   precip_mm,
            "in_critical": in_critical,
            "obs_date":    spi_data.get("obs_date"),
            "anomaly":     spi_data.get("anomaly", False),
            "drought":     spi_z < SPI_DROUGHT_THRESHOLD,
            "severe":      spi_z < SPI_STRONG_DROUGHT,
        }
        weighted_sum += sig * weight
        total_weight += weight
        active_pois.append(poi_id)

    if not active_pois:
        return base

    score_weighted = weighted_sum / total_weight if total_weight else 0.0

    if score_weighted > 0.3:
        signal, bias = +1, "LONG"
    else:
        signal, bias = 0, "NEUTRAL"

    drought_pois = [
        (pid, d["spi90_z"])
        for pid, d in poi_details.items()
        if d["drought"]
    ]
    desc = f"Rainfall SPI-90 score={score_weighted:+.2f} → {bias}"
    if drought_pois:
        drought_str = "  ".join(
            f"{pid.split('_')[0].upper()} SPI={z:+.2f}"
            for pid, z in drought_pois
        )
        desc += f"  Sequía: {drought_str}"

    return {
        "signal":         signal,
        "bias":           bias,
        "score_weighted": round(score_weighted, 3),
        "pois":           poi_details,
        "description":    desc,
    }
