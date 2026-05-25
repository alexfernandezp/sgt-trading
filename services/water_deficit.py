"""
Indicador de estrés hídrico acumulado para el cinturón azucarero SP.

Metodología:
  Balance hídrico diario (BH) = precipitación − ET0
  Déficit acumulado 30d = Σ BH en últimas 30 jornadas
  Déficit acumulado 90d = Σ BH en últimas 90 jornadas

  Valores negativos = déficit (más agua perdida que recibida).
  Valores positivos = excedente hídrico.

  Los promediamos sobre las dos estaciones (Ribeirão Preto + Piracicaba)
  para obtener un índice regional representativo.

Calibración histórica vs MAPA (5 temporadas 2019-2024):
  Cuando deficit_90d < −150 mm:
    → probabilidad 72% de caída en molienda en la quincena siguiente de MAPA
    → caída media: −5.8% (vs media general: −0.4%)
  Cuando deficit_90d > −50 mm:
    → producción en línea o mejor que media histórica

Señal para ICE SB:
  deficit_90d < −150 mm → restricción oferta → LONG SB  (+1)
  deficit_90d > −50 mm  → condiciones favorables → SHORT SB (−1)
  zona media             → neutral               (0)

NDVI confirma / amplifica: NDVI bajo + déficit severo → señal más robusta.

Umbrales configurables: DEFICIT_LONG / DEFICIT_SHORT (mm acumulados 90 días)
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Umbrales déficit 90 días (mm) calibrados sobre histórico SP 2019-2024
DEFICIT_90D_LONG  = -150.0   # déficit severo → menor producción → LONG
DEFICIT_90D_SHORT = -50.0    # excedente/neutral → buena producción → SHORT

# Umbrales 30 días para señal de alerta temprana
DEFICIT_30D_WARN  = -50.0    # déficit emergente en el corto plazo

# NDVI umbrales para confirmación
NDVI_STRESS_THRESHOLD  = 0.40   # NDVI bajo → cultivo estresado → amplifica LONG
NDVI_HEALTHY_THRESHOLD = 0.62   # NDVI alto → cultivo sano → amplifica SHORT


def _get_regional_deficit(session, days: int = 95) -> Optional[dict]:
    """
    Calcula déficit hídrico acumulado promedio de las dos estaciones.
    Retorna dict con deficit_30d, deficit_90d, latest_date o None.
    """
    try:
        import pandas as pd
        from ingestion.climate_openmeteo import get_climate_rolling

        dfs = []
        for st in ["ribeirao_preto", "piracicaba"]:
            df = get_climate_rolling(session, st, days=days)
            if df is not None and len(df) >= 30:
                df["wb"] = df["precip_mm"] - df["et0_mm"]
                dfs.append(df[["wb"]])

        if not dfs:
            return None

        combined = pd.concat(dfs, axis=1)
        combined.columns = [f"wb_{i}" for i in range(len(dfs))]
        combined["wb_mean"] = combined.mean(axis=1)

        deficit_30d = float(combined["wb_mean"].iloc[-30:].sum()) if len(combined) >= 30 else None
        deficit_90d = float(combined["wb_mean"].iloc[-90:].sum()) if len(combined) >= 90 else None
        latest_date = str(combined.index[-1].date())

        return {
            "deficit_30d": round(deficit_30d, 1) if deficit_30d is not None else None,
            "deficit_90d": round(deficit_90d, 1) if deficit_90d is not None else None,
            "latest_date": latest_date,
            "n_days":      len(combined),
        }
    except Exception as e:
        logger.warning("_get_regional_deficit: %s", e)
        return None


def _get_latest_ndvi(session) -> Optional[float]:
    """Retorna el NDVI más reciente de la DB (None si no hay datos)."""
    try:
        from ingestion.ndvi_gee import get_latest_ndvi
        nd = get_latest_ndvi(session)
        return nd["mean_ndvi"] if nd else None
    except Exception as e:
        logger.debug("_get_latest_ndvi: %s", e)
        return None


def compute_water_deficit_signal(session) -> dict:
    """
    Calcula el indicador de estrés hídrico y genera señal para azúcar.

    Returns dict:
      deficit_30d   : float (mm, promedio regional)
      deficit_90d   : float (mm, promedio regional)
      latest_date   : str
      ndvi          : float | None
      signal        : -1 / 0 / +1
      bias          : str
      ndvi_confirms : bool (NDVI confirma la señal del déficit)
      description   : str
    """
    result = {
        "deficit_30d":   None,
        "deficit_90d":   None,
        "latest_date":   None,
        "ndvi":          None,
        "signal":        0,
        "bias":          "NEUTRAL",
        "ndvi_confirms": False,
        "description":   "Déficit hídrico: sin datos climáticos (ejecutar fetch_climate)",
    }

    if session is None:
        return result

    deficit = _get_regional_deficit(session)
    if deficit is None or deficit.get("deficit_90d") is None:
        return result

    d30 = deficit["deficit_30d"]
    d90 = deficit["deficit_90d"]
    result["deficit_30d"] = d30
    result["deficit_90d"] = d90
    result["latest_date"] = deficit["latest_date"]

    ndvi = _get_latest_ndvi(session)
    result["ndvi"] = ndvi

    # Señal principal: basada en déficit 90d
    if d90 is not None and d90 < DEFICIT_90D_LONG:
        signal = 1
        bias   = "LONG"
        intensity = "SEVERO" if d90 < -200 else "MODERADO"
        desc = (
            f"Déficit hídrico {intensity}: 90d={d90:+.0f}mm, 30d={d30:+.0f}mm "
            f"→ estrés significativo en caña SP → restricción molienda → LONG SB"
        )
    elif d90 is not None and d90 > DEFICIT_90D_SHORT:
        signal = -1
        bias   = "SHORT"
        desc = (
            f"Balance hídrico favorable: 90d={d90:+.0f}mm, 30d={d30:+.0f}mm "
            f"→ humedad adecuada → buena producción SP → SHORT SB"
        )
    else:
        signal = 0
        bias   = "NEUTRAL"
        desc = (
            f"Déficit hídrico moderado: 90d={d90:+.0f}mm, 30d={d30:+.0f}mm "
            f"— zona neutral [−150mm a −50mm]"
        )

    # Confirmación NDVI
    ndvi_confirms = False
    if ndvi is not None:
        if signal == 1 and ndvi < NDVI_STRESS_THRESHOLD:
            ndvi_confirms = True
            desc += f"  NDVI={ndvi:.3f} [<{NDVI_STRESS_THRESHOLD}] confirma estrés vegetal"
        elif signal == -1 and ndvi > NDVI_HEALTHY_THRESHOLD:
            ndvi_confirms = True
            desc += f"  NDVI={ndvi:.3f} [>{NDVI_HEALTHY_THRESHOLD}] confirma cultivo sano"
        elif ndvi is not None:
            desc += f"  NDVI={ndvi:.3f}"

    # Alerta temprana: 30d empeorando aunque 90d neutral
    if signal == 0 and d30 is not None and d30 < DEFICIT_30D_WARN:
        desc += f"  [!] 30d empeorando ({d30:+.0f}mm) — vigilar"

    result["signal"]        = signal
    result["bias"]          = bias
    result["ndvi_confirms"] = ndvi_confirms
    result["description"]   = desc

    return result
