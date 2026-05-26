"""
Señal de ritmo de cosecha (Harvest Pace) via NDVI GEE.

Lógica por temporada:
  TEMPORADA DE COSECHA (harvest_months del POI):
    NDVI < baseline (z < -1.0) → cosecha avanzando más rápido de lo normal
                                  → más oferta a corto plazo → bajista SB
    NDVI > baseline (z > +1.0) → cosecha más lenta de lo normal
                                  → menos oferta disponible → alcista SB

  TEMPORADA DE CRECIMIENTO (meses fuera de harvest_months):
    NDVI < baseline (z < -1.0) → cultivo con estrés / menos biomasa
                                  → menor producción futura → alcista SB
    NDVI > baseline (z > +1.0) → cultivo excepcionalmente sano
                                  → mayor producción futura → bajista SB

Señal final: promedio ponderado de los POIs activos (peso en gee_pois.json).
  signal ∈ {-1, 0, +1}  |  bias ∈ {LONG, SHORT, NEUTRAL}

Requiere: tabla gee_metric poblada por scripts/run_gee_crops.py.
"""
import logging
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

# Umbrales z-score para señal activa
Z_SIGNAL   = 1.0    # |z| > 1.0 → señal
Z_STRONG   = 1.8    # |z| > 1.8 → señal fuerte (convicción)

# POI config (coincide con config/gee_pois.json)
_POI_HARVEST_MONTHS = {
    "br_sp_sugarcane":  [4, 5, 6, 7, 8, 9, 10, 11],
    "th_sugarcane_belt": [11, 12, 1, 2, 3],
    "in_up_maharashtra": [10, 11, 12, 1, 2, 3],
}
_POI_WEIGHTS = {
    "br_sp_sugarcane":   1.0,
    "th_sugarcane_belt": 0.7,
    "in_up_maharashtra": 0.5,
}


def _poi_signal(poi_id: str, z: float, current_month: int) -> int:
    """Señal para un POI dado su z-score NDVI y mes actual."""
    harvest_months = _POI_HARVEST_MONTHS.get(poi_id, [])
    in_harvest     = current_month in harvest_months

    if abs(z) < Z_SIGNAL:
        return 0

    if in_harvest:
        # Cosecha: NDVI bajo = cosecha rápida = más oferta = bajista
        return -1 if z < 0 else +1
    else:
        # Crecimiento: NDVI bajo = estrés = menos producción futura = alcista
        return +1 if z < 0 else -1


def compute_harvest_pace_signal(session) -> dict:
    """
    Señal combinada de harvest pace para BR + TH + IN.

    Returns dict:
      signal        : -1 / 0 / +1
      bias          : str
      score_weighted: float (promedio ponderado antes de redondear)
      pois          : dict {poi_id: {z_score, signal, in_harvest, obs_date}}
      description   : str
    """
    base = {
        "signal": 0, "bias": "NEUTRAL",
        "score_weighted": 0.0,
        "pois": {},
        "description": "Harvest pace: sin datos GEE (ejecutar run_gee_crops.py)",
    }

    if session is None:
        return base

    try:
        from ingestion.gee_crops import get_latest_gee_metrics
        all_metrics = get_latest_gee_metrics(
            session, poi_ids=list(_POI_HARVEST_MONTHS.keys())
        )
    except Exception as e:
        logger.warning("harvest_pace_signal: %s", e)
        base["description"] = f"Harvest pace: error ({e})"
        return base

    if not all_metrics:
        return base

    today         = date.today()
    current_month = today.month
    weighted_sum  = 0.0
    total_weight  = 0.0
    poi_details   = {}
    active_pois   = []

    for poi_id, weight in _POI_WEIGHTS.items():
        poi_data = all_metrics.get(poi_id, {})
        ndvi     = poi_data.get("ndvi")
        if ndvi is None or ndvi.get("z_score") is None:
            continue

        z             = ndvi["z_score"]
        sig           = _poi_signal(poi_id, z, current_month)
        in_harvest    = current_month in _POI_HARVEST_MONTHS.get(poi_id, [])

        poi_details[poi_id] = {
            "z_score":   z,
            "value":     ndvi.get("value"),
            "signal":    sig,
            "in_harvest": in_harvest,
            "obs_date":  ndvi.get("obs_date"),
            "anomaly":   ndvi.get("anomaly", False),
        }
        weighted_sum += sig * weight
        total_weight += weight
        active_pois.append(poi_id)

    if not active_pois:
        return base

    score_weighted = weighted_sum / total_weight if total_weight else 0.0

    if score_weighted > 0.35:
        signal, bias = +1, "LONG"
    elif score_weighted < -0.35:
        signal, bias = -1, "SHORT"
    else:
        signal, bias = 0, "NEUTRAL"

    # Descripción
    poi_summaries = []
    for pid, d in poi_details.items():
        season = "cosecha" if d["in_harvest"] else "crecimiento"
        poi_summaries.append(
            f"{pid.split('_')[0].upper()} z={d['z_score']:+.2f} "
            f"[{season}] {'⚠' if d['anomaly'] else ''}"
        )
    desc = (f"Harvest pace ({', '.join(active_pois)}): "
            f"score={score_weighted:+.2f} → {bias}  |  "
            + "  ".join(poi_summaries))

    return {
        "signal":         signal,
        "bias":           bias,
        "score_weighted": round(score_weighted, 3),
        "pois":           poi_details,
        "description":    desc,
    }
