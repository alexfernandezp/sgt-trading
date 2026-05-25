"""
Señal ENSO (El Niño/La Niña) para el modelo de azúcar ICE No.11.

Impacto histórico en producción de azúcar Brasil (CS):

  El Niño: reduce precipitación en São Paulo/Paraná durante la temporada
           húmeda (oct–mar). Cosechas históricamente afectadas:
           2002/03, 2009/10, 2015/16 → revisiones a la baja UNICA.
           Magnitud media: −3% a −8% en caña molida vs año anterior.

  La Niña: aumenta precipitación → mejor rendimiento de caña, pero
            también puede causar exceso de agua y reducir sacarosa
            en años muy húmedos.
            Cosechas favorecidas: 2007/08, 2010/11 (La Niña estándar).

Señal para futuros ICE SB:
  El Niño moderado/fuerte (ONI ≥ +0.5): restricción oferta → LONG SB
  La Niña moderada/fuerte (ONI ≤ −0.5): mayor oferta → SHORT SB

  El efecto es retardado: el impacto máximo aparece 4-9 meses
  después del pico del evento. La señal es de sesgo estacional.

Umbrales calibrados:
  ONI ≥ +1.0 → señal = +1 LONG (moderado/fuerte El Niño, impacto tangible)
  ONI en +0.5..+1.0 → señal = 0 neutral (débil El Niño, efecto incierto)
  ONI ≤ −1.0 → señal = −1 SHORT (moderada/fuerte La Niña, producción elevada)
  ONI en −1.0..−0.5 → señal = 0 neutral (débil La Niña)
  −0.5 < ONI < +0.5 → señal = 0 neutral (ENSO neutral)
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Umbrales de señal activa (ONI en °C)
NINO_BULLISH_THRESHOLD = 1.0   # El Niño moderado+: producción comprometida → LONG
NINA_BEARISH_THRESHOLD = -1.0  # La Niña moderada+: producción elevada → SHORT

# Percentil aproximado histórico para contexto de display
NINO_STRONG   = 1.5
NINA_STRONG   = -1.5


def compute_enso_signal(session) -> dict:
    """
    Lee el ONI más reciente de la DB y genera señal para azúcar.

    Returns dict:
      oni_value      : float
      season         : str (ej. 'DJF')
      year           : int
      obs_date       : str
      classification : str
      signal         : -1 / 0 / +1
      bias           : 'LONG' / 'SHORT' / 'NEUTRAL'
      lag_note       : str (contexto sobre retardo del impacto)
      description    : str
    """
    result = {
        "oni_value":      None,
        "season":         None,
        "year":           None,
        "obs_date":       None,
        "classification": None,
        "signal":         0,
        "bias":           "NEUTRAL",
        "lag_note":       "",
        "description":    "ENSO: sin datos ONI (ejecutar fetch_oni)",
    }

    if session is None:
        return result

    try:
        from ingestion.oni import get_latest_oni
        latest = get_latest_oni(session)
        if latest is None:
            return result

        oni   = latest["oni_value"]
        seas  = latest["season"]
        yr    = latest["year"]
        cls   = latest["classification"]
        dt    = latest["obs_date"]

        result["oni_value"]      = oni
        result["season"]         = seas
        result["year"]           = yr
        result["obs_date"]       = dt
        result["classification"] = cls

        # Señal y bias
        if oni >= NINO_BULLISH_THRESHOLD:
            signal = 1
            bias   = "LONG"
            intensity = "fuerte" if oni >= NINO_STRONG else "moderado"
            desc = (
                f"ENSO El Niño {intensity} (ONI={oni:+.2f}, {seas} {yr}) "
                f"→ déficit hídrico histórico SP/PR → restricción caña → LONG SB"
            )
            lag = "El efecto máximo en molienda aparece 4-9 meses tras pico ENSO."
        elif oni <= NINA_BEARISH_THRESHOLD:
            signal = -1
            bias   = "SHORT"
            intensity = "fuerte" if oni <= NINA_STRONG else "moderada"
            desc = (
                f"ENSO La Niña {intensity} (ONI={oni:+.2f}, {seas} {yr}) "
                f"→ lluvias favorables SP → producción elevada → SHORT SB"
            )
            lag = "Impacto en producción visible en cosecha siguiente (6-12 meses)."
        else:
            signal = 0
            bias   = "NEUTRAL"
            if 0.5 <= oni < NINO_BULLISH_THRESHOLD:
                desc = f"ENSO débil El Niño (ONI={oni:+.2f}, {seas} {yr}) — neutral para azúcar"
            elif NINA_BEARISH_THRESHOLD < oni <= -0.5:
                desc = f"ENSO débil La Niña (ONI={oni:+.2f}, {seas} {yr}) — neutral para azúcar"
            else:
                desc = f"ENSO neutral (ONI={oni:+.2f}, {seas} {yr}) — sin sesgo climático"
            lag = ""

        result["signal"]      = signal
        result["bias"]        = bias
        result["lag_note"]    = lag
        result["description"] = desc

    except Exception as e:
        logger.warning("compute_enso_signal: %s", e)
        result["description"] = f"ENSO: error ({e})"

    return result
