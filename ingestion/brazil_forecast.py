"""
Brazil CS next-quinzena forecast model.

Sub-modelos:
  1. Cane   — pace ratio: (actual_cum / hist_cum) × hist_Q_next, con shrinkage
              por temporada temprana y clamp ±30%
  2. Mix%   — trend continuation: hist_Q_next + anomaly × decay (60%)
  3. ATR    — ratio secuencial: actual_last × (hist_Q_next / hist_Q_last)
  4. Sugar  — derivado: cane_forecast × hist_sugar_cane_ratio,
              ajustado por anomalía de eficiencia (decay 50%)

Shrinkage del pace: a Q2 solo hay 2 periodos => incertidumbre alta.
El shrinkage escala de 0.33 (Q2) a 1.0 (Q6+) para evitar sobreacción
a anomalías de inicio de zafra.

Confidence levels:
  low    n <= 2  (menos de 2 quincenas publicadas)
  medium n <= 5
  high   n > 5
"""
from typing import Optional
import logging

logger = logging.getLogger(__name__)

_MIX_DECAY  = 0.60   # 60% de la anomalía continúa a la quincena siguiente
_SC_DECAY   = 0.50   # 50% de la anomalía sugar/cane continúa
_PACE_CLAMP = 0.30   # pace máx ±30% respecto a histórico

_Q_LABELS = [
    'Apr-16', 'May-01', 'May-16', 'Jun-01', 'Jun-16', 'Jul-01', 'Jul-16', 'Aug-01',
    'Aug-16', 'Sep-01', 'Sep-16', 'Oct-01', 'Oct-16', 'Nov-01', 'Nov-16', 'Dec-01',
    'Dec-16', 'Jan-01', 'Jan-16', 'Feb-01', 'Feb-16', 'Mar-01', 'Mar-16', 'Apr-01',
]


def _shrinkage(n: int) -> float:
    """Escala 0→1 conforme aumentan los periodos publicados.
    A n=2: 0.33 (señal débil). A n>=6: 1.0 (señal fiable)."""
    return min(1.0, n / 6.0)


