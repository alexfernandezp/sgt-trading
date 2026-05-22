"""
Volume Profile intradía y multiframe.

Calcula distribución de volumen por precio (no por tiempo) para
identificar nodos de liquidez, POC y Value Area.

Método: para cada barra se distribuye el volumen uniformemente
sobre el rango [low, high] en bins de precio fijos.

Timeframes disponibles:
  session  — barras 30m del dia actual
  weekly   — ultimas 5 sesiones en 30m
  mtd      — barras 30m desde el 1 del mes
  ytd      — barras 1h desde el 2 de enero
"""
import logging
import numpy as np
import pandas as pd
from datetime import date, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)

BIN_SIZE = 0.05   # c/lb por bin — ~20 bins por cada centavo de rango


def _fetch_bars(session, instrument, interval, from_dt):
    rows = session.execute(text("""
        SELECT datetime, high, low, close, volume
        FROM price_bars
        WHERE instrument = :instr AND interval = :iv
          AND datetime >= :from_dt
        ORDER BY datetime ASC
    """), {"instr": instrument, "iv": interval, "from_dt": from_dt}).fetchall()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["dt", "high", "low", "close", "volume"])
    for c in ["high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna()


def _build_profile(df, bin_size=BIN_SIZE):
    """
    Distribuye el volumen de cada barra uniformemente sobre [low, high].
    Devuelve array de (precio_centro_bin, volumen_acumulado).
    """
    if df is None or len(df) == 0:
        return None

    lo_global = float(df["low"].min())
    hi_global = float(df["high"].max())
    # Extend slightly to avoid edge issues
    lo_global = round(np.floor(lo_global / bin_size) * bin_size, 6)
    hi_global = round(np.ceil(hi_global  / bin_size) * bin_size, 6)

    bins    = np.arange(lo_global, hi_global + bin_size, bin_size)
    centers = bins[:-1] + bin_size / 2
    profile = np.zeros(len(centers))

    for _, row in df.iterrows():
        lo, hi, vol = float(row["low"]), float(row["high"]), float(row["volume"])
        if vol <= 0 or hi <= lo:
            # Single-price bar — add all volume to nearest bin
            idx = int(round((lo - lo_global) / bin_size))
            if 0 <= idx < len(profile):
                profile[idx] += vol
            continue
        # Find overlapping bins
        i_lo = max(0,             int(np.floor((lo - lo_global) / bin_size)))
        i_hi = min(len(profile)-1, int(np.floor((hi - lo_global) / bin_size)))
        n_bins = i_hi - i_lo + 1
        per_bin = vol / n_bins
        profile[i_lo:i_hi+1] += per_bin

    return pd.DataFrame({"price": centers, "volume": profile})


def _find_poc(prof_df):
    """Price with highest volume."""
    idx = prof_df["volume"].idxmax()
    return round(float(prof_df.loc[idx, "price"]), 4)


def _value_area(prof_df, pct=0.70):
    """
    Value Area: price range containing pct (default 70%) of total volume.
    Starts from POC, expands to adjacent bins by highest volume.
    Returns (VAL, VAH).
    """
    total    = prof_df["volume"].sum()
    target   = total * pct
    poc_idx  = prof_df["volume"].idxmax()

    lo_idx   = poc_idx
    hi_idx   = poc_idx
    captured = float(prof_df.loc[poc_idx, "volume"])

    while captured < target:
        lo_candidate = lo_idx - 1 if lo_idx > 0 else None
        hi_candidate = hi_idx + 1 if hi_idx < len(prof_df) - 1 else None

        lo_vol = float(prof_df.loc[lo_candidate, "volume"]) if lo_candidate is not None else 0.0
        hi_vol = float(prof_df.loc[hi_candidate, "volume"]) if hi_candidate is not None else 0.0

        if lo_vol == 0 and hi_vol == 0:
            break
        if lo_vol >= hi_vol and lo_candidate is not None:
            lo_idx   = lo_candidate
            captured += lo_vol
        elif hi_candidate is not None:
            hi_idx   = hi_candidate
            captured += hi_vol
        else:
            lo_idx   = lo_candidate
            captured += lo_vol

    val = round(float(prof_df.loc[lo_idx, "price"]), 4)
    vah = round(float(prof_df.loc[hi_idx, "price"]), 4)
    return val, vah


def _find_nodes(prof_df, n_hvn=5):
    """
    Finds High Volume Nodes (HVN) and Low Volume Nodes (LVN).
    HVN: local maxima above 75th percentile of volume.
    LVN: local minima below 25th percentile of volume (between HVNs).
    Returns (hvn_list, lvn_list) sorted by volume desc.
    """
    vols   = prof_df["volume"].values
    prices = prof_df["price"].values
    n      = len(vols)
    q75    = np.percentile(vols[vols > 0], 75)
    q25    = np.percentile(vols[vols > 0], 25)

    hvn = []
    for i in range(1, n - 1):
        if vols[i] >= q75 and vols[i] >= vols[i-1] and vols[i] >= vols[i+1]:
            hvn.append({"price": round(float(prices[i]), 4), "volume": float(vols[i])})

    lvn = []
    for i in range(1, n - 1):
        if vols[i] <= q25 and vols[i] <= vols[i-1] and vols[i] <= vols[i+1]:
            lvn.append({"price": round(float(prices[i]), 4), "volume": float(vols[i])})

    hvn = sorted(hvn, key=lambda x: -x["volume"])[:n_hvn]
    lvn = sorted(lvn, key=lambda x:  x["volume"])[:n_hvn]
    return hvn, lvn


def compute_volume_profile(
    session: Session,
    instrument: str = "SBN26",
    timeframe: str = "mtd",
) -> dict | None:
    """
    Calcula el Volume Profile para el timeframe indicado.

    timeframe:
      session  — dia actual, barras 30m
      weekly   — 5 sesiones, barras 30m
      mtd      — mes actual, barras 30m
      ytd      — ano actual, barras 1h

    Devuelve:
      poc, val, vah (Value Area Low/High 70%)
      hvn: High Volume Nodes (nodos de liquidez, soporte/resistencia)
      lvn: Low Volume Nodes (zonas de movimiento rapido)
      profile_df: DataFrame completo (price, volume)
      n_bars, from_dt, timeframe
    """
    today = date.today()

    if timeframe == "session":
        from_dt  = today
        interval = "30m"
    elif timeframe == "weekly":
        from_dt  = today - timedelta(days=7)
        interval = "30m"
    elif timeframe == "mtd":
        from_dt  = date(today.year, today.month, 1)
        interval = "30m"
    elif timeframe == "ytd":
        from_dt  = date(today.year, 1, 2)
        interval = "1h"
    else:
        logger.warning("volume_profile: unknown timeframe %s", timeframe)
        return None

    df = _fetch_bars(session, instrument, interval, from_dt)
    if df is None or len(df) < 5:
        logger.warning("volume_profile: not enough bars for %s %s", instrument, timeframe)
        return None

    prof = _build_profile(df)
    if prof is None:
        return None

    poc       = _find_poc(prof)
    val, vah  = _value_area(prof)
    hvn, lvn  = _find_nodes(prof)

    return {
        "timeframe":  timeframe,
        "from_dt":    str(from_dt),
        "interval":   interval,
        "n_bars":     len(df),
        "poc":        poc,
        "val":        val,
        "vah":        vah,
        "hvn":        hvn,   # [{price, volume}, ...]
        "lvn":        lvn,
        "price_min":  round(float(df["low"].min()),  4),
        "price_max":  round(float(df["high"].max()), 4),
        "profile":    prof,
    }


def get_multiframe_vp(session: Session, instrument: str = "SBN26") -> dict:
    """
    Calcula VP para los 4 timeframes y devuelve dict.
    Cada timeframe puede ser None si no hay datos suficientes.
    """
    result = {}
    for tf in ("session", "weekly", "mtd", "ytd"):
        try:
            result[tf] = compute_volume_profile(session, instrument, tf)
        except Exception as e:
            logger.warning("vp %s: %s", tf, e)
            result[tf] = None
    return result


def nearest_vp_level(vp_dict: dict, price: float, max_dist: float = 0.5) -> list:
    """
    Dado un dict de VP multiframe, encuentra todos los HVN/LVN/POC
    dentro de max_dist centavos del precio dado.
    Devuelve lista de {timeframe, type, price, dist} ordenada por |dist|.
    """
    hits = []
    for tf, vp in vp_dict.items():
        if vp is None:
            continue
        for node in vp.get("hvn", []):
            d = round(node["price"] - price, 4)
            if abs(d) <= max_dist:
                hits.append({"tf": tf, "type": "HVN", "price": node["price"], "dist": d})
        for node in vp.get("lvn", []):
            d = round(node["price"] - price, 4)
            if abs(d) <= max_dist:
                hits.append({"tf": tf, "type": "LVN", "price": node["price"], "dist": d})
        poc = vp.get("poc")
        if poc:
            d = round(poc - price, 4)
            if abs(d) <= max_dist:
                hits.append({"tf": tf, "type": "POC", "price": poc, "dist": d})
    return sorted(hits, key=lambda x: abs(x["dist"]))
