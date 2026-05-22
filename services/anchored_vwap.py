"""
VWAP anclado con bandas de desviacion estandar.

Soporta dos anclas:
  YTD: primer dia de trading del ano (usando barras 1h - 2 anos disponibles)
  MTD: primer dia de trading del mes (usando barras 30m - 60 dias disponibles)

Formula de varianza online (sin lookahead, sin circularidad):
  variance = E[TP^2] - E[TP]^2   (ponderado por volumen)
  donde E[X] = sum(V_i * X_i) / sum(V_i)
"""
import logging
import numpy as np
import pandas as pd
from datetime import date, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)


def _first_trading_day_on_or_after(session, instrument, anchor_date, interval):
    """Encuentra el primer datetime de bar en o despues de anchor_date."""
    row = session.execute(text("""
        SELECT MIN(datetime) FROM price_bars
        WHERE instrument = :instr AND interval = :iv
          AND DATE(datetime) >= :d
    """), {"instr": instrument, "iv": interval, "d": anchor_date}).fetchone()
    return row[0] if row and row[0] else None


def anchored_vwap(session: Session, instrument: str, anchor_date: date, interval: str = "1h") -> dict | None:
    """
    Calcula VWAP anclado desde anchor_date con 3 bandas de desviacion estandar.

    Devuelve:
      vwap, std, n_bars, anchor_used
      upper_1/2/3, lower_1/2/3
      price, sigma_position (en cuantas sigmas esta el precio)
      zone (descripcion de la zona)
    """
    anchor_dt = _first_trading_day_on_or_after(session, instrument, anchor_date, interval)
    if anchor_dt is None:
        logger.warning("anchored_vwap: no bars for %s %s from %s", instrument, interval, anchor_date)
        return None

    rows = session.execute(text("""
        SELECT datetime, high, low, close, volume
        FROM price_bars
        WHERE instrument = :instr AND interval = :iv
          AND datetime >= :anchor
        ORDER BY datetime ASC
    """), {"instr": instrument, "iv": interval, "anchor": anchor_dt}).fetchall()

    if not rows:
        return None

    df = pd.DataFrame(rows, columns=["dt", "high", "low", "close", "volume"])
    for c in ["high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna()
    if len(df) < 5:
        return None

    # Typical price
    df["tp"] = (df["high"] + df["low"] + df["close"]) / 3

    # Online cumulative VWAP + variance
    # Var(TP) = E[TP^2] - E[TP]^2  (volume-weighted)
    cum_vol  = df["volume"].cumsum()
    cum_tpv  = (df["tp"] * df["volume"]).cumsum()
    cum_tp2v = (df["tp"] ** 2 * df["volume"]).cumsum()

    vwap_series = cum_tpv / cum_vol
    var_series  = (cum_tp2v / cum_vol) - vwap_series ** 2
    std_series  = np.sqrt(var_series.clip(lower=0))

    current_vwap = float(vwap_series.iloc[-1])
    current_std  = float(std_series.iloc[-1])
    current_price = float(df["close"].iloc[-1])

    if current_std <= 0:
        return None

    sigma_pos = round((current_price - current_vwap) / current_std, 2)

    # Zone label
    abs_sigma = abs(sigma_pos)
    if abs_sigma < 0.5:
        zone = "VWAP (zona neutra)"
    elif abs_sigma < 1.0:
        sign = "soporte" if sigma_pos < 0 else "resistencia"
        zone = "entre VWAP y 1s (%s leve)" % sign
    elif abs_sigma < 2.0:
        sign = "soporte" if sigma_pos < 0 else "resistencia"
        zone = "entre 1s y 2s (%s estadistico)" % sign
    elif abs_sigma < 3.0:
        sign = "soporte" if sigma_pos < 0 else "resistencia"
        zone = "entre 2s y 3s (%s fuerte - posible rebote)" % sign
    else:
        sign = "soporte extremo" if sigma_pos < 0 else "resistencia extrema"
        zone = "mas de 3s (%s - zona de inversion)" % sign

    return {
        "anchor_date":   str(anchor_date),
        "anchor_dt":     str(anchor_dt)[:10],
        "interval":      interval,
        "n_bars":        len(df),
        "vwap":          round(current_vwap, 4),
        "std":           round(current_std, 4),
        "price":         current_price,
        "sigma_pos":     sigma_pos,
        "zone":          zone,
        "upper_1":       round(current_vwap + 1 * current_std, 4),
        "upper_2":       round(current_vwap + 2 * current_std, 4),
        "upper_3":       round(current_vwap + 3 * current_std, 4),
        "lower_1":       round(current_vwap - 1 * current_std, 4),
        "lower_2":       round(current_vwap - 2 * current_std, 4),
        "lower_3":       round(current_vwap - 3 * current_std, 4),
    }


def get_vwap_bands(session: Session, instrument: str = "SBN26") -> dict:
    """
    Calcula Session (5m), YTD (1h) y MTD (30m) anchored VWAP bands.
    Session: barras 5m desde el primer bar del dia actual.
    YTD: barras 1h desde el 2 de enero del ano en curso.
    MTD: barras 30m desde el 1 del mes en curso.
    """
    today = date.today()
    ytd_anchor = date(today.year, 1, 2)
    mtd_anchor = date(today.year, today.month, 1)

    ytd     = anchored_vwap(session, instrument, ytd_anchor, interval="1h")
    mtd     = anchored_vwap(session, instrument, mtd_anchor, interval="30m")
    session_ = anchored_vwap(session, instrument, today,      interval="5m")

    # Direction bias: sesion tiene el mismo peso que MTD/YTD (señal más inmediata)
    biases = []
    if ytd:
        biases.append("LONG" if ytd["sigma_pos"] < -0.5 else ("SHORT" if ytd["sigma_pos"] > 0.5 else "NEUTRAL"))
    if mtd:
        biases.append("LONG" if mtd["sigma_pos"] < -0.5 else ("SHORT" if mtd["sigma_pos"] > 0.5 else "NEUTRAL"))
    if session_:
        biases.append("LONG" if session_["sigma_pos"] < -0.5 else ("SHORT" if session_["sigma_pos"] > 0.5 else "NEUTRAL"))

    long_count  = biases.count("LONG")
    short_count = biases.count("SHORT")
    if long_count > short_count:
        vwap_bias = "LONG"
    elif short_count > long_count:
        vwap_bias = "SHORT"
    else:
        vwap_bias = "NEUTRAL"

    return {"session": session_, "ytd": ytd, "mtd": mtd, "bias": vwap_bias}
