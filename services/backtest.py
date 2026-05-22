"""
Simulacion walk-forward con senales L1 OOS-validadas.

Senales usadas (validadas out-of-sample 2023-2025):
  LONG : A1_regime (EXTREMO_CORTO / CROWDED_SHORT) + OI_divergencia (capitulacion)
  SHORT: A1_regime (EXTREMO_LARGO / CROWDED_LONG)  + OI_divergencia (distribucion)

Mejoras vs version anterior:
  - Lag de publicacion COT aplicado (martes → viernes, +3 business days)
  - OI divergencia en lugar de simple OI trend
  - Eliminado 'precio > MA20' (0% edge OOS) y A2b (overfit severo)
  - Stop por defecto 2xATR(14) diario
"""
import logging
import numpy as np
import pandas as pd
from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)


def _rolling_pct_exp(series):
    return series.expanding(min_periods=2).apply(
        lambda x: (x <= x[-1]).mean() * 100, raw=True)


def _rolling_pct_n(series, n):
    return series.rolling(n, min_periods=max(4, n // 2)).apply(
        lambda x: (x <= x[-1]).mean() * 100, raw=True)


def _build_df(session):
    price_rows = session.execute(text("""
        SELECT date, high, low, close
        FROM price_history WHERE instrument='SB_CONT'
        ORDER BY date ASC
    """)).fetchall()
    cot_rows = session.execute(text("""
        SELECT report_date, speculator_net, total_oi
        FROM cot_data ORDER BY report_date ASC
    """)).fetchall()

    if len(price_rows) < 50 or len(cot_rows) < 20:
        return None

    df = pd.DataFrame(price_rows, columns=["date", "high", "low", "close"])
    df["date"] = pd.to_datetime(df["date"])
    for c in ["high", "low", "close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_values("date").reset_index(drop=True)

    cot = pd.DataFrame(cot_rows, columns=["report_date", "spec_net", "total_oi"])
    cot["report_date"] = pd.to_datetime(cot["report_date"])
    for c in ["spec_net", "total_oi"]:
        cot[c] = pd.to_numeric(cot[c], errors="coerce")
    cot = cot.sort_values("report_date").reset_index(drop=True)

    # --- Señales COT (semanales) ---
    cot["spec_pct_all"] = _rolling_pct_exp(cot["spec_net"])
    cot["spec_pct_13w"] = _rolling_pct_n(cot["spec_net"], 13)
    cot["spec_ma4"]     = cot["spec_net"].rolling(4, min_periods=2).mean()
    cot["spec_trend4"]  = cot["spec_ma4"] - cot["spec_ma4"].shift(1)
    cot["oi_4w_chg"]    = cot["total_oi"] - cot["total_oi"].shift(4)

    # A1 contrarian regime
    p = cot["spec_pct_all"]; p13 = cot["spec_pct_13w"]; t = cot["spec_trend4"]
    cot["a1_long"]  = ((p <= 5) | ((t < 0) & (p13 <= 40))).astype(int)
    cot["a1_short"] = ((p >= 95) | ((p >= 85) & (t < 0)) | ((t > 0) & (p13 >= 60))).astype(int)

    # Lag de publicacion: +3 business days (martes → viernes)
    cot["eff_date"] = cot["report_date"] + pd.offsets.BusinessDay(3)

    # --- Indicadores diarios ---
    prev = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"]  - prev).abs(),
    ], axis=1).max(axis=1)
    df["atr14"]      = tr.rolling(14).mean()
    df["price_20ago"] = df["close"].shift(20)   # ~4 semanas trading

    # --- Merge COT en precio diario (con lag) ---
    cot_m = cot[["eff_date", "a1_long", "a1_short", "oi_4w_chg"]].rename(
        columns={"eff_date": "date"})
    df = pd.merge_asof(df.sort_values("date"), cot_m, on="date").ffill()

    # OI divergencia precio-OI
    price_up = df["close"] > df["price_20ago"]
    oi_fall  = df["oi_4w_chg"] < 0
    df["oi_long"]  = (oi_fall & ~price_up).astype(int)   # OI↓ + precio↓ = capitulacion
    df["oi_short"] = (oi_fall &  price_up).astype(int)   # OI↓ + precio↑ = distribucion

    # Señal combinada: A1 OR OI_divergencia
    df["sig_long"]  = ((df["a1_long"]  == 1) | (df["oi_long"]  == 1)).astype(int)
    df["sig_short"] = ((df["a1_short"] == 1) | (df["oi_short"] == 1)).astype(int)

    return df.dropna(subset=["atr14", "a1_long"]).reset_index(drop=True)


