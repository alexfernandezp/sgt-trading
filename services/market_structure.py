"""
Estructura de mercado multitemporal para L2 (intradía).

Timeframe stack:
  30m — contexto de sesion (~2h por swing con N=4)
  5m  — estructura de entrada (~30min por swing con N=3)
  1m  — posicion en barra y ratio ATR (momentum inmediato)

Senal L2-9:
  1 si ambos 30m y 5m alinean con la direccion del trade
  0 si al menos uno no alinea
  None si datos insuficientes en ambos
"""
import logging
import numpy as np
from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)

PIVOT_N       = {"30m": 4, "5m": 3}
MIN_SWING_PCT = {"30m": 0.003, "5m": 0.001}   # 0.3% y 0.1% del precio


def _fetch_bars(session: Session, instrument: str, interval: str, n: int) -> list:
    rows = session.execute(text("""
        SELECT datetime, high, low, close
        FROM price_bars
        WHERE instrument = :instr AND interval = :iv
        ORDER BY datetime DESC
        LIMIT :n
    """), {"instr": instrument, "iv": interval, "n": n}).fetchall()
    return [{"dt": r[0], "h": float(r[1] or 0), "l": float(r[2] or 0), "c": float(r[3] or 0)}
            for r in reversed(rows)]


def _pivot_highs_lows(bars: list, n: int) -> tuple:
    """Pivot high en i: bars[i].h es max de [i-n .. i+n]. Necesita n barras a cada lado."""
    ph, pl = [], []
    for i in range(n, len(bars) - n):
        if all(bars[j]["h"] <= bars[i]["h"] for j in range(i - n, i + n + 1) if j != i):
            ph.append(i)
        if all(bars[j]["l"] >= bars[i]["l"] for j in range(i - n, i + n + 1) if j != i):
            pl.append(i)
    return ph, pl


def _swing_structure(bars: list, interval: str) -> dict:
    """
    Clasifica la estructura de swing: bullish / bearish / contraction / expansion / unclear.

    bullish:     HH + HL  (tendencia alcista)
    bearish:     LH + LL  (tendencia bajista)
    contraction: LH + HL  (triangulo/coil)
    expansion:   HH + LL  (volatilidad)
    """
    n       = PIVOT_N.get(interval, 3)
    min_pct = MIN_SWING_PCT.get(interval, 0.001)

    ph_idx, pl_idx = _pivot_highs_lows(bars, n)

    if len(ph_idx) < 2 or len(pl_idx) < 2:
        return {"structure": "unclear", "pattern": "insuficientes pivots",
                "last_ph": None, "prev_ph": None, "last_pl": None, "prev_pl": None,
                "n_ph": len(ph_idx), "n_pl": len(pl_idx)}

    last_ph = bars[ph_idx[-1]]["h"]
    prev_ph = bars[ph_idx[-2]]["h"]
    last_pl = bars[pl_idx[-1]]["l"]
    prev_pl = bars[pl_idx[-2]]["l"]
    ref     = bars[-1]["c"] if bars else 1.0

    def sig(a, b):
        return ref > 0 and abs(a - b) / ref >= min_pct

    hh = last_ph > prev_ph and sig(last_ph, prev_ph)
    lh = last_ph < prev_ph and sig(last_ph, prev_ph)
    hl = last_pl > prev_pl and sig(last_pl, prev_pl)
    ll = last_pl < prev_pl and sig(last_pl, prev_pl)

    if hh and hl:
        structure, pattern = "bullish",     "HH+HL (tendencia alcista)"
    elif lh and ll:
        structure, pattern = "bearish",     "LH+LL (tendencia bajista)"
    elif lh and hl:
        structure, pattern = "contraction", "LH+HL (contraccion/triangulo)"
    elif hh and ll:
        structure, pattern = "expansion",   "HH+LL (expansion/volatilidad)"
    else:
        structure, pattern = "unclear",     "sin patron claro"

    return {
        "structure": structure,
        "pattern":   pattern,
        "last_ph":   round(last_ph, 4),
        "prev_ph":   round(prev_ph, 4),
        "last_pl":   round(last_pl, 4),
        "prev_pl":   round(prev_pl, 4),
        "n_ph":      len(ph_idx),
        "n_pl":      len(pl_idx),
    }