def forecast_next_quinzena(
    current_qs: list,
    hist_cane_avg: list,
    hist_sugar_avg: list,
    hist_mix_avg: list,
    hist_mix_p25: list,
    hist_mix_p75: list,
    hist_atr_avg: list,
    hist_atr_p25: list,
    hist_atr_p75: list,
) -> Optional[dict]:
    """
    Devuelve dict con forecast de la próxima quincena, o None si no hay datos.

    current_qs: lista de dicts con claves cane, sugar, mix, atr
                (una entrada por quincena publicada, orden cronológico)
    hist_*:     listas 24-elem (índice 0=Q1) de valores históricos
    """
    n = len(current_qs)
    if n == 0 or n >= 24:
        return None

    qi = n          # índice 0-based de la próxima quincena
    shrink = _shrinkage(n)

    # Guardia: necesitamos hist_cane_avg para el periodo siguiente
    if qi >= len(hist_cane_avg) or not hist_cane_avg[qi]:
        return None

    last = current_qs[-1]
    last_cane  = last.get("cane")
    last_sugar = last.get("sugar")
    last_mix   = last.get("mix")
    last_atr   = last.get("atr")

    # ── 1. Cane — pace ratio con shrinkage ───────────────────────────────────
    actual_cum = sum(q["cane"] for q in current_qs if q.get("cane"))
    hist_cum   = sum(v for v in hist_cane_avg[:n] if v)

    raw_pace = actual_cum / hist_cum if hist_cum else 1.0
    # Shrinkage hacia 1.0 en temporada temprana
    pace = 1.0 + (raw_pace - 1.0) * shrink
    # Clamp ±30%
    pace = max(1 - _PACE_CLAMP, min(1 + _PACE_CLAMP, pace))

    cane_f = round(pace * hist_cane_avg[qi], 1)

    # ── 2. Mix% — trend continuation con decay ───────────────────────────────
    h_last_mix = hist_mix_avg[n - 1] if (n - 1) < len(hist_mix_avg) else None
    h_next_mix = hist_mix_avg[qi]    if qi < len(hist_mix_avg) else None

    if last_mix and h_last_mix and h_next_mix:
        anomaly  = (last_mix - h_last_mix) * shrink
        mix_f    = round(max(10.0, min(70.0, h_next_mix + anomaly * _MIX_DECAY)), 1)
        shift    = mix_f - h_next_mix
        mix_p25  = round((hist_mix_p25[qi] or h_next_mix) + shift, 1) if qi < len(hist_mix_p25) else None
        mix_p75  = round((hist_mix_p75[qi] or h_next_mix) + shift, 1) if qi < len(hist_mix_p75) else None
    else:
        mix_f   = h_next_mix
        mix_p25 = hist_mix_p25[qi] if qi < len(hist_mix_p25) else None
        mix_p75 = hist_mix_p75[qi] if qi < len(hist_mix_p75) else None

    # ── 3. ATR — ratio secuencial ─────────────────────────────────────────────
    h_last_atr = hist_atr_avg[n - 1] if (n - 1) < len(hist_atr_avg) else None
    h_next_atr = hist_atr_avg[qi]    if qi < len(hist_atr_avg) else None

    if last_atr and h_last_atr and h_next_atr and h_last_atr > 0:
        ratio   = h_next_atr / h_last_atr          # ratio histórico Q_next/Q_last
        atr_f   = round(last_atr * ratio, 1)
        shift   = atr_f - h_next_atr
        atr_p25 = round((hist_atr_p25[qi] or h_next_atr) + shift, 1) if qi < len(hist_atr_p25) else None
        atr_p75 = round((hist_atr_p75[qi] or h_next_atr) + shift, 1) if qi < len(hist_atr_p75) else None
    else:
        atr_f   = h_next_atr
        atr_p25 = hist_atr_p25[qi] if qi < len(hist_atr_p25) else None
        atr_p75 = hist_atr_p75[qi] if qi < len(hist_atr_p75) else None

    # ── 4. Sugar — derivado de cane + eficiencia ──────────────────────────────
    h_next_sugar = hist_sugar_avg[qi] if qi < len(hist_sugar_avg) else None
    h_next_cane  = hist_cane_avg[qi]

    sugar_f = None
    if h_next_sugar and h_next_cane and h_next_cane > 0:
        hist_sc = h_next_sugar / h_next_cane  # ratio histórico sugar/cane Q_next

        if last_sugar and last_cane and last_cane > 0:
            h_last_sugar = hist_sugar_avg[n - 1] if (n - 1) < len(hist_sugar_avg) else None
            h_last_cane  = hist_cane_avg[n - 1]  if (n - 1) < len(hist_cane_avg)  else None
            if h_last_sugar and h_last_cane and h_last_cane > 0:
                act_sc    = last_sugar / last_cane
                baseline  = h_last_sugar / h_last_cane
                sc_ratio  = 1.0 + (act_sc / baseline - 1.0) * _SC_DECAY * shrink
                sugar_f   = round(cane_f * hist_sc * sc_ratio, 3)
            else:
                sugar_f = round(cane_f * hist_sc, 3)
        else:
            sugar_f = round(cane_f * hist_sc, 3)

    conf = "low" if n <= 2 else ("medium" if n <= 5 else "high")

    return {
        "q_num":          qi + 1,
        "label":          _Q_LABELS[qi] if qi < len(_Q_LABELS) else f"Q{qi+1}",
        "method":         "pace+trend+ratio",
        "confidence":     conf,
        "n_periods_in":   n,
        "pace_ratio":     round(raw_pace, 3),
        "pace_effective": round(pace, 3),
        "shrinkage":      round(shrink, 2),
        # Cane
        "cane_forecast":  cane_f,
        "cane_avg":       round(hist_cane_avg[qi], 1) if hist_cane_avg[qi] else None,
        # Sugar
        "sugar_forecast": sugar_f,
        "sugar_avg":      round(h_next_sugar, 3) if h_next_sugar else None,
        # Mix%
        "mix_forecast":   mix_f,
        "mix_p25":        mix_p25,
        "mix_p75":        mix_p75,
        "mix_avg":        h_next_mix,
        # ATR
        "atr_forecast":   atr_f,
        "atr_p25":        atr_p25,
        "atr_p75":        atr_p75,
        "atr_avg":        h_next_atr,
    }
