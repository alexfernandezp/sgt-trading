"""
Alineacion multi-timeframe (4h / 1h / 30m).

LONG  alineado: precio > MA20(4h)  Y  precio > MA20(1h)  Y  precio > VWAP sesion
SHORT alineado: precio < MA20(4h)  Y  precio < MA20(1h)  Y  precio < VWAP sesion

Score 0-3. Solo operar con 3/3 (o al menos 2/3 con advertencia).
"""
import logging
from datetime import date
from sqlalchemy.orm import Session
from sqlalchemy import text
from ingestion.intraday import calc_session_vwap
from services.scoring import get_current_price

logger = logging.getLogger(__name__)


def _bars(session, instrument, interval, n=25):
    rows = session.execute(text("""
        SELECT datetime, open, high, low, close, volume
        FROM price_bars
        WHERE instrument = :instr AND interval = :iv
        ORDER BY datetime DESC LIMIT :n
    """), {"instr": instrument, "iv": interval, "n": n}).fetchall()
    if not rows:
        return []
    return list(reversed(rows))  # chronological order


def _ma(closes, period=20):
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 4)


def _momentum(bars, n=3):
    """Cuantas de las ultimas n barras cierran por encima de su apertura."""
    if len(bars) < n:
        return None
    count = sum(1 for b in bars[-n:] if float(b[4]) > float(b[1]))  # close > open
    return count  # 0-n


def _prev_day(session, instrument):
    """High, low, close del ultimo dia de trading completo."""
    row = session.execute(text("""
        WITH pd AS (
            SELECT MAX(DATE(datetime)) AS d
            FROM price_bars
            WHERE instrument = :instr AND interval = '30m'
              AND DATE(datetime) < CURRENT_DATE
        )
        SELECT pd.d,
               MAX(pb.high)  AS h,
               MIN(pb.low)   AS l,
               (SELECT pb2.close
                FROM price_bars pb2
                WHERE pb2.instrument = :instr AND pb2.interval = '30m'
                  AND DATE(pb2.datetime) = pd.d
                ORDER BY pb2.datetime DESC LIMIT 1) AS c
        FROM price_bars pb, pd
        WHERE pb.instrument = :instr AND pb.interval = '30m'
          AND DATE(pb.datetime) = pd.d
        GROUP BY pd.d
    """), {"instr": instrument}).fetchone()
    if not row or row[1] is None:
        return None
    return {
        "date":  str(row[0]),
        "high":  round(float(row[1]), 4),
        "low":   round(float(row[2]), 4),
        "close": round(float(row[3]), 4),
    }


def compute_mtf_alignment(
    session: Session,
    direction: str,
    instrument: str = "SBN26",
) -> dict | None:
    direction = direction.upper()

    price = get_current_price(session, instrument)
    if price is None:
        return None

    details = {}
    score   = 0

    # ── 4h ──────────────────────────────────────────────────────────────
    b4h = _bars(session, instrument, "4h", 25)
    if len(b4h) >= 20:
        closes = [float(b[4]) for b in b4h]
        ma = _ma(closes)
        mom = _momentum(b4h)
        if ma is not None:
            aligned = (price > ma) if direction == "LONG" else (price < ma)
            if aligned:
                score += 1
            details["4h"] = {
                "ma20":    ma,
                "dist":    round(price - ma, 4),
                "aligned": aligned,
                "momentum_3bars": mom,
            }

    # ── 1h ──────────────────────────────────────────────────────────────
    b1h = _bars(session, instrument, "1h", 25)
    if len(b1h) >= 20:
        closes = [float(b[4]) for b in b1h]
        ma = _ma(closes)
        mom = _momentum(b1h)
        if ma is not None:
            aligned = (price > ma) if direction == "LONG" else (price < ma)
            if aligned:
                score += 1
            details["1h"] = {
                "ma20":    ma,
                "dist":    round(price - ma, 4),
                "aligned": aligned,
                "momentum_3bars": mom,
            }

    # ── 30m: VWAP sesion ─────────────────────────────────────────────────
    b30m = _bars(session, instrument, "30m", 30)
    vwap = calc_session_vwap(session, instrument)
    if vwap is not None:
        aligned = (price > vwap) if direction == "LONG" else (price < vwap)
        if aligned:
            score += 1
        mom = _momentum(b30m, n=3) if b30m else None
        details["30m"] = {
            "vwap":    round(vwap, 4),
            "dist":    round(price - vwap, 4),
            "aligned": aligned,
            "momentum_3bars": mom,
        }

    # ── Niveles dia anterior ─────────────────────────────────────────────
    prev = _prev_day(session, instrument)

    # max_score depende de si el VWAP esta disponible (solo en sesion activa)
    max_score = len(details)  # 4h + 1h + 30m (si hay VWAP) = 2 o 3

    # ── Recomendacion ────────────────────────────────────────────────────
    if score == max_score and max_score > 0:
        rec       = "OPERAR  - todos los timeframes disponibles alineados"
        rec_short = "OPERAR"
    elif score >= max(max_score - 1, 1):
        rec       = "ESPERAR - alineacion parcial (%d/%d), confirmar TF pendiente" % (score, max_score)
        rec_short = "ESPERAR"
    else:
        rec       = "NO OPERAR en esta direccion (%d/%d alineados)" % (score, max_score)
        rec_short = "NO OPERAR"

    return {
        "direction": direction,
        "price":     price,
        "score":     score,
        "max_score": max_score,
        "vwap_available": "30m" in details,
        "aligned":   score == max_score and max_score > 0,
        "details":   details,
        "prev_day":  prev,
        "rec":       rec,
        "rec_short": rec_short,
    }
