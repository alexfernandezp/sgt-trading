"""
Scoring diario SGT Trading - modelo de dos capas.
Ejecutar cada manana despues del pipeline.
Uso: py scripts/score_today.py
"""
import sys, os, logging
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)

from database import SessionLocal
from services.scoring import compute_auto_scores, save_scoring, get_current_price
from services.trade_setup import compute_trade_setup
from services.backtest import estimate_win_rate
from services.anchored_vwap import get_vwap_bands
from services.entry_zone import compute_entry_zone
from services.volume_profile import get_multiframe_vp, nearest_vp_level
from services.market_structure import compute_market_structure
from ingestion.intraday import fetch_intraday
from ingestion.options import score_options, get_latest_files

# ── Label dictionaries ────────────────────────────────────────────────────────

LABELS = {
    "a1_spec_vs_mean":  "A1  Especuladores net vs media historica",
    "a2_spec_change":   "A2  Cambio semanal specs (inv: baja=LONG, sube=SHORT)",
    "a3_comm_vs_mean":  "A3  Comerciales net vs media 13 semanas",
    "b1_spread":        "B1  Spread SBN/SBV contango vs backwardation",
    "b2_price_vs_ma20": "B2  Z-score precio vs media 26w (|z|>1.5)",
    "b3_vwap":          "B3  Precio actual vs VWAP sesion",
    "c1_key_level":     "C1  Precio cerca nivel tecnico relevante     [MANUAL]",
    "c2_open_volume":   "C2  Volumen apertura 30min vs media 60d",
    "c3_options":       "C3  Estructura opciones put/call ratio       [CSV/manual]",
    "d1_event_risk":    "D1  Sin evento alto impacto <24h             [MANUAL VETO]",
    "d2_liquidity":     "D2  Liquidez normal, no vencimiento/festivo  [MANUAL VETO]",
    "d3_drawdown":      "D3  Drawdown cuenta <5% ultimas 2 semanas",
}

# Layer 1: weekly bias — COT + structural market state
LAYER1_KEYS = ["a1_spec_vs_mean", "a2_spec_change", "a3_comm_vs_mean",
               "b1_spread", "b2_price_vs_ma20"]

# Layer 2 auto signals
LAYER2_SCORE_KEYS = ["b3_vwap", "c2_open_volume"]

# Full Layer 2 auto keys (for strength calculation)
LAYER2_AUTO_KEYS = ["b3_vwap", "vwap_mtd", "prev_day_break", "vp_weekly_poc",
                    "c2_open_volume", "c2b_vwap_sigma", "or_breakout", "vwap_touch",
                    "swing_structure"]

LAYER2_LABELS = {
    "b3_vwap":          "L2-1  VWAP sesion (1m)",
    "vwap_mtd":         "L2-2  VWAP MTD - tendencia mensual",
    "prev_day_break":   "L2-3  Ruptura dia anterior H/L",
    "vp_weekly_poc":    "L2-4  VP semanal - precio vs POC",
    "c2_open_volume":   "L2-5  Vol apertura precio-volumen (C2)",
    "c2b_vwap_sigma":   "L2-6  Extension sigma VWAP sesion",
    "or_breakout":      "L2-7  Opening Range breakout/rechazo",
    "vwap_touch":       "L2-8  VWAP multi-toque rechazo",
    "swing_structure":  "L2-9  Estructura swing 15m/5m",
    "c1_key_level":     "L2-10 Nivel tecnico clave C1       [MANUAL]",
}

# Criteria that invert for SHORT
DIRECTIONAL = {"a1_spec_vs_mean", "a2_spec_change", "a3_comm_vs_mean",
               "b1_spread", "b2_price_vs_ma20", "b3_vwap"}