def estimate_win_rate(
    session: Session,
    direction: str,
    atr_mult_stop: float = 2.0,
    targets_r: tuple = (1.5, 2.5, 4.0),
    max_hold_days: int = 15,
) -> dict | None:
    """
    Simulacion walk-forward: cuando la senal L1 activa, entra al cierre,
    aplica stop ATR y mide si alcanza cada objetivo R.
    Sin solapamiento entre trades (saltar max_hold_days tras cada entrada).
    """
    direction = direction.upper()
    df = _build_df(session)
    if df is None:
        logger.warning("backtest: datos insuficientes")
        return None

    sig_col = "sig_long" if direction == "LONG" else "sig_short"
    n = len(df)
    tallies    = {t: {"wins": 0, "losses": 0, "timeouts": 0, "exit_rs": []} for t in targets_r}
    skip_until = 0

    for i in range(n - max_hold_days - 1):
        if i < skip_until or not df.loc[i, sig_col]:
            continue
        entry   = float(df.loc[i, "close"])
        atr_val = float(df.loc[i, "atr14"])
        if atr_val <= 0 or np.isnan(atr_val):
            continue

        stop_dist = atr_mult_stop * atr_val
        stop      = (entry - stop_dist) if direction == "LONG" else (entry + stop_dist)
        t_prices  = {t: (entry + t * stop_dist if direction == "LONG" else entry - t * stop_dist)
                     for t in targets_r}
        t_state   = {t: {"done": False, "exit_r": 0.0, "outcome": "to"} for t in targets_r}

        for j in range(i + 1, min(i + max_hold_days + 1, n)):
            lo, hi   = float(df.loc[j, "low"]), float(df.loc[j, "high"])
            stop_hit = (lo <= stop) if direction == "LONG" else (hi >= stop)
            for t in targets_r:
                if t_state[t]["done"]:
                    continue
                tgt_hit = (hi >= t_prices[t]) if direction == "LONG" else (lo <= t_prices[t])
                if stop_hit and not tgt_hit:
                    t_state[t].update(done=True, outcome="loss", exit_r=-1.0)
                elif tgt_hit:
                    t_state[t].update(done=True, outcome="win", exit_r=t)

        ep    = float(df.loc[min(i + max_hold_days, n - 1), "close"])
        raw_r = (ep - entry) / stop_dist if direction == "LONG" else (entry - ep) / stop_dist
        for t in targets_r:
            if not t_state[t]["done"]:
                t_state[t]["exit_r"] = raw_r
            tl = tallies[t]
            tl["exit_rs"].append(t_state[t]["exit_r"])
            if   t_state[t]["outcome"] == "win":  tl["wins"]    += 1
            elif t_state[t]["outcome"] == "loss": tl["losses"]  += 1
            else:                                 tl["timeouts"] += 1

        skip_until = i + max_hold_days

    ref     = tallies[targets_r[0]]
    n_total = ref["wins"] + ref["losses"] + ref["timeouts"]
    if n_total == 0:
        return None

    per_target = {}
    for t in targets_r:
        tl  = tallies[t]
        tot = tl["wins"] + tl["losses"] + tl["timeouts"]
        if tot == 0:
            continue
        rs  = tl["exit_rs"]
        per_target[t] = {
            "win_rate":    round(tl["wins"] / tot, 3),
            "avg_r":       round(sum(rs) / tot, 3),
            "exp_value":   round(sum(rs) / tot, 3),   # E[R] per trade
            "n_wins":      tl["wins"],
            "n_losses":    tl["losses"],
            "n_timeout":   tl["timeouts"],
        }

    return {
        "n_trades":      n_total,
        "atr_mult":      atr_mult_stop,
        "max_hold_days": max_hold_days,
        "per_target":    per_target,
        "signal":        "A1_regime + OI_div (lag COT aplicado)",
    }
