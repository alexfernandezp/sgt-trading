"""
Señal de estrés del cultivo via LST (calor) + NDWI (agua) GEE.

Métricas:
  LST  — temperatura superficial terrestre MODIS MOD11A2 (°C)
         z > +1.5 → calor anómalo → estrés térmico caña → alcista SB
  NDWI — índice agua en vegetación Sentinel-2 (NIR-SWIR)/(NIR+SWIR)
         z < -1.5 → déficit agua en planta → estrés hídrico → alcista SB

Lógica de combinación:
  Ambos en estrés → señal fuerte (+1)
  Solo uno en estrés → señal moderada (se mantiene en +1 pero se anota)
  Ninguno en estrés → neutral (0)
  LST muy baja + NDWI alto → condiciones favorables → bajista (-1)

Señal final: promedio ponderado de POIs con datos.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

Z_STRESS_HEAT   = +1.5   # LST z > 1.5 → heat stress
Z_STRESS_WATER  = -1.5   # NDWI z < -1.5 → water stress
Z_FAVORABLE     = -1.2   # LST z < -1.2 y NDWI z > 1.2 → condiciones favorables

_POI_WEIGHTS = {
    "br_sp_sugarcane":   1.0,
    "th_sugarcane_belt": 0.7,
    "in_up_maharashtra": 0.5,
}


def _poi_stress_signal(lst_z: Optional[float], ndwi_z: Optional[float]) -> tuple:
    """
    Devuelve (signal, components) para un POI.
    signal: +1 estrés → alcista | -1 favorable → bajista | 0 neutral
    """
    heat_stress  = lst_z is not None  and lst_z  >  Z_STRESS_HEAT
    water_stress = ndwi_z is not None and ndwi_z < Z_STRESS_WATER
    favorable    = (lst_z is not None  and lst_z  < -Z_FAVORABLE and
                    ndwi_z is not None and ndwi_z >  Z_FAVORABLE)

    components = {
        "heat_stress":  heat_stress,
        "water_stress": water_stress,
        "favorable":    favorable,
        "lst_z":        lst_z,
        "ndwi_z":       ndwi_z,
    }

    if heat_stress or water_stress:
        signal = +1
    elif favorable:
        signal = -1
    else:
        signal = 0

    return signal, components


def compute_crop_stress_signal(session) -> dict:
    """
    Señal de estrés de cultivo combinada (LST + NDWI) para BR + TH + IN.

    Returns dict:
      signal        : -1 / 0 / +1
      bias          : str
      score_weighted: float
      pois          : dict {poi_id: {signal, lst_z, ndwi_z, heat_stress, water_stress}}
      description   : str
    """
    base = {
        "signal": 0, "bias": "NEUTRAL",
        "score_weighted": 0.0,
        "pois": {},
        "description": "Crop stress: sin datos GEE (ejecutar run_gee_crops.py)",
    }

    if session is None:
        return base

    try:
        from ingestion.gee_crops import get_latest_gee_metrics
        all_metrics = get_latest_gee_metrics(
            session, poi_ids=list(_POI_WEIGHTS.keys())
        )
    except Exception as e:
        logger.warning("crop_stress_signal: %s", e)
        base["description"] = f"Crop stress: error ({e})"
        return base

    if not all_metrics:
        return base

    weighted_sum = 0.0
    total_weight = 0.0
    poi_details  = {}
    active_pois  = []

    for poi_id, weight in _POI_WEIGHTS.items():
        poi_data = all_metrics.get(poi_id, {})
        lst      = poi_data.get("lst")
        ndwi     = poi_data.get("ndwi")

        lst_z  = lst["z_score"]  if lst  and lst.get("z_score")  is not None else None
        ndwi_z = ndwi["z_score"] if ndwi and ndwi.get("z_score") is not None else None

        if lst_z is None and ndwi_z is None:
            continue

        sig, comp = _poi_stress_signal(lst_z, ndwi_z)
        poi_details[poi_id] = {
            "signal":       sig,
            "lst_z":        lst_z,
            "ndwi_z":       ndwi_z,
            "lst_c":        lst["value"]  if lst  else None,
            "ndwi_val":     ndwi["value"] if ndwi else None,
            "heat_stress":  comp["heat_stress"],
            "water_stress": comp["water_stress"],
            "favorable":    comp["favorable"],
            "obs_date":     lst["obs_date"] if lst else (ndwi["obs_date"] if ndwi else None),
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

    stress_flags = [
        pid.split("_")[0].upper()
        for pid, d in poi_details.items()
        if d["heat_stress"] or d["water_stress"]
    ]
    fav_flags = [
        pid.split("_")[0].upper()
        for pid, d in poi_details.items()
        if d["favorable"]
    ]
    desc = f"Crop stress score={score_weighted:+.2f} → {bias}"
    if stress_flags:
        desc += f"  Estrés activo: {', '.join(stress_flags)}"
    if fav_flags:
        desc += f"  Favorable: {', '.join(fav_flags)}"

    return {
        "signal":         signal,
        "bias":           bias,
        "score_weighted": round(score_weighted, 3),
        "pois":           poi_details,
        "description":    desc,
    }
