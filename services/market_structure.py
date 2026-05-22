"""
Estructura de mercado multitemporal para L2 (intradía).

Timeframe stack (todo intraday, sin barras 30m/4h):
  15m — contexto reciente: resampleado de 5m en tiempo real, N=3
        un swing confirma en ~1.5h
  5m  — estructura de entrada, N=3
        un swing confirma en ~30min
  1m  — posicion en barra y ratio ATR (momentum inmediato)

Senal L2-9:
  1 si 15m y 5m alinean con la direccion del trade
  0 si al menos uno no alinea
  None si datos insuficientes en ambos
"""
import logging
import numpy as np
from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)

PIVOT_N       = {"15m": 3, "5m": 3}
MIN_SWING_PCT = {"15m": 0.002, "5m": 0.001}   # 0.2% y 0.1% del precio


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


def _resample_to_15m(bars_5m: list) -> list:
    """
    Agrupa barras 5m en grupos de 3 → barras 15m.
    OHLC: open=primero, high=max, low=min, close=ultimo.
    No requiere timestamps alineados: usa posicion ordinal.
    """
    result = []
    for i in range(0, len(bars_5m) - 2, 3):
        grp = bars_5m[i:i + 3]
        if len(grp) < 3:
            break
        result.append({
            "dt": grp[-1]["dt"],
            "h":  max(b["h"] for b in grp),
            "l":  min(b["l"] for b in grp),
            "c":  grp[-1]["c"],
        })
    return result


def _pivot_highs_lows(bars: list, n: int) -> tuple:
    """Pivot high en i: bars[i].h es maximo de [i-n .. i+n]."""
    ph, pl = [], []
    for i in range(n, len(bars) - n):
        if all(bars[j]["h"] <= bars[i]["h"] for j in range(i - n, i + n + 1) if j != i):
            ph.append(i)
        if all(bars[j]["l"] >= bars[i]["l"] for j in range(i - n, i + n + 1) if j != i):
            pl.append(i)
    return ph, pl


def _swing_structure(bars: list, interval: str) -> dict:
    """
    Clasifica la estructura de swing de las ultimas barras.

    bullish:     HH + HL  (tendencia alcista)
    bearish:     LH + LL  (tendencia bajista)
    contraction: LH + HL  (triangulo/coil)
    expansion:   HH + LL  (volatilidad)
    unclear:     patron insuficiente o no significativo
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
    ATR(14) actual vs media de ATRs historicos en ventanas de 14 barras (en 5m).
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
    Calcula estructura de swing intraday (15m resampleado + 5m) y momentum 1m.

    15m: resampleado de 5m en tiempo real (sin necesidad de barras 15m en DB).
    Ambos TF viven dentro de la sesion intraday.

    Returns dict:
      signal       — 1 si ambos TF alinean, 0 si no, None si sin datos suficientes
      swing_15m    — "bullish"|"bearish"|"contraction"|"expansion"|"unclear"
      swing_5m     — idem
      pattern_15m  — descripcion textual
      pattern_5m   — idem
      aligned_15m  — bool: 15m alineado con direction
      aligned_5m   — bool: 5m alineado con direction
      position_pct — posicion en ultima barra 1m (0-100%)
      atr_ratio    — ATR(14) actual / ATR historico medio (5m)
    """
    # 150 barras 5m = 12.5h → suficiente para ~50 barras 15m tras resample
    bars_5m_raw = _fetch_bars(session, instrument, "5m",  150)
    bars_15m    = _resample_to_15m(bars_5m_raw)
    bars_1m     = _fetch_bars(session, instrument, "1m",  20)

    no_data_15 = {"structure": "unclear", "pattern": "sin datos 15m",
                  "last_ph": None, "prev_ph": None, "last_pl": None, "prev_pl": None}
    no_data_5  = {"structure": "unclear", "pattern": "sin datos 5m",
                  "last_ph": None, "prev_ph": None, "last_pl": None, "prev_pl": None}

    sw_15m = _swing_structure(bars_15m,    "15m") if len(bars_15m)    >= 10 else no_data_15
    sw_5m  = _swing_structure(bars_5m_raw, "5m")  if len(bars_5m_raw) >= 10 else no_data_5

    pos_pct   = _position_in_bar(bars_1m)
    atr_ratio = _atr_ratio(bars_5m_raw)

    if direction == "LONG":
        aligned_15m = sw_15m["structure"] == "bullish"
        aligned_5m  = sw_5m["structure"]  == "bullish"
    else:
        aligned_15m = sw_15m["structure"] == "bearish"
        aligned_5m  = sw_5m["structure"]  == "bearish"

    both_unclear = (sw_15m["structure"] == "unclear" and sw_5m["structure"] == "unclear")
    signal = None if both_unclear else (1 if (aligned_15m and aligned_5m) else 0)

    return {
        "signal":       signal,
        "swing_15m":    sw_15m["structure"],
        "swing_5m":     sw_5m["structure"],
        "pattern_15m":  sw_15m["pattern"],
        "pattern_5m":   sw_5m["pattern"],
        "last_ph_15m":  sw_15m.get("last_ph"),
        "last_pl_15m":  sw_15m.get("last_pl"),
        "last_ph_5m":   sw_5m.get("last_ph"),
        "last_pl_5m":   sw_5m.get("last_pl"),
        "n_ph_15m":     sw_15m.get("n_ph", 0),
        "n_pl_15m":     sw_15m.get("n_pl", 0),
        "n_ph_5m":      sw_5m.get("n_ph", 0),
        "n_pl_5m":      sw_5m.get("n_pl", 0),
        "n_bars_15m":   len(bars_15m),
        "aligned_15m":  aligned_15m,
        "aligned_5m":   aligned_5m,
        "position_pct": pos_pct,
        "atr_ratio":    atr_ratio,
    }
