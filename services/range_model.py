"""
Modelo de rango intradiario esperado.

Correlacion historica ritmo-de-volumen / rango-diario para estimar
cuanto puede moverse el mercado en la sesion actual, y clasificar
el tipo de dia (LOW_VOL, NORMAL, HIGH_VOL).
"""
import logging
import math
from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)

DAY_TYPES = {
    "LOW_VOL":     (0.00, 0.55),
    "BAJO_NORMAL": (0.55, 0.85),
    "NORMAL":      (0.85, 1.25),
    "ALTO_NORMAL": (1.25, 1.70),
    "HIGH_VOL":    (1.70, 9.99),
}


def classify_day_type(pace_ratio: float) -> str:
    for label, (lo, hi) in DAY_TYPES.items():
        if lo <= pace_ratio < hi:
            return label
    return "NORMAL"


def get_today_ohlc(session: Session, instrument: str = "SBN26") -> dict | None:
    """OHLC de la sesion en curso (barras 30m de hoy)."""
    rows = session.execute(text("""
        SELECT open, high, low, close
        FROM price_bars
        WHERE instrument = :instr AND interval = '30m'
          AND DATE(datetime) = CURRENT_DATE
        ORDER BY datetime ASC
    """), {"instr": instrument}).fetchall()
    if not rows:
        return None
    highs  = [float(r[1]) for r in rows if r[1] is not None]
    lows   = [float(r[2]) for r in rows if r[2] is not None]
    if not highs or not lows:
        return None
    return {
        "open":   round(float(rows[0][0]), 4),
        "high":   round(max(highs), 4),
        "low":    round(min(lows), 4),
        "close":  round(float(rows[-1][3]), 4),
        "range":  round(max(highs) - min(lows), 4),
        "n_bars": len(rows),
    }


def estimate_full_range(
    session:    Session,
    instrument: str,
    pace_ratio: float,
    n_bars:     int,
    atr_daily:  float,
) -> dict:
    """
    Estima el rango total esperado de la sesion actual.

    Metodo empirico:
    1. Para los ultimos 60 dias calcula early_vol (suma a N barras)
       y full_range (high - low del dia completo).
    2. Normaliza early_vol -> ritmo (vs media).
    3. Selecciona dias con ritmo similar (+/- 30%) al de hoy.
    4. Toma la mediana de sus rangos como estimacion.
    5. Fallback: ATR * sqrt(pace_ratio) si no hay suficiente historia.

    Returns:
      expected_range    — rango esperado para el dia completo
      remaining_range   — rango restante (expected - range_so_far)
      day_type          — LOW_VOL / NORMAL / HIGH_VOL / ...
      confidence        — HIGH / MED / LOW
      n_similar         — dias historicos similares usados
    """
    today_ohlc = get_today_ohlc(session, instrument)
    range_so_far = today_ohlc["range"] if today_ohlc else 0.0

    if n_bars < 1:
        expected = atr_daily
        return _pack(expected, range_so_far, pace_ratio, "LOW", 0)

    rows = session.execute(text("""
        WITH bar_nums AS (
            SELECT
                DATE(datetime)::date                                                  AS day,
                CAST(volume AS FLOAT)                                                 AS vol,
                CAST(high   AS FLOAT)                                                 AS high,
                CAST(low    AS FLOAT)                                                 AS low,
                ROW_NUMBER() OVER (PARTITION BY DATE(datetime) ORDER BY datetime)     AS bar_num
            FROM price_bars
            WHERE instrument = :instr AND interval = '30m'
              AND DATE(datetime) < CURRENT_DATE
              AND DATE(datetime) >= CURRENT_DATE - INTERVAL '90 days'
        ),
        daily AS (
            SELECT
                day,
                SUM(CASE WHEN bar_num <= :n THEN vol ELSE 0 END) AS early_vol,
                MAX(high) - MIN(low)                              AS full_range
            FROM bar_nums
            GROUP BY day
        )
        SELECT day, early_vol, full_range
        FROM daily
        WHERE early_vol > 0 AND full_range > 0
        ORDER BY day DESC
        LIMIT 50
    """), {"instr": instrument, "n": n_bars}).fetchall()

    if len(rows) < 5:
        expected = round(atr_daily * math.sqrt(max(pace_ratio, 0.10)), 4)
        return _pack(expected, range_so_far, pace_ratio, "LOW", 0)

    early_vols = [float(r[1]) for r in rows]
    avg_early  = sum(early_vols) / len(early_vols)

    similar = _find_similar(rows, pace_ratio, avg_early, tolerance=0.30)
    if len(similar) < 4:
        similar = _find_similar(rows, pace_ratio, avg_early, tolerance=0.50)

    if similar:
        similar.sort()
        expected   = round(similar[len(similar) // 2], 4)
        confidence = "HIGH" if len(similar) >= 8 else ("MED" if len(similar) >= 4 else "LOW")
    else:
        expected   = round(atr_daily * math.sqrt(max(pace_ratio, 0.10)), 4)
        confidence = "LOW"

    return _pack(expected, range_so_far, pace_ratio, confidence, len(similar))


def _find_similar(rows, pace, avg_early, tolerance):
    result = []
    for _, ev, fr in rows:
        day_pace = float(ev) / avg_early if avg_early > 0 else 1.0
        if abs(day_pace - pace) <= tolerance:
            result.append(float(fr))
    return result


def _pack(expected, range_so_far, pace_ratio, confidence, n_similar):
    remaining = round(max(expected - range_so_far, expected * 0.20), 4)
    return {
        "expected_range":  expected,
        "range_so_far":    round(range_so_far, 4),
        "remaining_range": remaining,
        "day_type":        classify_day_type(pace_ratio),
        "confidence":      confidence,
        "n_similar":       n_similar,
    }