def _position_in_bar(bars_1m: list) -> float | None:
    """
    Posicion relativa del cierre en el rango de la ultima barra 1m.
    0% = en el minimo  |  100% = en el maximo.
    """
    if not bars_1m:
        return None
    last = bars_1m[-1]
    rng  = last["h"] - last["l"]
    if rng <= 0:
        return None
    return round((last["c"] - last["l"]) / rng * 100, 1)


def _atr(bars: list, period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    trs = [max(bars[i]["h"] - bars[i]["l"],
               abs(bars[i]["h"] - bars[i - 1]["c"]),
               abs(bars[i]["l"] - bars[i - 1]["c"]))
           for i in range(1, len(bars))]
    return float(np.mean(trs[-period:])) if trs else None


def _atr_ratio(bars_5m: list) -> float | None:
    """
    ATR(14) de las ultimas 14 barras 5m vs media de ATRs historicos (ventanas de 14).
    > 1.20 → expansion de volatilidad  |  < 0.80 → contraccion.
    """
    if len(bars_5m) < 30:
        return None
    current = _atr(bars_5m[-15:], period=14)
    if not current or current <= 0:
        return None
    atrs = []
    for i in range(0, min(len(bars_5m) - 15, 200), 14):
        a = _atr(bars_5m[i:i + 15], period=14)
        if a and a > 0:
            atrs.append(a)
    if not atrs:
        return None
    return round(current / float(np.mean(atrs)), 2)


def compute_market_structure(session: Session, instrument: str, direction: str) -> dict:
    """
    Calcula estructura de swing multitemporal (30m + 5m) y momentum 1m.

    Returns dict:
      signal       — 1 si ambos TF alinean, 0 si no, None si sin datos suficientes
      swing_30m    — "bullish"|"bearish"|"contraction"|"expansion"|"unclear"
      swing_5m     — idem
      pattern_30m  — descripcion textual
      pattern_5m   — idem
      aligned_30m  — bool: 30m alineado con direction
      aligned_5m   — bool: 5m alineado con direction
      position_pct — posicion en ultima barra 1m (0-100%)
      atr_ratio    — ATR actual / ATR historico (5m, periodo 14)
    """
    bars_30m = _fetch_bars(session, instrument, "30m", 80)
    bars_5m  = _fetch_bars(session, instrument, "5m",  100)
    bars_1m  = _fetch_bars(session, instrument, "1m",  20)

    no_data_30 = {"structure": "unclear", "pattern": "sin datos 30m",
                  "last_ph": None, "prev_ph": None, "last_pl": None, "prev_pl": None}
    no_data_5  = {"structure": "unclear", "pattern": "sin datos 5m",
                  "last_ph": None, "prev_ph": None, "last_pl": None, "prev_pl": None}

    sw_30m = _swing_structure(bars_30m, "30m") if len(bars_30m) >= 12 else no_data_30
    sw_5m  = _swing_structure(bars_5m,  "5m")  if len(bars_5m)  >= 10 else no_data_5

    pos_pct   = _position_in_bar(bars_1m)
    atr_ratio = _atr_ratio(bars_5m)

    if direction == "LONG":
        aligned_30m = sw_30m["structure"] == "bullish"
        aligned_5m  = sw_5m["structure"]  == "bullish"
    else:
        aligned_30m = sw_30m["structure"] == "bearish"
        aligned_5m  = sw_5m["structure"]  == "bearish"

    both_unclear = (sw_30m["structure"] == "unclear" and sw_5m["structure"] == "unclear")
    if both_unclear:
        signal = None
    else:
        signal = 1 if (aligned_30m and aligned_5m) else 0

    return {
        "signal":       signal,
        "swing_30m":    sw_30m["structure"],
        "swing_5m":     sw_5m["structure"],
        "pattern_30m":  sw_30m["pattern"],
        "pattern_5m":   sw_5m["pattern"],
        "last_ph_30m":  sw_30m.get("last_ph"),
        "last_pl_30m":  sw_30m.get("last_pl"),
        "last_ph_5m":   sw_5m.get("last_ph"),
        "last_pl_5m":   sw_5m.get("last_pl"),
        "n_ph_30m":     sw_30m.get("n_ph", 0),
        "n_pl_30m":     sw_30m.get("n_pl", 0),
        "n_ph_5m":      sw_5m.get("n_ph", 0),
        "n_pl_5m":      sw_5m.get("n_pl", 0),
        "aligned_30m":  aligned_30m,
        "aligned_5m":   aligned_5m,
        "position_pct": pos_pct,
        "atr_ratio":    atr_ratio,
    }