# All auto keys (for DB save compatibility)
AUTO_KEYS = ["a1_spec_vs_mean", "a2_spec_change", "a3_comm_vs_mean",
             "b1_spread", "b2_price_vs_ma20", "b3_vwap",
             "c2_open_volume", "d3_drawdown"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tag(score):
    if score == 1:
        return "[OK]"
    if score == 0:
        return "[--]"
    return "[??]"


def _auto_sum(scores):
    return sum(v for v in scores.values() if v is not None and v in (0, 1))


def _layer_sum(sig, keys):
    return sum(sig.get(k) or 0 for k in keys if sig.get(k) is not None and sig.get(k) in (0, 1))


def _layer_valid(sig, keys):
    return sum(1 for k in keys if sig.get(k) is not None and sig.get(k) in (0, 1))


def _strength(score, valid):
    if valid == 0:
        return "NEUTRO"
    if score >= min(4, valid):
        return "FUERTE"
    if score >= 3:
        return "MODERADO"
    return "DEBIL"


def _vwap_gate(vwap_data, direction):
    """
    Gate de entrada basado en sigma del VWAP de sesion.

    BLOCKED : |sigma| > 2.0 opuesto a direction  → no operar
    WARN    : |sigma| 1.0-2.0 opuesto             → reducir size
    CLEAR   : dentro de rango normal

    Returns (blocked, sigma, level, message)
    """
    sv = (vwap_data or {}).get("session")
    if not sv:
        return False, None, "CLEAR", None
    sigma = sv.get("sigma_pos", 0)
    vwap  = sv.get("vwap", 0)
    lo1   = sv.get("lower_1", 0)
    hi1   = sv.get("upper_1", 0)

    if direction == "SHORT" and sigma < -2.0:
        msg = ("GATE VWAP: precio a %.2fσ bajo VWAP sesion — zona de rebote estadistico.\n"
               "  SHORT BLOQUEADO ahora. Esperar bounce al rango [-1σ, VWAP] (%.4f - %.4f).\n"
               "  Entrada SHORT optima: rechazo en ese rango con L2-9 5m confirmado.") % (sigma, lo1, vwap)
        return True, sigma, "BLOCKED", msg
    if direction == "LONG" and sigma > +2.0:
        msg = ("GATE VWAP: precio a +%.2fσ sobre VWAP sesion — zona de rechazo estadistico.\n"
               "  LONG BLOQUEADO ahora. Esperar pullback al rango [VWAP, +1σ] (%.4f - %.4f).\n"
               "  Entrada LONG optima: soporte en ese rango con L2-9 5m confirmado.") % (sigma, vwap, hi1)
        return True, sigma, "BLOCKED", msg
    if direction == "SHORT" and sigma < -1.0:
        msg = ("AVISO VWAP: sigma=%.2f, SHORT en zona de posible rebote.\n"
               "  Reducir size 40%%. Confirmar L2-9 5m bajista antes de entrar.") % sigma
        return False, sigma, "WARN", msg
    if direction == "LONG" and sigma > +1.0:
        msg = ("AVISO VWAP: sigma=+%.2f, LONG en zona de posible rechazo.\n"
               "  Reducir size 40%%. Confirmar L2-9 5m alcista antes de entrar.") % sigma
        return False, sigma, "WARN", msg
    return False, sigma, "CLEAR", None


def _get_opening_range(session, instrument="SBN26"):
    """High/Low de las primeras 2 barras 30m de hoy (primera hora = Opening Range)."""
    from sqlalchemy import text
    from datetime import date
    rows = session.execute(text("""
        SELECT high, low FROM price_bars
        WHERE instrument = :instr AND interval = '30m'
          AND DATE(datetime) = :d
        ORDER BY datetime ASC LIMIT 2
    """), {"instr": instrument, "d": date.today()}).fetchall()
    if not rows:
        return None, None
    highs = [float(r[0]) for r in rows]
    lows  = [float(r[1]) for r in rows]
    return round(max(highs), 4), round(min(lows), 4)


def _build_layer2(session, scores, mtf, vp_dict, price, direction, vwap_data=None):
    """Combine auto scores + MTF + VP + VWAP sigma + OR into Layer 2 signal dict."""
    sig = {
        "b3_vwap":        scores.get("b3_vwap"),
        "c2_open_volume": scores.get("c2_open_volume"),
        "c1_key_level":   None,  # manual, filled later
    }

    # VWAP MTD — tendencia mensual (ya calculado en vwap_data)
    mtd = (vwap_data or {}).get("mtd")
    if mtd and price:
        sig["vwap_mtd"] = 1 if (price > mtd["vwap"] if direction == "LONG" else price < mtd["vwap"]) else 0
        sig["_mtd_vwap"]  = mtd["vwap"]
        sig["_mtd_sigma"] = mtd.get("sigma_pos")
    else:
        sig["vwap_mtd"]   = None
        sig["_mtd_vwap"]  = None
        sig["_mtd_sigma"] = None

    # Prev day break — precio sobre H / bajo L del dia anterior
    from services.mtf_alignment import _prev_day as _pd
    prev = _pd(session, "SBN26")
    if prev and price:
        if direction == "LONG":
            sig["prev_day_break"] = 1 if price > prev["high"] else 0
        else:
            sig["prev_day_break"] = 1 if price < prev["low"] else 0
        sig["_prev_high"] = prev["high"]
        sig["_prev_low"]  = prev["low"]
    else:
        sig["prev_day_break"] = None
        sig["_prev_high"] = None
        sig["_prev_low"]  = None

    # VP weekly POC
    vp_weekly = (vp_dict or {}).get("weekly")
    if vp_weekly and price:
        poc = vp_weekly.get("poc")
        if poc:
            sig["vp_weekly_poc"] = 1 if (price >= poc if direction == "LONG" else price <= poc) else 0
        else:
            sig["vp_weekly_poc"] = None
    else:
        sig["vp_weekly_poc"] = None

    # VWAP sesion sigma — señal de extension/media reversion
    # SHORT: precio a >+1.0σ del VWAP de sesion = resistencia estadistica
    # LONG:  precio a <-1.0σ del VWAP de sesion = soporte estadistico
    sv = (vwap_data or {}).get("session")
    if sv:
        sigma = sv.get("sigma_pos", 0)
        sig["c2b_vwap_sigma"] = 1 if (sigma > 1.0 if direction == "SHORT" else sigma < -1.0) else 0
        sig["_session_sigma"] = sigma   # para display
    else:
        sig["c2b_vwap_sigma"] = None
        sig["_session_sigma"] = None

    # Opening Range breakout/rechazo
    # LONG:  precio > OR High  -> breakout alcista confirmado  [OK]
    # SHORT: precio < OR Low   -> breakout bajista confirmado  [OK]
    # Dentro del OR -> ambos [--] (zona de indecision)
    or_hi, or_lo = _get_opening_range(session)
    if or_hi is not None and price is not None:
        if direction == "LONG":
            sig["or_breakout"] = 1 if price > or_hi else 0
        else:
            sig["or_breakout"] = 1 if price < or_lo else 0
    else:
        sig["or_breakout"] = None
    sig["_or_hi"] = or_hi
    sig["_or_lo"] = or_lo

    # VWAP multi-toque (L2-8)
    # ATR 30m rapido para calibrar tolerancia de toque
    try:
        from sqlalchemy import text as _text
        from services.vwap_touch import compute_vwap_touch_signal
        _atr_rows = session.execute(_text("""
            SELECT high, low, close FROM price_bars
            WHERE instrument = 'SBN26' AND interval = '30m'
            ORDER BY datetime DESC LIMIT 20
        """)).fetchall()
        if len(_atr_rows) >= 14 and price is not None:
            import pandas as _pd
            _df = _pd.DataFrame(_atr_rows, columns=["high", "low", "close"])
            for _c in _df.columns:
                _df[_c] = _pd.to_numeric(_df[_c], errors="coerce")
            _df = _df.iloc[::-1].reset_index(drop=True)
            _pc = _df["close"].shift(1)
            _tr = _pd.concat([(_df["high"]-_df["low"]),
                               (_df["high"]-_pc).abs(),
                               (_df["low"]-_pc).abs()], axis=1).max(axis=1)
            _atr_30m = float(_tr.rolling(14).mean().dropna().iloc[-1])
            touch_result = compute_vwap_touch_signal(
                session, "SBN26", vwap_data, direction, price, _atr_30m)
            sig["vwap_touch"]  = touch_result.get("signal")
            sig["_touch_data"] = touch_result
        else:
            sig["vwap_touch"]  = None
            sig["_touch_data"] = {}
    except Exception as _e:
        logger.debug("vwap_touch error: %s", _e)
        sig["vwap_touch"]  = None
        sig["_touch_data"] = {}

    # Swing structure (L2-9): 30m contexto + 5m entrada
    try:
        ms = compute_market_structure(session, "SBN26", direction)
        sig["swing_structure"] = ms.get("signal")
        sig["_ms_data"]        = ms
    except Exception as _e:
        logger.debug("market_structure error: %s", _e)
        sig["swing_structure"] = None
        sig["_ms_data"]        = {}

    return sig


# ── Layer display ─────────────────────────────────────────────────────────────

def _detail_for_key(key, inputs):
    if key == "a1_spec_vs_mean" and "spec_net" in inputs:
        return "  spec={:+,}  P_hist={:.0f}%  P_3m={:.0f}%  tend4s={:+,}  chg2s={:+,}  [{}]".format(
            int(inputs["spec_net"]),
            inputs.get("spec_alltime_pct", 0),
            inputs.get("spec_3m_pct", 0),
            int(inputs.get("spec_trend_4wk", 0)),
            int(inputs.get("spec_change_2wk", 0)),
            inputs.get("cot_regime", ""))
    if key == "a2_spec_change" and "spec_change_wk" in inputs:
        chg = int(inputs["spec_change_wk"])
        signal = "LONG (spec reduce)" if chg < 0 else "SHORT (spec aumenta)"
        return "  1s={:+,}  4s={:+,}  -> {}".format(
            chg, int(inputs.get("spec_change_4wk", 0)), signal)
    if key == "a3_comm_vs_mean" and "comm_net" in inputs:
        comm = int(inputs["comm_net"])
        ref  = inputs.get("comm_mean_13w") or inputs.get("comm_mean_hist")
        if ref is not None:
            signal = "LONG (menos hedgeado)" if comm > ref else "SHORT (más hedgeado)"
            return "  comm={:+,}  13w_mean={:+,}  -> {}".format(comm, int(ref), signal)
        return "  comm={:+,}".format(comm)
    if key == "b1_spread" and inputs.get("sbn26"):
        return "  SBN=%.4f SBV=%.4f spread=%+.3f" % (inputs["sbn26"], inputs["sbv26"], inputs["spread_sbn_sbv"])
    if key == "b2_price_vs_ma20" and "b2_z26" in inputs:
        z    = inputs.get("b2_z26", 0)
        mean = inputs.get("mean_26w", 0)
        zone = inputs.get("b2_zone", "NEUTRAL")
        return "  precio=%.4f  mean26w=%.4f  z=%+.3f  [%s]" % (
            inputs.get("price", 0), mean, z, zone)
    if key == "b3_vwap" and "vwap" in inputs:
        return "  precio=%.4f VWAP=%.4f" % (inputs.get("price", 0), inputs.get("vwap", 0))
    if key == "c2_open_volume" and "open_ratio" in inputs:
        or_ = inputs.get("open_ratio", "?")
        vc  = inputs.get("vol_class", "")
        pu  = inputs.get("price_up_from_open")
        pm  = inputs.get("price_move_from_open")
        pr  = inputs.get("pace_ratio")
        pcr = inputs.get("prev_close_ratio")
        s   = "  apertura=%s[%s]" % (or_, vc)
        if pu is not None:
            s += "  precio%s%+.3fc" % ("^" if pu else "v", pm or 0)
        if pr is not None:
            s += "  pace=%d%%(%s)" % (int(pr*100), "bajo" if pr < 0.70 else ("alto" if pr > 1.30 else "norm"))
        if pcr is not None:
            s += "  cierre_ayer=%s" % pcr
        return s
    if key == "d3_drawdown":
        dd = inputs.get("max_drawdown_usd") or inputs.get("drawdown_usd")
        return "  dd=$%s" % dd if dd is not None else ""
    return ""


def print_layer1(sl, sr, inputs):
    """CAPA 1 - Sesgo semanal: A1/A2/A3/B1/B2."""
    print()
    print("=" * 72)
    print("  CAPA 1 - SESGO SEMANAL (COT + estructura)              LONG   SHORT")
    print("=" * 72)

    for key in LAYER1_KEYS:
        label  = LABELS[key][:46]
        tl     = _tag(sl.get(key))
        tr     = _tag(sr.get(key))
        detail = _detail_for_key(key, inputs)
        print("  %-46s %s     %s%s" % (label, tl, tr, detail))

    l1_l = _layer_sum(sl, LAYER1_KEYS)
    l1_r = _layer_sum(sr, LAYER1_KEYS)
    l1_valid = _layer_valid(sl, LAYER1_KEYS)
    print("-" * 72)

    if l1_l > l1_r:
        l1_dir = "LONG"
        l1_sc  = l1_l
    elif l1_r > l1_l:
        l1_dir = "SHORT"
        l1_sc  = l1_r
    else:
        l1_dir = "NEUTRAL"
        l1_sc  = 0

    strength = _strength(l1_sc, l1_valid)
    print("  %-46s LONG=%d/%d  SHORT=%d/%d   -> %s %s" % (
        "Sesgo Capa 1:", l1_l, l1_valid, l1_r, l1_valid, l1_dir, strength))

    # Confianza OOS por direccion (validado 2023-2025)
    if l1_dir == "SHORT":
        print("  Confianza OOS: ALTA  (6/6 senales SHORT aguantan OOS 2023+)")
    elif l1_dir == "LONG":
        print("  Confianza OOS: BAJA  (solo B2_Z26w + OI_DIV validos OOS en LONG)")
        print("  [!] LONG requiere B2_Z26w z<-1.5 y/o OI_DIV activo para alta conviccion")


def print_layer2(l2l, l2r, vp_dict, price, inputs=None, vwap_data=None):
    """CAPA 2 - Ejecucion intradiaria: VWAP + MTF + VP + volumen."""
    print()
    print("=" * 72)
    print("  CAPA 2 - EJECUCION INTRADIARIA (tecnico/timing)        LONG   SHORT")
    print("=" * 72)

    L2_DISPLAY_KEYS = ["b3_vwap", "vwap_mtd", "prev_day_break", "vp_weekly_poc",
                       "c2_open_volume", "c2b_vwap_sigma", "or_breakout", "vwap_touch",
                       "swing_structure"]

    for key in L2_DISPLAY_KEYS:
        label = LAYER2_LABELS[key][:46]
        tl    = _tag(l2l.get(key))
        tr    = _tag(l2r.get(key))

        # Inline details
        detail = ""
        if key == "b3_vwap":
            sv = (vwap_data or {}).get("session")
            if sv:
                detail = "  precio=%.4f  VWAP_ses=%.4f  sigma=%+.2f" % (
                    price or 0, sv["vwap"], sv.get("sigma_pos", 0))
        elif key == "vwap_mtd":
            mtd_v = l2l.get("_mtd_vwap")
            mtd_s = l2l.get("_mtd_sigma")
            if mtd_v is not None:
                detail = "  VWAP_MTD=%.4f  precio=%.4f  sigma=%+.2f" % (mtd_v, price or 0, mtd_s or 0)
        elif key == "prev_day_break":
            ph = l2l.get("_prev_high")
            pl = l2l.get("_prev_low")
            if ph is not None:
                detail = "  prev_H=%.4f  prev_L=%.4f  precio=%.4f" % (ph, pl, price or 0)
        elif key == "vp_weekly_poc":
            vp_w = (vp_dict or {}).get("weekly")
            if vp_w:
                poc = vp_w.get("poc", 0)
                detail = "  POC=%.4f  precio=%s" % (poc, "%.4f" % price if price else "?")
        elif key == "c2_open_volume":
            # Mostrar señal precio-volumen para LONG (ref)
            or_ = inputs.get("open_ratio")
            vc  = inputs.get("vol_class", "?")
            pu  = inputs.get("price_up_from_open")
            pm  = inputs.get("price_move_from_open")
            pr  = inputs.get("pace_ratio")
            if or_ is not None:
                dir_sym = "^" if pu else ("v" if pu is False else "?")
                pace_s  = ("  pace=%d%%(%s)" % (int(pr*100), "bajo" if pr < 0.70 else ("alto" if pr > 1.30 else "norm"))) if pr is not None else ""
                detail  = "  apertura=%s[%s]%s  precio%s%+.3fc" % (or_, vc, pace_s, dir_sym, pm or 0)
        elif key == "c2b_vwap_sigma":
            sigma = l2l.get("_session_sigma")
            if sigma is not None:
                detail = "  sigma_sesion=%+.2f  (umbral +/-1.0)" % sigma
        elif key == "or_breakout":
            or_hi = l2l.get("_or_hi")
            or_lo = l2l.get("_or_lo")
            if or_hi is not None and price is not None:
                if price > or_hi:
                    pos = "SOBRE OR (breakout alcista)"
                elif price < or_lo:
                    pos = "BAJO OR (breakout bajista)"
                else:
                    pos = "DENTRO OR (indecision)"
                detail = "  OR=[%.4f - %.4f]  precio=%.4f  %s" % (or_lo, or_hi, price, pos)
        elif key == "vwap_touch":
            td = l2l.get("_touch_data") or {}
            if td.get("level_name"):
                rej   = td.get("rejection_prob")
                n_h   = td.get("n_hist", 0)
                n_t   = td.get("touches_today", 0)
                wick  = "mecha=SI" if td.get("wick_rejection") else "mecha=no"
                vold  = "vol=v" if td.get("vol_declining") else "vol=-"
                rej_s = ("%.0f%%(N=%d)" % (rej * 100, n_h)) if rej is not None else "N/D"
                detail = "  %s  toques=%d  rechazo_hist=%s  %s  %s" % (
                    td["level_name"], n_t, rej_s, wick, vold)
        elif key == "swing_structure":
            ms = l2l.get("_ms_data") or {}
            if ms.get("pattern_15m"):
                p15   = ms.get("pattern_15m", "?")
                p5    = ms.get("pattern_5m", "?")
                atr_r = ms.get("atr_ratio")
                pos   = ms.get("position_pct")
                atr_s = ("  ATR=%.2fx" % atr_r) if atr_r is not None else ""
                pos_s = ("  pos=%.0f%%" % pos)  if pos  is not None else ""
                detail = "  15m:[%s] 5m:[%s]%s%s" % (p15[:22], p5[:22], atr_s, pos_s)

        print("  %-46s %s     %s%s" % (label, tl, tr, detail))

    l2l_auto = _layer_sum(l2l, LAYER2_AUTO_KEYS)
    l2r_auto = _layer_sum(l2r, LAYER2_AUTO_KEYS)
    l2_valid = _layer_valid(l2l, LAYER2_AUTO_KEYS)
    print("-" * 72)

    if l2l_auto > l2r_auto:
        l2_dir = "LONG"
        l2_sc  = l2l_auto
    elif l2r_auto > l2l_auto:
        l2_dir = "SHORT"
        l2_sc  = l2r_auto
    else:
        l2_dir = "NEUTRAL"
        l2_sc  = 0

    strength = _strength(l2_sc, l2_valid)
    print("  %-46s LONG=%d/%d  SHORT=%d/%d   -> %s %s" % (
        "Trigger Capa 2:", l2l_auto, l2_valid, l2r_auto, l2_valid, l2_dir, strength))


# ── Combined decision ─────────────────────────────────────────────────────────

def compute_combined_decision(sl, sr, l2l, l2r):
    """
    Two-layer decision: L2 is trigger, L1 amplifies/filters.
    Returns (direction, decision_str, rationale).
    decision_str: MAX_CONVICTION / STANDARD / REDUCED / NO_TRADE
    """
    l1_l = _layer_sum(sl, LAYER1_KEYS)
    l1_r = _layer_sum(sr, LAYER1_KEYS)

    l2_la = _layer_sum(l2l, LAYER2_AUTO_KEYS)
    l2_ra = _layer_sum(l2r, LAYER2_AUTO_KEYS)
    l2_valid = _layer_valid(l2l, LAYER2_AUTO_KEYS)

    # Layer 2 direction
    if l2_la > l2_ra:
        l2_dir = "LONG"
        l2_sc  = l2_la
        l2_opp = l2_ra
        l1_sc  = l1_l
        l1_opp = l1_r
    elif l2_ra > l2_la:
        l2_dir = "SHORT"
        l2_sc  = l2_ra
        l2_opp = l2_la
        l1_sc  = l1_r
        l1_opp = l1_l
    else:
        return "NEUTRAL", "NO_TRADE", "L2 sin sesgo claro (%d/%d auto)" % (l2_la, l2_valid)

    l2_strength = _strength(l2_sc, l2_valid)

    if l2_strength == "DEBIL":
        return l2_dir, "NO_TRADE", "L2 insuficiente (%d/%d)" % (l2_sc, l2_valid)

    # L1 alignment
    l1_valid = _layer_valid(sl, LAYER1_KEYS)
    l1_str   = _strength(l1_sc, l1_valid)
    l1_conflict = l1_opp > l1_sc

    if l1_conflict:
        return l2_dir, "REDUCED", "L1 contradice L2 (L1={}/{} contra) - size reducido".format(l1_opp, l1_valid)

    # Decision table
    if l2_strength == "FUERTE" and l1_str == "FUERTE":
        decision  = "MAX_CONVICTION"
        rationale = "L2 FUERTE + L1 FUERTE [L2=%d/%d L1=%d/%d]" % (l2_sc, l2_valid, l1_sc, l1_valid)
    elif l2_strength == "FUERTE" and l1_str == "MODERADO":
        decision  = "STANDARD"
        rationale = "L2 FUERTE + L1 MODERADO [L2=%d/%d L1=%d/%d]" % (l2_sc, l2_valid, l1_sc, l1_valid)
    elif l2_strength == "MODERADO" and l1_str == "FUERTE":
        decision  = "STANDARD"
        rationale = "L2 MODERADO + L1 FUERTE [L2=%d/%d L1=%d/%d]" % (l2_sc, l2_valid, l1_sc, l1_valid)
    elif l2_strength == "FUERTE" and l1_str == "DEBIL":
        decision  = "STANDARD"
        rationale = "L2 FUERTE, L1 debil - no amplificado [L2=%d/%d L1=%d/%d]" % (l2_sc, l2_valid, l1_sc, l1_valid)
    elif l2_strength == "MODERADO" and l1_str == "MODERADO":
        decision  = "REDUCED"
        rationale = "L2 MODERADO + L1 MODERADO [L2=%d/%d L1=%d/%d]" % (l2_sc, l2_valid, l1_sc, l1_valid)
    else:
        decision  = "NO_TRADE"
        rationale = "Senales insuficientes [L2=%d/%d L1=%d/%d]" % (l2_sc, l2_valid, l1_sc, l1_valid)

    return l2_dir, decision, rationale


# ── VWAP bands display ────────────────────────────────────────────────────────

def print_vwap_bands(vwap_data):
    print()
    print("=" * 72)
    print("  VWAP ANCLADO (bandas 3 desviaciones)")
    print("=" * 72)

    for label, key, tf in [("Sesion", "session", "1m"), ("YTD", "ytd", "1h"), ("MTD", "mtd", "30m")]:
        v = vwap_data.get(key)
        if not v:
            print("  %s (%s): sin datos" % (label, tf))
            continue

        price = v["price"]
        vwap  = v["vwap"]
        sigma = v["sigma_pos"]
        zone  = v["zone"]
        n     = v["n_bars"]
        anch  = v["anchor_dt"]

        if sigma < -0.5:
            bias_txt = "-> bias LONG  (precio bajo VWAP)"
        elif sigma > 0.5:
            bias_txt = "-> bias SHORT (precio sobre VWAP)"
        else:
            bias_txt = "-> NEUTRAL (precio en zona VWAP)"

        print()
        print("  %s (desde %s, %s, N=%d barras)" % (label, anch, tf, n))

        all_vals = [v["lower_3"], v["lower_2"], v["lower_1"], vwap,
                    v["upper_1"], v["upper_2"], v["upper_3"]]
        nearest = min(all_vals, key=lambda x: abs(x - price))

        def _bfmt(name, val):
            mk = "*" if val == nearest else " "
            return "%s%s=%.4f" % (mk, name, val)

        print("  Inf:  %s  %s  %s" % (
            _bfmt("-3s", v["lower_3"]),
            _bfmt("-2s", v["lower_2"]),
            _bfmt("-1s", v["lower_1"])))
        print("  VWAP: %.4f   Precio=%.4f  sigma=%+.2f" % (vwap, price, sigma))
        print("  Sup:  %s  %s  %s" % (
            _bfmt("+1s", v["upper_1"]),
            _bfmt("+2s", v["upper_2"]),
            _bfmt("+3s", v["upper_3"])))
        print("  Zona: %s  %s" % (zone, bias_txt))

    bias = vwap_data.get("bias", "NEUTRAL")
    print()
    print("  BIAS VWAP GLOBAL: %s" % bias)


# ── Input helpers ─────────────────────────────────────────────────────────────

def ask(label, is_veto=False):
    veto = " ** VETO si 0 **" if is_veto else ""
    while True:
        val = input("  %s%s  [1/0]: " % (label, veto)).strip()
        if val in ("1", "0"):
            return int(val)
        print("  Introduce 1 o 0.")


def ask_direction(suggestion):
    print()
    opts = ["LONG", "SHORT", "NEUTRAL"]
    while True:
        raw = input("  Confirmar direccion [LONG/SHORT/NEUTRAL] (Enter = %s): " % suggestion).strip().upper()
        if raw == "":
            return suggestion
        if raw in opts:
            return raw
        print("  Introduce LONG, SHORT o NEUTRAL.")


# ── Volume profile display ────────────────────────────────────────────────────

def print_volume_profile(vp_dict, price):
    if not vp_dict or all(v is None for v in vp_dict.values()):
        return

    print("\n  -- VOLUME PROFILE MULTIFRAME --")
    print("  %-8s  %-6s  %-8s  %-8s  %-8s  Nodos HVN cercanos" % (
        "TF", "Barras", "VAL", "POC", "VAH"))
    print("  " + "-" * 68)

    tf_labels = {"session": "Sesion", "weekly": "Semanal", "mtd": "MTD", "ytd": "YTD"}
    for tf in ("session", "weekly", "mtd", "ytd"):
        vp = vp_dict.get(tf)
        if not vp:
            continue
        poc = vp["poc"]
        val = vp["val"]
        vah = vp["vah"]

        if price < val:
            pos = "BAJO VA"
        elif price > vah:
            pos = "SOBRE VA"
        else:
            pos = "EN VA"

        poc_dist = round(price - poc, 4)
        poc_str  = "%.4f (%+.4fc)" % (poc, poc_dist)

        hvn_near = [h for h in vp.get("hvn", []) if abs(h["price"] - price) <= 0.30]
        hvn_str  = "  ".join("%.4f" % h["price"] for h in sorted(hvn_near, key=lambda x: abs(x["price"] - price)))

        print("  %-8s  %-6d  %-8.4f  %-22s  %-8.4f  [%-8s] %s" % (
            tf_labels.get(tf, tf), vp["n_bars"],
            val, poc_str, vah, pos, hvn_str))

    nearby = nearest_vp_level(vp_dict, price, max_dist=0.30)
    if nearby:
        print()
        print("  Nodos liquidez cerca (precio=%.4f):" % price)
        for n in nearby[:8]:
            tag  = "[HVN]" if n["type"] == "HVN" else ("[LVN]" if n["type"] == "LVN" else "[POC]")
            role = "soporte" if n["dist"] <= 0 else "resistencia"
            print("  %s  %-7s  %.4f  (%+.4fc)  [%s]" % (tag, n["tf"], n["price"], n["dist"], role))


# ── Entry zone display ────────────────────────────────────────────────────────

def print_entry_zone(ez):
    if not ez:
        return
    price = ez["price"]
    atr   = ez["atr_30m"]
    rec   = ez["rec"]

    print("\n  -- ZONA DE ENTRADA --")

    # Fibonacci context
    fib = ez.get("fib_data")
    if fib:
        print("  Fibonacci SB_CONT (%d dias):  H=%.4f  L=%.4f  Rango=%.4fc" % (
            fib["lookback"], fib["swing_high"], fib["swing_low"], fib["range_pts"]))
        fib_near = [(lbl, lv) for lbl, lv in fib["levels"].items()
                    if abs(lv["value"] - price) <= 3.0 * atr]
        if fib_near:
            fib_near.sort(key=lambda x: x[1]["value"])
            parts = []
            for lbl, lv in fib_near:
                dist = lv["value"] - price
                parts.append("  %s=%.4f (%+.4fc)" % (lbl, lv["value"], dist))
            print("  Niveles cercanos: %s" % "  ".join(p.strip() for p in parts))
        print()

    levels_sorted = sorted(ez["levels"], key=lambda x: x["value"])
    print("  Mapa de niveles (precio actual = %.4f):" % price)
    for lv in levels_sorted:
        arrow    = "<-- PRECIO AQUI" if abs(lv["dist"]) < 0.5 * atr else ""
        role_tag = "[sup]" if lv["role"] == "support" else "[res]"
        stars    = "*" * lv["sig"]
        dist_str = "%+.4fc" % lv["dist"]
        print("  %s %-5s  %-18s  %.4f  (%s)  %s %s" % (
            role_tag, stars, lv["name"][:18], lv["value"], dist_str,
            arrow, "(ATR x%.1f)" % (abs(lv["dist"]) / atr) if atr > 0 else ""))

    quality_tags = {
        "OPTIMA":   "[***]",
        "BUENA":    "[ **]",
        "MODERADA": "[  *]",
        "ESPERAR":  "[ !! ]",
    }
    qtag = quality_tags.get(rec["quality"], "[?]")
    print()

    cluster = rec.get("cluster")
    if cluster:
        print("  *** CLUSTER DE CONFLUENCIA (%d niveles, sig=%d) ***" % (
            cluster["size"], cluster["total_sig"]))
        for lv in cluster["levels"]:
            print("    %.4f  %-20s  sig=%s" % (lv["value"], lv["name"], "*" * lv["sig"]))
        print()

    print("  %s  Tipo: %-18s  Calidad: %s" % (qtag, rec["type"], rec["quality"]))
    if rec["entry"] is not None:
        print("  Entrada optima : %.4f c/lb" % rec["entry"])
        print("  Zona           : %.4f - %.4f c/lb" % (rec["zone_lo"], rec["zone_hi"]))
        cs = rec.get("cluster_stop")
        if cs is not None:
            print("  Stop cluster   : %.4f c/lb  (%.4fc riesgo vs entrada)" % (
                cs, abs(rec["entry"] - cs)))
    print("  Condicion      : %s" % rec["condition"])
    print("  Razon          : %s" % rec["rationale"])


# ── Market structure display ──────────────────────────────────────────────────

def print_market_structure(ms):
    """Muestra estructura de swing 30m/5m + posicion barra 1m + ATR ratio."""
    if not ms:
        return

    s15 = ms.get("swing_15m", "unclear")
    s5  = ms.get("swing_5m",  "unclear")
    p15 = ms.get("pattern_15m", "sin datos")
    p5  = ms.get("pattern_5m",  "sin datos")
    a15 = ms.get("aligned_15m", False)
    a5  = ms.get("aligned_5m",  False)
    ph15, pl15 = ms.get("last_ph_15m"), ms.get("last_pl_15m")
    ph5,  pl5  = ms.get("last_ph_5m"),  ms.get("last_pl_5m")
    pos = ms.get("position_pct")
    atr = ms.get("atr_ratio")
    n15 = ms.get("n_bars_15m", 0)

    print("\n  -- ESTRUCTURA DE MERCADO (swing 15m / 5m, intradía) --")

    ok15 = "[OK]" if a15 else "[--]"
    ok5  = "[OK]" if a5  else "[--]"
    lvl15 = ("  PH=%.4f  PL=%.4f" % (ph15, pl15)) if ph15 and pl15 else ""
    lvl5  = ("  PH=%.4f  PL=%.4f" % (ph5,  pl5))  if ph5  and pl5  else ""
    n15_s = ("  (N=%d barras 15m)" % n15) if n15 else ""
    print("  %s  15m  %s%s%s" % (ok15, p15, lvl15, n15_s))
    print("  %s   5m  %s%s" % (ok5,  p5,  lvl5))

    if pos is not None:
        pos_desc = "alta" if pos > 70 else ("baja" if pos < 30 else "media")
        bar_bar  = int(pos / 5)
        bar_str  = "[" + "#" * bar_bar + "." * (20 - bar_bar) + "]"
        print("  Posicion barra 1m : %.0f%%  %s  (%s en rango)" % (pos, bar_str, pos_desc))

    if atr is not None:
        if atr > 1.20:
            atr_desc = "EXPANSION"
        elif atr < 0.80:
            atr_desc = "CONTRACCION"
        else:
            atr_desc = "normal"
        print("  ATR ratio (5m)    : %.2fx  [%s]" % (atr, atr_desc))



# ── Trade card ────────────────────────────────────────────────────────────────

def print_trade_card(setup, bt, ez=None, vp=None, ms=None):
    w   = 68
    sgn = "-" if setup["direction"] == "LONG" else "+"
    dir_label = "LONG  (compra)" if setup["direction"] == "LONG" else "SHORT (venta)"

    print()
    print("=" * w)
    print("  TRADE CARD - SETUP INTRADIARIO CUANTITATIVO")
    print("=" * w)

    print("\n  Instrumento  : SBN26 (Sugar No.11 Jul 2026)")
    print("  Direccion    : %s" % dir_label)
    print("  Entrada      : %.4f c/lb" % setup["entry"])
    print("  ATR diario   : %.4f c/lb   ATR 30m: %.4f c/lb" % (setup["atr_daily"], setup["atr_30m"]))

    # Range model
    ri   = setup.get("range_info")
    ohlc = setup.get("today_ohlc")
    if ri:
        conf_map = {"HIGH": "alta", "MED": "media", "LOW": "baja"}
        conf_str = conf_map.get(ri["confidence"], ri["confidence"])
        pace     = setup.get("pace_ratio", 0)
        print("\n  -- TIPO DE DIA Y RANGO ESPERADO --")
        print("  Tipo de dia       : %-14s (pace ratio = %.0f%%)" % (ri["day_type"], pace * 100))
        print("  Rango esperado    : %.4f c/lb" % ri["expected_range"])
        print("  Rango hecho       : %.4f c/lb   Rango restante: %.4f c/lb" % (
            ri["range_so_far"], ri["remaining_range"]))
        print("  Confianza modelo  : %-6s (%d dias similares usados)" % (conf_str, ri["n_similar"]))
        if ohlc:
            print("  OHLC sesion       : O=%.4f  H=%.4f  L=%.4f  C=%.4f  (%d barras)" % (
                ohlc["open"], ohlc["high"], ohlc["low"], ohlc["close"], ohlc["n_bars"]))

    print_market_structure(ms)
    print_volume_profile(vp, setup["entry"])
    print_entry_zone(ez)

    diff_30m = abs(setup["entry"] - setup["stop_atr_30m"])
    print("\n  -- STOP INTRADIARIO --")
    print("  Stop 2xATR(30m) : %.4f c/lb   (%s%.4fc)" % (setup["stop_atr_30m"], sgn, diff_30m))
    if setup["stop_swing"] is not None:
        diff_sw = abs(setup["entry"] - setup["stop_swing"])
        print("  Stop Swing      : %.4f c/lb   (%s%.4fc)" % (setup["stop_swing"], sgn, diff_sw))
    diff_f = abs(setup["entry"] - setup["stop_final"])
    print("  Stop Final      : %.4f c/lb   (%s%.4fc)   [%s] [ACTIVO]" % (
        setup["stop_final"], sgn, diff_f, setup["stop_type"]))
    print("  Riesgo/lote     : ${:,.2f}".format(setup["risk_per_lot_usd"]))

    print("\n  -- SIZING --")
    print("  Scoring decision : %s  (%d lotes max)" % (setup["decision"], setup["lots_max_scoring"]))
    lots_str = str(setup["lots_by_risk"])
    if setup["lots_by_risk"] > setup["lots_max_scoring"] > 0:
        lots_str += "  -> cap scoring: %d" % setup["lots_max_scoring"]
    print("  Lotes por 1%%R    : %s" % lots_str)
    print("  Lotes finales    : %d" % setup["lots_final"])
    if setup["lots_final"] > 0:
        print("  Riesgo total     : ${:,.2f}  ({:.2f}% cuenta)".format(setup["total_risk_usd"], setup["pct_account"]))
    else:
        print("  Riesgo total     : $0  (NO TRADE)")

    rem_str = ("%.4f c/lb" % ri["remaining_range"]) if ri else "N/D"
    print("\n  -- TARGETS ESTRUCTURALES (rango restante = %s) --" % rem_str)
    target_rows = [
        ("T1  [%-16s]" % setup["t1_name"][:16], setup["t1"], setup["rr_t1"], setup["t1_usd"]),
        ("T2  [%-16s]" % setup["t2_name"][:16], setup["t2"], setup["rr_t2"], setup["t2_usd"]),
        ("T3  [%-16s]" % setup["t3_name"][:16], setup["t3"], setup["rr_t3"], setup["t3_usd"]),
    ]
    for i, (lbl, tprice, rr_val, pnl_usd) in enumerate(target_rows):
        marker = "  <- objetivo" if i == 1 else ""
        flag   = "  [R/R BAJO]" if i == 1 and rr_val < 1.0 else (
                 "  [WARN]"     if i == 1 and rr_val < 1.5 else "")
        if setup["lots_final"] > 0:
            print("  {:<24}  {:.4f} c/lb   R/R={:.1f}x   P&L +${:,.0f}{}{}".format(
                lbl, tprice, rr_val, pnl_usd, marker, flag))
        else:
            print("  %-24s  %.4f c/lb   R/R=%.1fx%s%s" % (lbl, tprice, rr_val, marker, flag))

    if not setup.get("rr_gate_passed", True):
        print()
        print("  [!!!] GATE R/R ACTIVADO: %s" % setup.get("rr_gate_reason", ""))
        print("        Lotes = 0  ->  NO TRADE  (R/R insuficiente para arriesgar capital)")

    # Adverse scenario
    adverse = setup.get("adverse", [])
    if adverse:
        print("\n  -- ESCENARIO ADVERSO (si el stop se activa) --")
        print("  Si la operacion falla, el mercado puede continuar hacia:")
        for lv in adverse:
            print("  %.4f c/lb  %-16s  (+%.4fc mas alla del stop)" % (
                lv["price"], lv["name"][:16], lv["dist_from_stop"]))

    if setup.get("cot_percentile") is not None:
        pct      = setup["cot_percentile"]
        net      = setup["cot_net"]
        label    = setup["cot_label"]
        regime   = setup.get("cot_regime", "")
        r52      = setup.get("cot_recent_pct")
        t4wk     = setup.get("cot_trend_4wk")
        t4wk_wk  = setup.get("cot_change_4wk")
        hist_min = setup.get("cot_hist_min")
        hist_max = setup.get("cot_hist_max")
        pct_min  = setup.get("cot_pct_from_min")

        pct3m    = setup.get("cot_3m_pct")
        chg2wk   = setup.get("cot_change_2wk")

        print("\n  -- CONTEXTO COT (regimen de posicionamiento) --")
        print("  Spec net actual  : {:+,}".format(net))
        print("  Percentil hist.  : P{:.0f}  (min: {:+,}  max: {:+,}  pos: {:.0f}% rango)".format(
            pct, hist_min or 0, hist_max or 0, pct_min or 0))
        if pct3m is not None:
            print("  Percentil 3 meses: P{:.0f}  (contexto reciente accionable)".format(pct3m))
        if r52 is not None:
            print("  Percentil 52 sem : P{:.0f}  (referencia anual)".format(r52))
        if chg2wk is not None:
            dir2 = "subiendo" if chg2wk > 0 else "bajando"
            print("  Cambio 2 semanas : {:+,}  ({})".format(chg2wk, dir2))
        if t4wk is not None:
            dir_txt = "subiendo (cubriendo cortos/acumulando largos)" if t4wk > 0 else "bajando (anadiendo cortos/liquidando largos)"
            print("  Tendencia 4 sem  : {:+,}  MA4s {}".format(t4wk, dir_txt))
        if t4wk_wk is not None:
            print("  Cambio 4 sem     : {:+,}".format(t4wk_wk))

        regime_desc = {
            "EXTREMO_CORTO_ABSOLUTO": "EXTREMO ABSOLUTO CORTO - P<=5 histor. cobertura/squeeze inminente  [LONG fuerte]",
            "EXTREMO_LARGO_ABSOLUTO": "EXTREMO ABSOLUTO LARGO - P>=95 histor. liquidacion inminente       [SHORT fuerte]",
            "CONTRARIAN_SHORT":       "CONTRARIAN BAJISTA - extremo historico P>=85 + revirtiendo          [SHORT fuerte]",
            "CROWDED_SHORT":          "CROWDED SHORT - specs apilando cortos en zona baja → contrarian LONG [LONG moderado]",
            "CROWDED_LONG":           "CROWDED LONG  - specs apilando largos en zona alta → contrarian SHORT[SHORT moderado]",
            "NEUTRAL":                "NEUTRAL - sin posicionamiento extremo ni crowded detectable",
        }
        desc = regime_desc.get(regime, regime)
        print("  Regimen COT      : {}".format(desc))

        if setup["direction"] == "LONG":
            if regime == "EXTREMO_CORTO_ABSOLUTO":
                print("  Calidad COT      : MAXIMA  (extremo historico P<=5, squeeze inminente)")
            elif regime == "CROWDED_SHORT":
                print("  Calidad COT      : ALTA    (specs crowded cortos, contrarian long)")
            elif regime in ("CROWDED_LONG", "CONTRARIAN_SHORT", "EXTREMO_LARGO_ABSOLUTO"):
                print("  Calidad COT WARN : BAJA    (specs en contra del LONG - crowded largos/extremo largo)")
            else:
                print("  Calidad COT      : MODERADA  (NEUTRAL - sin señal contrarian clara)")
        else:
            if regime == "EXTREMO_LARGO_ABSOLUTO":
                print("  Calidad COT      : MAXIMA  (extremo historico P>=95, liquidacion inminente)")
            elif regime in ("CONTRARIAN_SHORT", "CROWDED_LONG"):
                print("  Calidad COT      : ALTA    (specs en extremo/crowded largos, contrarian short)")
            elif regime in ("EXTREMO_CORTO_ABSOLUTO", "CROWDED_SHORT"):
                print("  Calidad COT WARN : BAJA    (specs en contra del SHORT - extremo corto/crowded cortos)")
            else:
                print("  Calidad COT      : MODERADA  (NEUTRAL - sin señal contrarian clara)")

    if bt and bt.get("per_target"):
        pt = bt["per_target"]
        print("\n  -- SESGO HISTORICO (N=%d extremos COT, 10 anos) --" % bt["n_trades"])
        print("  [spec<P25 + precio>MA20 | Stop %sxATR diario | Max %dd]" % (bt["atr_mult"], bt["max_hold_days"]))
        for r_key in (1.5, 2.5, 4.0):
            stats = pt.get(r_key)
            if stats:
                print("    %.1fR en 15d : %d%%   AvgR=%+.2f  (W=%d L=%d)" % (
                    r_key, stats["win_rate"] * 100, stats["avg_r"], stats["n_wins"], stats["n_losses"]))
    else:
        print("\n  [!] Sin datos historicos suficientes para backtest")

    print()
    print("=" * w)


# ── Main flow ─────────────────────────────────────────────────────────────────

def run():
    print()
    print("=" * 72)
    print("  SGT TRADING - SCORING DIARIO  (modelo dos capas)")
    print("=" * 72)

    session = SessionLocal()

    # [0] Refresh intraday
    print("\n[0/5] Refrescando barras intraday SBN26...")
    try:
        fetch_intraday(session, instruments=["SBN26"], intervals=["1m", "5m", "30m"])
        price = get_current_price(session, "SBN26")
        print("  Precio actual SBN26: %s c/lb (Yahoo ~10min delay)" % price)
    except Exception as e:
        print("  Aviso: %s" % e)
        price = get_current_price(session, "SBN26")

    # [1] Layer 1: auto scores LONG + SHORT
    print("\n[1/5] CAPA 1 - Sesgo semanal (COT + estructura)...")
    result_l = compute_auto_scores(session, "LONG")
    result_r = compute_auto_scores(session, "SHORT")
    scores_l = result_l["scores"]
    scores_r = result_r["scores"]
    inputs   = result_l["inputs"]

    print_layer1(scores_l, scores_r, inputs)

    # [2] Layer 2: VP + VWAP + señales auto
    print("\n[2/5] CAPA 2 - Ejecucion intradiaria (VP + VWAP + swing + volumen)...")
    try:
        vp_dict = get_multiframe_vp(session)
    except Exception as e:
        print("  Aviso VP: %s" % e)
        vp_dict = {}

    # VWAP calculado aqui: necesario para señales L2 antes de mostrar bandas
    try:
        vwap_data = get_vwap_bands(session)
    except Exception as e:
        print("  Aviso VWAP (pre-L2): %s" % e)
        vwap_data = {}

    l2l = _build_layer2(session, scores_l, None, vp_dict, price, "LONG",  vwap_data)
    l2r = _build_layer2(session, scores_r, None, vp_dict, price, "SHORT", vwap_data)

    print_layer2(l2l, l2r, vp_dict, price, inputs, vwap_data=vwap_data)

    # [3] VWAP bands (ya calculado arriba, solo display)
    print("\n[3/5] VWAP anclado Sesion + YTD + MTD...")
    try:
        print_vwap_bands(vwap_data)
        vwap_bias = vwap_data.get("bias", "NEUTRAL")
    except Exception as e:
        print("  Aviso VWAP: %s" % e)
        vwap_bias = "NEUTRAL"

    # [4] Options C3
    print("\n[4/5] Opciones...")
    opt_c3_long  = None
    opt_c3_short = None
    opt_inputs   = {}
    files = get_latest_files("SBN26")
    if files["chain"]:
        print("  Archivo: %s" % files["chain"].name)
        c3_l, opt_l = score_options("LONG",  "SBN26", price)
        c3_r, opt_r = score_options("SHORT", "SBN26", price)
        if c3_l is not None:
            opt_c3_long  = c3_l
            opt_c3_short = c3_r
            opt_inputs.update({"put_call_ratio_oi": opt_l.get("put_call_ratio_oi"), "max_pain": opt_l.get("max_pain")})
            print("  C3 LONG=%s  SHORT=%s  P/C=%.2f  max_pain=%s" % (
                _tag(c3_l), _tag(c3_r),
                opt_l.get("put_call_ratio_oi", 0) or 0,
                opt_l.get("max_pain", "?")))
        else:
            print("  [!] No se pudo parsear: %s" % opt_l.get("error", ""))
    else:
        print("  Sin CSV de opciones (C3 manual mas adelante)")

    # [5] Combined decision
    direction, decision_auto, rationale = compute_combined_decision(scores_l, scores_r, l2l, l2r)

    print()
    print("=" * 72)
    print("  DECISION COMBINADA (dos capas)")
    print("=" * 72)
    print("  Direccion  : %s" % direction)
    print("  Sizing     : %s" % decision_auto)
    print("  Razon      : %s" % rationale)
    print("  VWAP bias  : %s" % vwap_bias)
    if vwap_bias != "NEUTRAL" and vwap_bias != direction and direction != "NEUTRAL":
        print("  [!] VWAP bias contradice la direccion - revisar")
    # Gate preview: mostrar estado antes de pedir confirmacion
    if direction not in ("NEUTRAL", None):
        _gb, _gs, _gl, _gm = _vwap_gate(vwap_data, direction)
        if _gl == "BLOCKED":
            print("  [!!!] VWAP GATE ACTIVO: precio a %.2fσ — lectura completa tras confirmar" % (_gs or 0))
        elif _gl == "WARN":
            print("  [!]   VWAP WARN: sigma=%.2f — reducir size si confirmas" % (_gs or 0))
    print("=" * 72)

    confirmed_direction = ask_direction(direction)

    if confirmed_direction == "NEUTRAL":
        print("\n  NEUTRAL seleccionado. Sin trade hoy.")
        session.close()
        return

    # ── VWAP sigma gate ───────────────────────────────────────────────────────
    gate_blocked, gate_sigma, gate_level, gate_msg = _vwap_gate(vwap_data, confirmed_direction)
    if gate_level in ("WARN", "BLOCKED"):
        print()
        prefix = "  [!!!]" if gate_blocked else "  [!] "
        for line in gate_msg.split("\n"):
            print("%s %s" % (prefix, line.strip()))
    if gate_blocked:
        print()
        print("  " + "=" * 64)
        print("  GATE ACTIVADO — NO operar en direccion %s en este momento." % confirmed_direction)
        print("  Esperar mean-reversion del VWAP de sesion antes de entrar.")
        print("  " + "=" * 64)
        session.close()
        return

    # Manual criteria for confirmed direction
    print("\n[5/5] Criterios manuales para %s:\n" % confirmed_direction)
    manual = {}

    manual["c1_key_level"] = ask(LABELS["c1_key_level"])

    # C1 also feeds Layer 2 — update l2 signal for sizing
    l2_confirmed = l2l if confirmed_direction == "LONG" else l2r
    l2_confirmed["c1_key_level"] = manual["c1_key_level"]

    c3_auto = opt_c3_long if confirmed_direction == "LONG" else opt_c3_short
    if c3_auto is not None:
        scores_dir = scores_l if confirmed_direction == "LONG" else scores_r
        scores_dir["c3_options"] = c3_auto
        inputs.update(opt_inputs)
        print("  C3  [auto] %s" % _tag(c3_auto))
    else:
        manual["c3_options"] = ask(LABELS["c3_options"])

    manual["d1_event_risk"] = ask(LABELS["d1_event_risk"], is_veto=True)
    manual["d2_liquidity"]  = ask(LABELS["d2_liquidity"],  is_veto=True)
    notes = input("\n  Notas (Enter para omitir): ").strip() or None

    scores_dir = scores_l if confirmed_direction == "LONG" else scores_r
    scoring = save_scoring(session, confirmed_direction, scores_dir, inputs,
                           manual_overrides=manual, notes=notes)

    print()
    print("=" * 72)
    print("  RESULTADO SCORING  %s" % confirmed_direction)
    print("=" * 72)
    print("\n  Score total : %d / 12" % scoring.total_score)
    print("  Veto        : %s" % ("SI - NO OPERAR" if scoring.veto else "No"))
    print("  Decision    : %s" % scoring.decision)
    print("  Lotes max   : %d" % scoring.max_lots)
    if scoring.notes:
        print("  Notas       : %s" % scoring.notes)

    # Override decision with two-layer result if more conservative
    from services.trade_setup import LOT_MAP
    lots_two_layer = LOT_MAP.get(decision_auto, 0)
    lots_scoring   = LOT_MAP.get(scoring.decision, 0)
    final_decision = decision_auto if lots_two_layer <= lots_scoring else scoring.decision
    if final_decision != scoring.decision:
        print("  Decision dos capas: %s  (mas conservador - aplicado)" % final_decision)

    if scoring.decision != "NO_TRADE" and not scoring.veto:
        # Entry zone primero: extraer cluster entry/stop para el trade setup
        print("\n  Calculando zona de entrada...")
        try:
            ez = compute_entry_zone(session, confirmed_direction)
        except Exception as e:
            print("  Aviso entry_zone: %s" % e)
            ez = None

        cluster_entry = None
        cluster_stop  = None
        if ez and ez.get("rec"):
            cluster_entry = ez["rec"].get("entry")
            cluster_stop  = ez["rec"].get("cluster_stop")

        print("  Calculando setup cuantitativo...")
        try:
            setup = compute_trade_setup(
                session, confirmed_direction, final_decision,
                entry_price=cluster_entry or price,
                vwap_bands=vwap_data, vp_dict=vp_dict,
                cluster_stop=cluster_stop,
            )
        except Exception as e:
            print("  Aviso trade_setup: %s" % e)
            setup = None

        if setup and not setup.get("rr_gate_passed", True):
            print()
            print("  " + "!" * 64)
            print("  GATE R/R: %s" % setup["rr_gate_reason"])
            print("  El trade NO cumple R/R minimo — se cancela automaticamente")
            print("  " + "!" * 64)
            final_decision = "NO_TRADE"

        print("  Ejecutando backtest historico...")
        try:
            bt = estimate_win_rate(session, confirmed_direction, atr_mult_stop=1.0)
        except Exception as e:
            print("  Aviso backtest: %s" % e)
            bt = None

        l2_confirmed = l2l if confirmed_direction == "LONG" else l2r
        ms_data      = l2_confirmed.get("_ms_data")

        if setup:
            print_trade_card(setup, bt, ez=ez, vp=vp_dict, ms=ms_data)
        else:
            print("\n  [!] No se pudo calcular el setup (datos insuficientes)")
    else:
        print()

    session.close()


if __name__ == "__main__":
    run()
