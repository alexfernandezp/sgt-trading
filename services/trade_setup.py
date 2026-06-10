import logging
import pandas as pd
from sqlalchemy.orm import Session
from sqlalchemy import text
from config import RISK_PER_TRADE_USD, CONTRACT_SIZE_LBS

logger = logging.getLogger(__name__)

LOT_MAP = {"NO_TRADE": 0, "REDUCED": 5, "STANDARD": 10, "MAX_CONVICTION": 20}

RR_MIN_T2 = 1.0   # R/R minimo en T2 para que el trade sea valido


def _daily_df(session, instrument="SB_CONT", n=30):
    rows = session.execute(text("""
        SELECT date, high, low, close
        FROM price_history WHERE instrument = :instr
        ORDER BY date DESC LIMIT :n
    """), {"instr": instrument, "n": n}).fetchall()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["date", "high", "low", "close"])
    df = df.sort_values("date").reset_index(drop=True)
    for c in ["high", "low", "close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _intraday_df(session, instrument="SBN26", interval="30m", n=80):
    rows = session.execute(text("""
        SELECT datetime, high, low, close, volume
        FROM price_bars WHERE instrument = :instr AND interval = :iv
        ORDER BY datetime DESC LIMIT :n
    """), {"instr": instrument, "iv": interval, "n": n}).fetchall()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["datetime", "high", "low", "close", "volume"])
    df = df.sort_values("datetime").reset_index(drop=True)
    for c in ["high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _atr(df, period=14):
    hi, lo, cp = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([(hi - lo), (hi - cp).abs(), (lo - cp).abs()], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if pd.notna(val) else None


def _swing_low(df, lookback=25, min_dist=None):
    lows = df["low"].values
    n = len(lows)
    for i in range(n - 2, max(n - lookback, 1), -1):
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            val = float(lows[i])
            if min_dist is None or (float(df["close"].iloc[-1]) - val) >= min_dist:
                return val
    return None


def _swing_high(df, lookback=25, min_dist=None):
    highs = df["high"].values
    n = len(highs)
    for i in range(n - 2, max(n - lookback, 1), -1):
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            val = float(highs[i])
            if min_dist is None or (val - float(df["close"].iloc[-1])) >= min_dist:
                return val
    return None


def _cot_full(session):
    from services.cot_signal import get_cot_signal
    from dataclasses import asdict
    sig = get_cot_signal(session)
    if sig.level_regime == "INSUFFICIENT_DATA":
        return None, None, {}, "NEUTRO"
    pct   = sig.mm_pct_alltime
    net   = sig.mm_net
    ctx   = asdict(sig)
    label = "EXTREMO" if pct <= 15 or pct >= 85 else \
            "ELEVADO"  if pct <= 25 or pct >= 75 else "NEUTRO"
    return round(pct, 1), net, ctx, label


# ── Structural targets ────────────────────────────────────────────────────────

def _collect_levels_for_targets(direction, entry, vwap_bands, vp_dict, max_dist):
    """
    Recolecta niveles estructurales en la direccion del trade
    dentro de max_dist centavos del entry.
    Returns: lista [(precio, nombre)] ordenada de mas cercano a mas lejano.
    """
    levels = []

    def _add(price, name):
        if price is None:
            return
        d = (entry - float(price)) if direction == "SHORT" else (float(price) - entry)
        if 0 < d <= max_dist:
            levels.append((round(float(price), 4), name))

    # VWAP bands
    for key, prefix in [("session", "VWAPses"), ("mtd", "VWAPmtd"), ("ytd", "VWAPytd")]:
        vb = (vwap_bands or {}).get(key)
        if not vb:
            continue
        for lk, ls in [("lower_3", "-3s"), ("lower_2", "-2s"), ("lower_1", "-1s"),
                        ("vwap", ""), ("upper_1", "+1s"), ("upper_2", "+2s"), ("upper_3", "+3s")]:
            _add(vb.get(lk), f"{prefix}{ls}")

    # VP nodes
    for tf, tl in [("session", "VPs"), ("weekly", "VPw"), ("mtd", "VPm"), ("ytd", "VPy")]:
        vp = (vp_dict or {}).get(tf)
        if not vp:
            continue
        _add(vp.get("poc"), f"{tl}POC")
        _add(vp.get("val"), f"{tl}VAL")
        _add(vp.get("vah"), f"{tl}VAH")
        for hvn in vp.get("hvn", []):
            _add(hvn["price"], f"{tl}HVN")

    # Sort by distance
    if direction == "SHORT":
        levels.sort(key=lambda x: entry - x[0])
    else:
        levels.sort(key=lambda x: x[0] - entry)

    # Deduplicate (niveles a < 0.04c se fusionan)
    deduped = []
    for price, name in levels:
        if not deduped or abs(price - deduped[-1][0]) > 0.04:
            deduped.append((price, name))

    return deduped


def _structural_targets(levels, entry, risk_pts, direction, atr_daily):
    """
    Selecciona T1/T2/T3 de niveles estructurales.
    - T1: minimo 1.0R, dentro del 50% del ATR diario
    - T2: minimo 1.5R, dentro del 80% del ATR diario
    - T3: minimo 2.0R, dentro del 100% del ATR diario
    - Fallback a fracciones del ATR diario si no hay nivel estructural
    """
    def dist(p):
        return (entry - p) if direction == "SHORT" else (p - entry)

    t_data = [(None, "", 1.0, 0.50),
              (None, "", 1.5, 0.80),
              (None, "", 2.0, 1.00)]

    result = [None, None, None]
    names  = ["",   "",   ""]

    for price, name in levels:
        d = dist(price)
        if d <= 0:
            continue
        rr_val    = d / risk_pts  if risk_pts  > 0 else 0
        pct_range = d / atr_daily if atr_daily > 0 else 1.0

        for i, (_, _, min_rr, max_pct) in enumerate(t_data):
            if result[i] is None and rr_val >= min_rr and pct_range <= max_pct:
                prev = result[i-1] if i > 0 else None
                if prev is None or abs(price - prev) > 0.04:
                    result[i] = round(price, 4)
                    names[i]  = name
                    break

    # Fallback: fracciones del ATR diario
    fracs = [0.35, 0.65, 0.90]
    for i, (t, frac) in enumerate(zip(result, fracs)):
        if t is None:
            if direction == "SHORT":
                result[i] = round(entry - frac * atr_daily, 4)
            else:
                result[i] = round(entry + frac * atr_daily, 4)
            names[i] = f"{int(frac*100)}% ATR"

    return result[0], names[0], result[1], names[1], result[2], names[2]


# ── Adverse scenario ─────────────────────────────────────────────────────────

def _adverse_scenario(entry, stop, direction, vwap_bands, vp_dict):
    """
    Si el trade falla (stop alcanzado), donde puede ir el mercado.
    Returns: lista [{price, name, dist_from_stop}] de mas cercano a mas lejano.
    """
    adverse_dir = "LONG" if direction == "SHORT" else "SHORT"
    sign = 1 if direction == "SHORT" else -1   # adverso: subida para SHORT, bajada para LONG

    levels = []

    def _add(price, name):
        if price is None:
            return
        d = sign * (float(price) - stop)  # distancia desde el stop en direccion adversa
        if d > 0:
            levels.append({"price": round(float(price), 4), "name": name,
                           "dist_from_stop": round(d, 4)})

    # VWAP bands
    for key, prefix in [("session", "VWAPses"), ("mtd", "VWAPmtd"), ("ytd", "VWAPytd")]:
        vb = (vwap_bands or {}).get(key)
        if not vb:
            continue
        for lk, ls in [("upper_1", "+1s"), ("upper_2", "+2s"), ("vwap", ""),
                        ("lower_1", "-1s"), ("lower_2", "-2s")]:
            _add(vb.get(lk), f"{prefix}{ls}")

    # VP nodes
    for tf, tl in [("weekly", "VPw"), ("mtd", "VPm"), ("ytd", "VPy")]:
        vp = (vp_dict or {}).get(tf)
        if not vp:
            continue
        _add(vp.get("poc"), f"{tl}POC")
        _add(vp.get("vah"), f"{tl}VAH")
        for hvn in vp.get("hvn", []):
            _add(hvn["price"], f"{tl}HVN")

    levels.sort(key=lambda x: x["dist_from_stop"])

    # Deduplicate
    deduped = []
    for lv in levels:
        if not deduped or abs(lv["price"] - deduped[-1]["price"]) > 0.04:
            deduped.append(lv)

    return deduped[:4]


# ── Main function ─────────────────────────────────────────────────────────────

def compute_trade_setup(
    session:      Session,
    direction:    str,
    decision:     str,
    entry_price:  float = None,
    vwap_bands:   dict  = None,
    vp_dict:      dict  = None,
    cluster_stop: float = None,
) -> dict | None:
    """
    Trade card intradiario cuantitativo.

    Stop    : 2xATR(30m) o swing structure (el mas conservador)
    Targets : niveles estructurales (VWAP / VP / prev-day) capados
              por el rango esperado del dia segun volumen-pace
    Adverse : escenario si el stop se activa
    """
    direction = direction.upper()

    if entry_price is None:
        row = session.execute(text("""
            SELECT close FROM price_bars
            WHERE instrument = 'SBN26' AND interval = '30m'
            ORDER BY datetime DESC LIMIT 1
        """)).fetchone()
        if not row:
            return None
        entry_price = float(row[0])

    # ATR
    intra     = _intraday_df(session, "SBN26", "30m", 80)
    daily     = _daily_df(session, "SB_CONT", 30)
    atr_30m   = _atr(intra, 14) if intra is not None and len(intra) >= 15 else None
    atr_daily = _atr(daily, 14) if daily is not None and len(daily) >= 15 else None
    if atr_30m is None or atr_daily is None:
        return None

    # Stop
    stop_atr_30m = round(entry_price - 2.0 * atr_30m, 4) if direction == "LONG" \
               else round(entry_price + 2.0 * atr_30m, 4)
    swing = None
    if intra is not None and len(intra) >= 5:
        swing = (_swing_low(intra, 25, atr_30m) if direction == "LONG"
                 else _swing_high(intra, 25, atr_30m))
    if swing is not None:
        swing = round(swing, 4)
    if swing is not None:
        stop_final = min(stop_atr_30m, swing) if direction == "LONG" else max(stop_atr_30m, swing)
        stop_type  = "Swing" if stop_final == swing else "2xATR(30m)"
    else:
        stop_final = stop_atr_30m
        stop_type  = "2xATR(30m)"

    # Cluster stop: usar si es mas ajustado que el stop actual
    # SHORT: cluster_stop valido si < stop_final (stop mas bajo = mas ajustado para SHORT)
    # LONG:  cluster_stop valido si > stop_final (stop mas alto = mas ajustado para LONG)
    if cluster_stop is not None:
        cluster_stop = round(cluster_stop, 4)
        use_cluster = (
            (direction == "SHORT" and cluster_stop < stop_final) or
            (direction == "LONG"  and cluster_stop > stop_final)
        )
        if use_cluster:
            stop_final = cluster_stop
            stop_type  = "Cluster estructural"

    risk_pts = abs(entry_price - stop_final)
    if risk_pts <= 0:
        return None
    risk_per_lot = round(risk_pts * CONTRACT_SIZE_LBS / 100, 2)

    # Sizing
    max_lots_scoring = LOT_MAP.get(decision, 0)
    lots_by_risk     = int(RISK_PER_TRADE_USD / risk_per_lot) if risk_per_lot > 0 else 0
    lots_final       = min(lots_by_risk, max_lots_scoring) if max_lots_scoring > 0 else 0
    total_risk       = round(lots_final * risk_per_lot, 2)

    # Rango esperado del dia (range model)
    from services.range_model import estimate_full_range, get_today_ohlc
    today_ohlc  = get_today_ohlc(session, "SBN26")
    range_so_far = today_ohlc["range"] if today_ohlc else 0.0
    n_bars_today = today_ohlc["n_bars"] if today_ohlc else 0

    # Obtener pace_ratio desde inputs de C2 si disponible, o calcular
    row_pace = session.execute(text("""
        WITH ranked AS (
            SELECT DATE(datetime) AS day,
                   volume,
                   ROW_NUMBER() OVER (PARTITION BY DATE(datetime) ORDER BY datetime) AS bar_num
            FROM price_bars
            WHERE instrument = 'SBN26' AND interval = '30m'
              AND DATE(datetime) < CURRENT_DATE
        ),
        daily_cv AS (
            SELECT day, SUM(volume) AS cumvol
            FROM ranked WHERE bar_num <= :n
            GROUP BY day ORDER BY day DESC LIMIT 20
        )
        SELECT AVG(cumvol) FROM daily_cv
    """), {"n": max(n_bars_today, 1)}).fetchone()

    today_cumvol = 0
    if today_ohlc and n_bars_today > 0:
        rows_cv = session.execute(text("""
            SELECT SUM(volume) FROM price_bars
            WHERE instrument = 'SBN26' AND interval = '30m'
              AND DATE(datetime) = CURRENT_DATE
        """)).fetchone()
        today_cumvol = float(rows_cv[0]) if rows_cv and rows_cv[0] else 0

    avg_cv = float(row_pace[0]) if row_pace and row_pace[0] else None
    pace_ratio = round(today_cumvol / avg_cv, 2) if avg_cv and avg_cv > 0 else 1.0

    range_info = estimate_full_range(session, "SBN26", pace_ratio, n_bars_today, atr_daily)
    remaining  = range_info["remaining_range"]

    # Structural targets (capped by remaining range)
    # Obtener VWAP bands y VP si no se pasaron
    if vwap_bands is None:
        try:
            from services.anchored_vwap import get_vwap_bands
            vwap_bands = get_vwap_bands(session, "SBN26")
        except Exception:
            vwap_bands = {}

    if vp_dict is None:
        try:
            from services.volume_profile import get_multiframe_vp
            vp_dict = get_multiframe_vp(session, "SBN26")
        except Exception:
            vp_dict = {}

    max_target_dist = atr_daily          # hasta 1×ATR diario desde la entrada
    struct_levels   = _collect_levels_for_targets(direction, entry_price, vwap_bands, vp_dict,
                                                   max_target_dist)
    t1, t1_name, t2, t2_name, t3, t3_name = _structural_targets(
        struct_levels, entry_price, risk_pts, direction, atr_daily
    )

    def rr(t):
        return round(abs(t - entry_price) / risk_pts, 1) if risk_pts > 0 else 0

    # Gate R/R minimo: evaluar antes de calcular P&L (pnl usa lots_final)
    rr_t2_val = rr(t2)
    rr_gate_passed = rr_t2_val >= RR_MIN_T2
    if not rr_gate_passed:
        rr_gate_reason = "R/R T2=%.1fx < minimo %.1fx" % (rr_t2_val, RR_MIN_T2)
        lots_final = 0
        total_risk = 0.0
    else:
        rr_gate_reason = None

    def pnl(t):
        return round(lots_final * abs(t - entry_price) * CONTRACT_SIZE_LBS / 100, 0)

    # Escenario adverso
    adverse = _adverse_scenario(entry_price, stop_final, direction, vwap_bands, vp_dict)

    # COT
    cot_pct, cot_net, cot_ctx, cot_label = _cot_full(session)

    return {
        "direction":         direction,
        "entry":             entry_price,
        "atr_30m":           round(atr_30m, 4),
        "atr_daily":         round(atr_daily, 4),
        "stop_atr_30m":      stop_atr_30m,
        "stop_swing":        swing,
        "stop_final":        stop_final,
        "stop_type":         stop_type,
        "risk_pts":          round(risk_pts, 4),
        "risk_per_lot_usd":  risk_per_lot,
        "lots_by_risk":      lots_by_risk,
        "lots_max_scoring":  max_lots_scoring,
        "lots_final":        lots_final,
        "total_risk_usd":    total_risk,
        "pct_account":       round(total_risk / 500_000 * 100, 2),
        # Gate R/R
        "rr_gate_passed":    rr_gate_passed,
        "rr_gate_reason":    rr_gate_reason,
        # Targets estructurales
        "t1": t1, "t1_name": t1_name, "rr_t1": rr(t1), "t1_usd": pnl(t1),
        "t2": t2, "t2_name": t2_name, "rr_t2": rr_t2_val,  "t2_usd": pnl(t2),
        "t3": t3, "t3_name": t3_name, "rr_t3": rr(t3), "t3_usd": pnl(t3),
        # Rango esperado del dia
        "range_info":        range_info,
        "range_so_far":      round(range_so_far, 4),
        "pace_ratio":        pace_ratio,
        "today_ohlc":        today_ohlc,
        # Escenario adverso
        "adverse":           adverse,
        # COT (nivel × velocidad — modelo compuesto)
        "cot_percentile":     cot_pct,
        "cot_net":            cot_net,
        "cot_label":          cot_label,
        "cot_level_regime":   cot_ctx.get("level_regime"),
        "cot_velocity_class": cot_ctx.get("velocity_class"),
        "cot_weekly_z":       cot_ctx.get("mm_weekly_z"),
        "cot_composite":      cot_ctx.get("composite_state"),
        "cot_conviction":     cot_ctx.get("conviction"),
        "cot_context_str":    cot_ctx.get("context_str"),
        "cot_recent_pct":     cot_ctx.get("mm_pct_1yr"),
        "cot_trend_4wk":      cot_ctx.get("mm_trend_4wk"),
        "cot_change_1wk":     cot_ctx.get("mm_change_1wk"),
        "cot_change_4wk":     cot_ctx.get("mm_change_4wk"),
        "cot_hist_min":       cot_ctx.get("mm_3yr_min"),
        "cot_hist_max":       cot_ctx.get("mm_3yr_max"),
        "decision":          decision,
    }
