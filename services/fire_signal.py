"""
Señal INPE BDQueimadas — focos de incendio São Paulo + Paraná.

Lógica: focos actuales vs baseline estacional histórico (mismo mes, 5 años).
  z > +1.0  → anomalía positiva (más fuego de lo normal) → sequía / estrés
              hídrico confirmado → reduce rendimiento cosecha → alcista → +1 LONG
  z < -1.0  → condiciones más húmedas de lo normal → neutral → 0
  -1 ≤ z ≤ +1 → dentro de rango estacional → 0 neutral

Período de riesgo relevante: mayo–octubre (cosecha Centro-Sur Brasil).
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

Z_BULLISH_THRESHOLD = 1.0   # z > 1.0 → sequía significativa → LONG
Z_BEARISH_THRESHOLD = -1.5  # z < -1.5 → lluvia anómala → posible SHORT


def compute_fire_signal(session, state: str = "SP+PR") -> dict:
    """
    Señal de anomalía de focos de incendio INPE para la región dada.

    Returns dict con:
      signal       : +1 LONG / -1 SHORT / 0 neutral
      bias         : str
      z_score      : z-score actual vs baseline rolling 30 días
      current_total: focos del día
      baseline_mean: media rolling de días anteriores
      baseline_std : desviación estándar
      month        : mes actual (1-12)
      state        : región monitoreada
      description  : texto resumen
    """
    base = {
        "signal": 0, "bias": "NEUTRAL",
        "z_score": None, "current_total": None,
        "baseline_mean": None, "baseline_std": None,
        "month": None, "state": state,
        "description": f"INPE fuego {state}: sin datos",
    }

    if session is None:
        return base

    try:
        from ingestion.inpe_fires import get_fire_baseline
        stats = get_fire_baseline(session, state=state)
    except Exception as e:
        logger.warning("fire_signal: %s", e)
        base["description"] = f"INPE fuego {state}: error — {e}"
        return base

    if stats is None:
        base["description"] = f"INPE fuego {state}: datos insuficientes (<7 días acumulados)"
        return base

    z     = stats.get("z_score")
    curr  = stats.get("current_month_total")
    mean  = stats.get("baseline_mean")
    std   = stats.get("baseline_std")
    month = stats.get("month")

    if z is None:
        return base

    MONTH_NAMES = {5:"May", 6:"Jun", 7:"Jul", 8:"Ago", 9:"Sep", 10:"Oct",
                   11:"Nov", 12:"Dic", 1:"Ene", 2:"Feb", 3:"Mar", 4:"Abr"}
    month_s = MONTH_NAMES.get(month, str(month))

    if z > Z_BULLISH_THRESHOLD:
        signal = 1; bias = "LONG"
        desc = (f"Fuego {state} {month_s}: {curr} focos (z={z:+.2f}) — "
                f"anomalía positiva vs media {mean:.0f}±{std:.0f} "
                f"→ sequía/estrés hídrico → alcista azúcar")
    elif z < Z_BEARISH_THRESHOLD:
        signal = -1; bias = "SHORT"
        desc = (f"Fuego {state} {month_s}: {curr} focos (z={z:+.2f}) — "
                f"anomalía negativa vs media {mean:.0f}±{std:.0f} "
                f"→ condiciones húmedas → mayor rendimiento → bajista azúcar")
    else:
        signal = 0; bias = "NEUTRAL"
        desc = (f"Fuego {state} {month_s}: {curr} focos (z={z:+.2f}) — "
                f"dentro de rango estacional (media {mean:.0f}±{std:.0f})")

    return {
        "signal":        signal,
        "bias":          bias,
        "z_score":       z,
        "current_total": curr,
        "baseline_mean": mean,
        "baseline_std":  std,
        "month":         month,
        "state":         state,
        "description":   desc,
    }
