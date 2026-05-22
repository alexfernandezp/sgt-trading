"""
Niveles de Fibonacci automaticos.

Detecta swing high y swing low en los ultimos N dias de price_history
(SB_CONT continuo) y calcula todos los niveles de retraccion estandar.

Convencion: 0% = swing_high, 100% = swing_low.
Los niveles intermedios son zonas de soporte/resistencia segun el precio actual.
"""
import logging
from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)

FIB_RATIOS = [0.0, 0.236, 0.382, 0.500, 0.618, 0.786, 1.0]

FIB_SIG = {
    0.0:   2,
    0.236: 2,
    0.382: 3,
    0.500: 3,
    0.618: 3,
    0.786: 2,
    1.0:   2,
}

FIB_LABELS = {
    0.0:   "Fib  0.0%",
    0.236: "Fib 23.6%",
    0.382: "Fib 38.2%",
    0.500: "Fib 50.0%",
    0.618: "Fib 61.8%",
    0.786: "Fib 78.6%",
    1.0:   "Fib 100% ",
}


def get_fibonacci_levels(
    session:      Session,
    instrument:   str = "SB_CONT",
    lookback_days: int = 45,
) -> dict | None:
    """
    Detecta swing high y swing low en los ultimos N dias de precio diario
    y calcula niveles de retraccion Fibonacci estandar.

    Convencion: 0% = swing_high (resistencia maxima), 100% = swing_low.

    Returns:
      swing_high  — maximo detectado en el periodo
      swing_low   — minimo detectado en el periodo
      range_pts   — amplitud del swing (high - low)
      lookback    — dias usados
      levels      — {label: {"value", "ratio", "sig"}}
      None si no hay datos suficientes
    """
    rows = session.execute(text("""
        SELECT date, high, low
        FROM price_history
        WHERE instrument = :instr
        ORDER BY date DESC LIMIT :n
    """), {"instr": instrument, "n": lookback_days}).fetchall()

    if len(rows) < 10:
        logger.warning("fibonacci: datos insuficientes para %s (%d dias)", instrument, len(rows))
        return None

    highs = [float(r[1]) for r in rows if r[1] is not None]
    lows  = [float(r[2]) for r in rows if r[2] is not None]

    if not highs or not lows:
        return None

    swing_high = round(max(highs), 4)
    swing_low  = round(min(lows),  4)
    range_pts  = round(swing_high - swing_low, 4)

    if range_pts <= 0:
        return None

    levels = {}
    for ratio in FIB_RATIOS:
        price = round(swing_high - ratio * range_pts, 4)
        label = FIB_LABELS[ratio]
        levels[label] = {
            "value": price,
            "ratio": ratio,
            "sig":   FIB_SIG[ratio],
        }

    return {
        "swing_high":  swing_high,
        "swing_low":   swing_low,
        "range_pts":   range_pts,
        "lookback":    lookback_days,
        "levels":      levels,
    }
