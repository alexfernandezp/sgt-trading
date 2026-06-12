"""
SGT Trading Dashboard — Flask backend.
Arrancar: py dashboard/app.py
Abre: http://localhost:5000
"""
import sys, os, json, math, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.WARNING)

from flask import Flask, render_template, jsonify
from database import SessionLocal
from sqlalchemy import text
from datetime import datetime, date, timedelta
from pathlib import Path

app = Flask(__name__)
LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _backdrop():
    path = os.path.join(LOGS_DIR, "fundamental_backdrop.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _safe_float(v, scale=1.0):
    try:
        return round(float(v) * scale, 4) if v is not None else None
    except Exception:
        return None

def _win_rate(vals):
    if not vals: return None
    return round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1)


# ── Pages ──────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", now=datetime.now().strftime("%Y-%m-%d %H:%M"))


# ── API: Señal del Día ─────────────────────────────────────────────────────────

@app.route("/api/signal")
def api_signal():
    bd = _backdrop()
    with SessionLocal() as s:
        trades = s.execute(text("""
            SELECT signal_date, direction, decision, score_total, score_max,
                   l1_long, l1_short, l2_long, l2_short, veto,
                   entry_price, cot_pct, fundamental_dir, fundamental_bias,
                   ret_1d, ret_5d, ret_10d, ret_20d,
                   dir_ret_1d, dir_ret_5d, dir_ret_10d, dir_ret_20d
            FROM shadow_trades
            ORDER BY signal_date DESC LIMIT 30
        """)).fetchall()

    win_stats = {}
    for n in [1, 5, 10, 20]:
        vals = [float(getattr(t, f"dir_ret_{n}d"))
                for t in trades if getattr(t, f"dir_ret_{n}d") is not None]
        win_stats[f"wr_{n}d"] = _win_rate(vals)
        win_stats[f"n_{n}d"]  = len(vals)

    rows = []
    for t in trades:
        rows.append({
            "date":          str(t.signal_date),
            "direction":     t.direction,
            "decision":      t.decision,
            "score":         f"{t.score_total or 0}/{t.score_max or 12}",
            "entry_price":   _safe_float(t.entry_price),
            "cot_pct":       _safe_float(t.cot_pct),
            "fundamental":   t.fundamental_dir,
            "veto":          bool(t.veto) if t.veto is not None else False,
            "ret_1d":        _safe_float(t.ret_1d,  100),
            "ret_5d":        _safe_float(t.ret_5d,  100),
            "ret_10d":       _safe_float(t.ret_10d, 100),
            "ret_20d":       _safe_float(t.ret_20d, 100),
            "dir_ret_5d":    _safe_float(t.dir_ret_5d, 100),
        })

    return jsonify({"backdrop": bd, "trades": rows, "win_stats": win_stats})


# ── API: COT ──────────────────────────────────────────────────────────────────

@app.route("/api/cot")
def api_cot():
    with SessionLocal() as s:
        rows = s.execute(text("""
            SELECT report_date, mm_net, ncomm_net, total_oi,
                   mm_long, mm_short, swap_net
            FROM cot_data ORDER BY report_date
        """)).fetchall()

    dates    = [str(r.report_date) for r in rows]
    mm_net   = [int(r.mm_net)   if r.mm_net   else 0 for r in rows]
    ncomm    = [int(r.ncomm_net) if r.ncomm_net else 0 for r in rows]
    oi       = [int(r.total_oi) if r.total_oi else 0 for r in rows]
    swap_net = [int(r.swap_net) if r.swap_net else 0 for r in rows]

    # Rolling 3yr percentile for mm_net
    WINDOW = 156
    pct = []
    for i, v in enumerate(mm_net):
        w = mm_net[max(0, i - WINDOW):i]
        if len(w) < 13:
            pct.append(None)
        else:
            pct.append(round(sum(1 for x in w if x <= v) / len(w) * 100, 1))

    latest = {
        "date":     dates[-1] if dates else None,
        "mm_net":   mm_net[-1] if mm_net else None,
        "mm_pct":   pct[-1]   if pct   else None,
        "ncomm":    ncomm[-1] if ncomm else None,
        "oi":       oi[-1]    if oi    else None,
        "swap_net": swap_net[-1] if swap_net else None,
    }

    # Last 3 years only for chart (performance)
    n3y = min(len(dates), 156)
    return jsonify({
        "dates":    dates[-n3y:],
        "mm_net":   mm_net[-n3y:],
        "ncomm":    ncomm[-n3y:],
        "oi":       oi[-n3y:],
        "pct":      pct[-n3y:],
        "swap_net": swap_net[-n3y:],
        "latest":   latest,
        "all_dates":  dates,
        "all_mm_net": mm_net,
        "all_pct":    pct,
    })


# ── API: UNICA / Brazil ───────────────────────────────────────────────────────

@app.route("/api/unica")
def api_unica():
    T = 1_000_000.0  # t → Mt
    M = 1_000_000.0  # m3 → Mm3

    with SessionLocal() as s:
        cs_rows = s.execute(text("""
            SELECT safra, quinzena_date,
                   ROW_NUMBER() OVER (PARTITION BY safra ORDER BY quinzena_date) AS q_num,
                   cane_crushed_t, sugar_t,
                   ethanol_anidro_m3, ethanol_hidratado_m3, ethanol_total_m3,
                   sugar_mix_pct, eth_mix_pct, atr_kg_ton
            FROM unica_biweekly
            WHERE region = 'CS'
            ORDER BY safra, quinzena_date
        """)).fetchall()

        sp_rows = s.execute(text("""
            SELECT safra, quinzena_date,
                   ROW_NUMBER() OVER (PARTITION BY safra ORDER BY quinzena_date) AS q_num,
                   cane_crushed_t, sugar_t, ethanol_total_m3, sugar_mix_pct, eth_mix_pct, atr_kg_ton
            FROM unica_biweekly
            WHERE region = 'SP'
            ORDER BY safra, quinzena_date
        """)).fetchall()

    def _mix(sugar_mix, eth_mix):
        if sugar_mix is not None and float(sugar_mix) > 0:
            return round(float(sugar_mix), 1)
        if eth_mix is not None:
            return round(100.0 - float(eth_mix), 1)
        return None

    # ── Build CS per-safra quincena data ──────────────────────────────────────
    cs_by_safra = {}
    for r in cs_rows:
        k = r.safra
        if k not in cs_by_safra:
            cs_by_safra[k] = []
        cs_by_safra[k].append({
            "q":    int(r.q_num),
            "date": str(r.quinzena_date),
            "cane": round(float(r.cane_crushed_t) / T, 3) if r.cane_crushed_t else None,
            "sugar": round(float(r.sugar_t) / T, 4)       if r.sugar_t        else None,
            "eth_an":  round(float(r.ethanol_anidro_m3) / M, 4)   if r.ethanol_anidro_m3   else None,
            "eth_hid": round(float(r.ethanol_hidratado_m3) / M, 4) if r.ethanol_hidratado_m3 else None,
            "eth":  round(float(r.ethanol_total_m3) / M, 4)       if r.ethanol_total_m3   else None,
            "mix":  _mix(r.sugar_mix_pct, r.eth_mix_pct),
            "atr":  round(float(r.atr_kg_ton), 2) if r.atr_kg_ton else None,
        })

    # ── Build SP per-safra data ───────────────────────────────────────────────
    sp_by_safra = {}
    for r in sp_rows:
        k = r.safra
        if k not in sp_by_safra:
            sp_by_safra[k] = []
        sp_by_safra[k].append({
            "q":     int(r.q_num),
            "sugar": round(float(r.sugar_t) / T, 4) if r.sugar_t else None,
            "cane":  round(float(r.cane_crushed_t) / T, 3) if r.cane_crushed_t else None,
            "mix":   _mix(r.sugar_mix_pct, r.eth_mix_pct),
            "atr":   round(float(r.atr_kg_ton), 2) if r.atr_kg_ton else None,
        })

    safra_list = sorted(cs_by_safra.keys(), reverse=True)
    current_safra = safra_list[0]
    prev_safra    = safra_list[1] if len(safra_list) > 1 else None

    # ── Cumulative series (sugar + ethanol) per safra ─────────────────────────
    cum_sugar = {}
    cum_eth   = {}
    cum_cane  = {}
    for sk, qs in cs_by_safra.items():
        s_acc = e_acc = c_acc = 0.0
        cs_s, cs_e, cs_c = [], [], []
        for q in qs:
            if q["sugar"] is not None: s_acc += q["sugar"]
            if q["eth"]   is not None: e_acc += q["eth"]
            if q["cane"]  is not None: c_acc += q["cane"]
            cs_s.append(round(s_acc, 3))
            cs_e.append(round(e_acc, 3))
            cs_c.append(round(c_acc, 1))
        cum_sugar[sk] = cs_s
        cum_eth[sk]   = cs_e
        cum_cane[sk]  = cs_c

    # ── Season totals per safra (for the summary table) ───────────────────────
    season_totals = []
    for sk in safra_list:
        qs = cs_by_safra[sk]
        total_sugar = sum(q["sugar"] for q in qs if q["sugar"])
        total_cane  = sum(q["cane"]  for q in qs if q["cane"])
        total_eth   = sum(q["eth"]   for q in qs if q["eth"])
        # Medias ponderadas por caña molida (simple average produce valores erróneos:
        # las últimas quincenas tienen poco volumen pero mix muy bajo y tiran la media)
        cane_mix = [(q["cane"], q["mix"]) for q in qs if q["cane"] and q["mix"]]
        cane_atr = [(q["cane"], q["atr"]) for q in qs if q["cane"] and q["atr"]]
        w_cane_mix = sum(c for c, _ in cane_mix)
        w_cane_atr = sum(c for c, _ in cane_atr)
        n_q         = len(qs)
        is_complete = n_q >= 23
        season_totals.append({
            "safra":       sk,
            "total_sugar": round(total_sugar, 2),
            "total_cane":  round(total_cane,  1),
            "total_eth":   round(total_eth,   2),
            "avg_mix":     round(sum(c * m for c, m in cane_mix) / w_cane_mix, 1) if w_cane_mix else None,
            "avg_atr":     round(sum(c * a for c, a in cane_atr) / w_cane_atr, 1) if w_cane_atr else None,
            "n_q":         n_q,
            "complete":    is_complete,
        })

    # ── Projection: historical Q-pace ratio ───────────────────────────────────
    cur_qs    = cs_by_safra[current_safra]
    cur_q_num = len(cur_qs)
    cum_now   = cum_sugar[current_safra][-1] if cum_sugar[current_safra] else 0.0

    ratios = []
    for st in season_totals:
        if st["complete"] and st["total_sugar"] > 0:
            hist_qs = cs_by_safra[st["safra"]]
            if len(hist_qs) >= cur_q_num:
                cum_at_q = sum(q["sugar"] for q in hist_qs[:cur_q_num] if q["sugar"])
                ratios.append(cum_at_q / st["total_sugar"])
    proj_sugar = None
    if ratios and cum_now > 0:
        median_ratio = sorted(ratios)[len(ratios) // 2]
        if median_ratio > 0:
            proj_sugar = round(cum_now / median_ratio, 2)

    # ── Current summary KPIs ──────────────────────────────────────────────────
    last_q = cur_qs[-1] if cur_qs else {}

    yoy_sugar = yoy_cane = yoy_eth = None
    if prev_safra and prev_safra in cs_by_safra:
        prev_qs = cs_by_safra[prev_safra]
        if len(prev_qs) >= cur_q_num:
            prev_cum_sugar = sum(q["sugar"] for q in prev_qs[:cur_q_num] if q["sugar"])
            prev_cum_cane  = sum(q["cane"]  for q in prev_qs[:cur_q_num] if q["cane"])
            prev_cum_eth   = sum(q["eth"]   for q in prev_qs[:cur_q_num] if q["eth"])
            if prev_cum_sugar > 0:
                yoy_sugar = round((cum_now - prev_cum_sugar) / prev_cum_sugar * 100, 1)
            if prev_cum_cane  > 0:
                yoy_cane  = round((cum_cane[current_safra][-1]  - prev_cum_cane)  / prev_cum_cane  * 100, 1)
            if prev_cum_eth   > 0:
                yoy_eth   = round((cum_eth[current_safra][-1]   - prev_cum_eth)   / prev_cum_eth   * 100, 1)

    # ── YoY per quincena (vs same Q prev safra) ───────────────────────────────
    prev_q_map = {}
    if prev_safra and prev_safra in cs_by_safra:
        for q in cs_by_safra[prev_safra]:
            prev_q_map[q["q"]] = q

    cur_detail = []
    for q in cur_qs:
        pq    = prev_q_map.get(q["q"], {})
        ps    = pq.get("sugar")
        yoy_q = round((q["sugar"] - ps) / ps * 100, 1) if (q["sugar"] and ps and ps > 0) else None
        cum_q = cum_sugar[current_safra][q["q"] - 1]
        cur_detail.append({**q, "yoy_q": yoy_q, "cum_sugar": cum_q})

    summary = {
        "safra":        current_safra,
        "q_num":        cur_q_num,
        "q_date":       last_q.get("date"),
        "cum_sugar":    round(cum_now, 3),
        "cum_cane":     cum_cane[current_safra][-1] if cum_cane[current_safra] else None,
        "cum_eth":      cum_eth[current_safra][-1]  if cum_eth[current_safra]  else None,
        "atr":          last_q.get("atr"),
        "mix":          last_q.get("mix"),
        "yoy_sugar":    yoy_sugar,
        "yoy_cane":     yoy_cane,
        "yoy_eth":      yoy_eth,
        "proj_sugar":      proj_sugar,
        "n_proj_hist":     len(ratios),
        "proj_reliable":   cur_q_num >= 6,
    }

    # ── YoY on season totals ─────────────────────────────────────────────────
    for i, st in enumerate(season_totals):
        prev_st = season_totals[i + 1] if i + 1 < len(season_totals) else None
        st["yoy_sugar"] = (round((st["total_sugar"] - prev_st["total_sugar"]) /
                                  prev_st["total_sugar"] * 100, 1)
                           if prev_st and prev_st["total_sugar"] > 0 else None)

    # ── Historical season production range (display instead of bogus projection) ──
    hist_complete_vals = sorted([
        st["total_sugar"] for st in season_totals
        if st["complete"] and st["total_sugar"]
    ])
    hist_season_min = round(hist_complete_vals[0],  1) if hist_complete_vals else None
    hist_season_max = round(hist_complete_vals[-1], 1) if hist_complete_vals else None
    hist_season_med = round(hist_complete_vals[len(hist_complete_vals) // 2], 1) if hist_complete_vals else None

    # ── Comparison rows: Q1-Q24 side-by-side both seasons ────────────────────
    prev_q_by_num = {}
    if prev_safra:
        for q in cs_by_safra.get(prev_safra, []):
            prev_q_by_num[q["q"]] = q

    comparison_rows = []
    for qi in range(1, 25):
        cur_q  = next((q for q in cur_qs if q["q"] == qi), None)
        prev_q = prev_q_by_num.get(qi)
        if cur_q is None and prev_q is None:
            continue
        d_sugar = d_mix = d_atr = None
        if cur_q and prev_q:
            if cur_q.get("sugar") and prev_q.get("sugar") and prev_q["sugar"] > 0:
                d_sugar = round((cur_q["sugar"] - prev_q["sugar"]) / prev_q["sugar"] * 100, 1)
            if cur_q.get("mix") is not None and prev_q.get("mix") is not None:
                d_mix = round(cur_q["mix"] - prev_q["mix"], 1)
            if cur_q.get("atr") is not None and prev_q.get("atr") is not None:
                d_atr = round(cur_q["atr"] - prev_q["atr"], 1)
        comparison_rows.append({
            "q":          qi,
            "has_cur":    cur_q is not None,
            "prev_date":  prev_q.get("date")  if prev_q else None,
            "prev_cane":  prev_q.get("cane")  if prev_q else None,
            "prev_sugar": prev_q.get("sugar") if prev_q else None,
            "prev_mix":   prev_q.get("mix")   if prev_q else None,
            "prev_atr":   prev_q.get("atr")   if prev_q else None,
            "cur_date":   cur_q.get("date")   if cur_q else None,
            "cur_cane":   cur_q.get("cane")   if cur_q else None,
            "cur_sugar":  cur_q.get("sugar")  if cur_q else None,
            "cur_mix":    cur_q.get("mix")    if cur_q else None,
            "cur_atr":    cur_q.get("atr")    if cur_q else None,
            "d_sugar":    d_sugar,
            "d_mix":      d_mix,
            "d_atr":      d_atr,
        })

    # ── Historical range per Q (p25/avg/p75 across 10 complete safras) ────────
    COMPLETE_KEYS = [sk for sk in safra_list[1:] if len(cs_by_safra[sk]) >= 23][:10]
    MAX_Q = 24
    hist_range_min, hist_range_p25, hist_range_avg, hist_range_p75, hist_range_max = \
        [], [], [], [], []
    hist_mix_p25, hist_mix_avg, hist_mix_p75 = [], [], []
    hist_atr_p25, hist_atr_avg, hist_atr_p75 = [], [], []
    hist_sugar_avg, hist_cane_avg = [], []
    for qi in range(MAX_Q):
        vals = sorted([cum_sugar[sk][qi] for sk in COMPLETE_KEYS
                       if qi < len(cum_sugar.get(sk, []))])
        if vals:
            n = len(vals)
            hist_range_min.append(round(vals[0], 2))
            hist_range_p25.append(round(vals[max(0, n // 4 - 1)], 2))
            hist_range_avg.append(round(sum(vals) / n, 2))
            hist_range_p75.append(round(vals[min(n - 1, 3 * n // 4)], 2))
            hist_range_max.append(round(vals[-1], 2))
        else:
            for lst in [hist_range_min, hist_range_p25, hist_range_avg,
                        hist_range_p75, hist_range_max]:
                lst.append(None)

        # Mix% historical range per quinzena
        mix_v = sorted([cs_by_safra[sk][qi]["mix"] for sk in COMPLETE_KEYS
                        if qi < len(cs_by_safra.get(sk, []))
                        and cs_by_safra[sk][qi]["mix"] is not None])
        if mix_v:
            n = len(mix_v)
            hist_mix_p25.append(round(mix_v[max(0, n // 4 - 1)], 1))
            hist_mix_avg.append(round(sum(mix_v) / n, 1))
            hist_mix_p75.append(round(mix_v[min(n - 1, 3 * n // 4)], 1))
        else:
            hist_mix_p25.append(None); hist_mix_avg.append(None); hist_mix_p75.append(None)

        # ATR historical range per quinzena
        atr_v = sorted([cs_by_safra[sk][qi]["atr"] for sk in COMPLETE_KEYS
                        if qi < len(cs_by_safra.get(sk, []))
                        and cs_by_safra[sk][qi]["atr"] is not None])
        if atr_v:
            n = len(atr_v)
            hist_atr_p25.append(round(atr_v[max(0, n // 4 - 1)], 1))
            hist_atr_avg.append(round(sum(atr_v) / n, 1))
            hist_atr_p75.append(round(atr_v[min(n - 1, 3 * n // 4)], 1))
        else:
            hist_atr_p25.append(None); hist_atr_avg.append(None); hist_atr_p75.append(None)

        # Sugar + Cane per-quinzena historical avg (for next-quinzena forecast)
        sugar_v = [cs_by_safra[sk][qi]["sugar"] for sk in COMPLETE_KEYS
                   if qi < len(cs_by_safra.get(sk, [])) and cs_by_safra[sk][qi]["sugar"] is not None]
        hist_sugar_avg.append(round(sum(sugar_v) / len(sugar_v), 3) if sugar_v else None)
        cane_v = [cs_by_safra[sk][qi]["cane"] for sk in COMPLETE_KEYS
                  if qi < len(cs_by_safra.get(sk, [])) and cs_by_safra[sk][qi]["cane"] is not None]
        hist_cane_avg.append(round(sum(cane_v) / len(cane_v), 1) if cane_v else None)

    # ── UNICA publication tracker ─────────────────────────────────────────────
    _state_path = Path(__file__).parent.parent / "data" / "unica_state.json"
    unica_tracker = {"status": "unknown", "is_overdue": False}
    try:
        _st = json.loads(_state_path.read_text(encoding="utf-8")) if _state_path.exists() else {}
        _pos_str = _st.get("last_position_date")
        if _pos_str:
            _pos      = date.fromisoformat(_pos_str)
            _next_pos = _pos + timedelta(days=15)
            _next_pub = _next_pos + timedelta(days=26)
            _days_to  = (_next_pub - date.today()).days
            unica_tracker = {
                "last_position_date": str(_pos),
                "next_data_period":   str(_next_pos),
                "next_pub_est":       str(_next_pub),
                "days_to_pub":        _days_to,
                "overdue_days":       max(0, -_days_to),
                "is_overdue":         _days_to < 0,
                "status": "OVERDUE" if _days_to < 0 else ("WATCHMODE" if _days_to <= 5 else "PENDING"),
            }
    except Exception as _e:
        logging.warning("unica_tracker: %s", _e)

    # ── Next quinzena forecast (10-yr hist avg for the upcoming period) ────────
    next_qi = len(cs_by_safra[current_safra])   # 0-indexed: number of periods already published
    _Q_DATE_LABELS = [
        'Apr-16','May-01','May-16','Jun-01','Jun-16','Jul-01','Jul-16','Aug-01',
        'Aug-16','Sep-01','Sep-16','Oct-01','Oct-16','Nov-01','Nov-16','Dec-01',
        'Dec-16','Jan-01','Jan-16','Feb-01','Feb-16','Mar-01','Mar-16','Apr-01'
    ]
    next_q_forecast = None
    if 0 <= next_qi < MAX_Q:
        next_q_forecast = {
            "q_num":     next_qi + 1,
            "label":     _Q_DATE_LABELS[next_qi],
            "sugar_avg": hist_sugar_avg[next_qi] if next_qi < len(hist_sugar_avg) else None,
            "cane_avg":  hist_cane_avg[next_qi]  if next_qi < len(hist_cane_avg)  else None,
            "mix_avg":   hist_mix_avg[next_qi]   if next_qi < len(hist_mix_avg)   else None,
            "mix_p25":   hist_mix_p25[next_qi]   if next_qi < len(hist_mix_p25)   else None,
            "mix_p75":   hist_mix_p75[next_qi]   if next_qi < len(hist_mix_p75)   else None,
            "atr_avg":   hist_atr_avg[next_qi]   if next_qi < len(hist_atr_avg)   else None,
            "atr_p25":   hist_atr_p25[next_qi]   if next_qi < len(hist_atr_p25)   else None,
            "atr_p75":   hist_atr_p75[next_qi]   if next_qi < len(hist_atr_p75)   else None,
        }

    # ── Per-quincena series: only current + prev (clean comparison) ───────────
    def _pad24(series):
        """Pad series to 24 with None; truncate at 24."""
        out = list(series[:24])
        out += [None] * (24 - len(out))
        return out

    cur_mix  = _pad24([q["mix"] for q in cs_by_safra[current_safra]])
    prev_mix = _pad24([q["mix"] for q in cs_by_safra[prev_safra]]) if prev_safra else []
    cur_atr  = _pad24([q["atr"] for q in cs_by_safra[current_safra]])
    prev_atr = _pad24([q["atr"] for q in cs_by_safra[prev_safra]]) if prev_safra else []
    cur_eth_an  = _pad24([q["eth_an"]  for q in cs_by_safra[current_safra]])
    cur_eth_hid = _pad24([q["eth_hid"] for q in cs_by_safra[current_safra]])
    prev_eth_an  = _pad24([q["eth_an"]  for q in cs_by_safra[prev_safra]]) if prev_safra else []
    prev_eth_hid = _pad24([q["eth_hid"] for q in cs_by_safra[prev_safra]]) if prev_safra else []

    return jsonify({
        "summary":        summary,
        "cur_detail":     cur_detail,
        "season_totals":  season_totals[:14],
        "safra_list":     safra_list,
        "prev_safra":     prev_safra,
        # Accumulated sugar: current + prev + historical range
        "cum_sugar_cur":  _pad24(cum_sugar[current_safra]),
        "cum_sugar_prev": _pad24(cum_sugar[prev_safra]) if prev_safra else [],
        "hist_p25":       hist_range_p25,
        "hist_avg":       hist_range_avg,
        "hist_p75":       hist_range_p75,
        "hist_mix_p25":   hist_mix_p25,
        "hist_mix_avg":   hist_mix_avg,
        "hist_mix_p75":   hist_mix_p75,
        "hist_atr_p25":   hist_atr_p25,
        "hist_atr_avg":   hist_atr_avg,
        "hist_atr_p75":   hist_atr_p75,
        # Per-quincena comparison: current vs prev only
        "cur_mix":   cur_mix,  "prev_mix":  prev_mix,
        "cur_atr":   cur_atr,  "prev_atr":  prev_atr,
        "cur_eth_an":  cur_eth_an,  "cur_eth_hid":  cur_eth_hid,
        "prev_eth_an": prev_eth_an, "prev_eth_hid": prev_eth_hid,
        # Season-aligned comparison (Q1-Q24, both seasons)
        "comparison_rows":  comparison_rows,
        # Historical production range (replaces bogus pace-based projection)
        "hist_season_min":  hist_season_min,
        "hist_season_max":  hist_season_max,
        "hist_season_med":  hist_season_med,
        # UNICA publication tracker + next quinzena forecast
        "unica_tracker":    unica_tracker,
        "next_q_forecast":  next_q_forecast,
    })


# ── API: USDA Fundamentales ───────────────────────────────────────────────────

@app.route("/api/usda/global")
def api_usda_global():
    with SessionLocal() as s:
        rows = s.execute(text("""
            SELECT marketing_year, pub_month, attribute_name, value_1000mt
            FROM usda_psd
            WHERE country_code = 'WB'
            ORDER BY marketing_year ASC, pub_month DESC
        """)).fetchall()

    by_my = {}
    for r in rows:
        my   = r.marketing_year
        attr = r.attribute_name
        if my not in by_my:
            by_my[my] = {}
        if attr not in by_my[my]:
            by_my[my][attr] = _safe_float(r.value_1000mt)

    series = []
    prev_stu = None
    for my in sorted(by_my.keys()):
        d    = by_my[my]
        prod = d.get("production")
        cons = d.get("dom_consumption")
        ends = d.get("ending_stocks")
        if not (prod and cons and ends and cons > 0):
            continue
        stu     = round(ends / cons * 100, 1)
        surplus = round((prod - cons) / 1000, 1)
        trend   = round(stu - prev_stu, 1) if prev_stu else None
        prev_stu = stu
        series.append({
            "my":         my,
            "label":      f"{my}/{str(my+1)[-2:]}",
            "prod":       round(prod / 1000, 1),
            "cons":       round(cons / 1000, 1),
            "ends":       round(ends / 1000, 1),
            "stu":        stu,
            "surplus":    surplus,
            "stu_trend":  trend,
        })

    return jsonify({"series": series, "latest": series[-1] if series else {}})


@app.route("/api/usda/countries")
def api_usda_countries():
    CODES = {"BR": "Brasil", "IN": "India", "TH": "Tailandia",
             "EU": "UE", "CN": "China", "AU": "Australia",
             "US": "EE.UU.", "PK": "Pakistan", "MX": "Mexico"}

    with SessionLocal() as s:
        rows = s.execute(text("""
            SELECT country_code, marketing_year, pub_month,
                   attribute_name, value_1000mt
            FROM usda_psd
            WHERE attribute_name = 'production'
              AND country_code IN ('BR','IN','TH','EU','CN','AU','US','PK','MX')
            ORDER BY country_code, marketing_year, pub_month DESC
        """)).fetchall()

    # latest per country+year
    data = {}
    for r in rows:
        k = (r.country_code, r.marketing_year)
        if k not in data:
            data[k] = _safe_float(r.value_1000mt)

    max_my = max((k[1] for k in data), default=2025)
    result = []
    for cc, name in CODES.items():
        cur  = data.get((cc, max_my),     0) or 0
        prev = data.get((cc, max_my - 1), 0) or 0
        yoy  = round((cur - prev) / prev * 100, 1) if prev > 0 else None
        result.append({
            "code": cc, "name": name,
            "prod": round(cur / 1000, 1),
            "yoy":  yoy, "my": max_my,
        })

    result.sort(key=lambda x: -x["prod"])
    return jsonify({"countries": result, "my": max_my})


# ── API: Seasonality ──────────────────────────────────────────────────────────

@app.route("/api/seasonal")
def api_seasonal():
    """Estacionalidad mensual de precios SB_CONT y Q1 trigger."""
    with SessionLocal() as s:
        px = s.execute(text(
            "SELECT date, close FROM price_history "
            "WHERE instrument='SB_CONT' ORDER BY date"
        )).fetchall()
        cot = s.execute(text(
            "SELECT report_date, mm_net FROM cot_data ORDER BY report_date"
        )).fetchall()

    # End-of-month prices
    eom = {}
    for r in px:
        if r.close:
            k = (r.date.year, r.date.month)
            eom[k] = float(r.close)

    # Monthly returns by calendar month
    by_month = {m: [] for m in range(1, 13)}
    for (yr, mo), px_v in eom.items():
        if not (2007 <= yr <= 2025): continue
        prev = (yr, mo-1) if mo > 1 else (yr-1, 12)
        if prev in eom and eom[prev] > 0:
            by_month[mo].append(round((px_v - eom[prev]) / eom[prev] * 100, 2))

    month_stats = []
    names = ["", "Ene", "Feb", "Mar", "Abr", "May", "Jun",
             "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
    for mo in range(1, 13):
        vals = by_month[mo]
        if not vals: continue
        avg = sum(vals) / len(vals)
        wr  = sum(1 for v in vals if v > 0) / len(vals) * 100
        month_stats.append({
            "month": mo, "name": names[mo],
            "avg": round(avg, 2), "wr": round(wr, 0),
            "n":   len(vals),
            "min": round(min(vals), 1),
            "max": round(max(vals), 1),
        })

    # Q1 trigger per year
    mm_by_ym = {}
    for r in cot:
        if r.mm_net is not None:
            k = (r.report_date.year, r.report_date.month)
            if k not in mm_by_ym: mm_by_ym[k] = []
            mm_by_ym[k].append(float(r.mm_net))
    mm_avg = {k: sum(v)/len(v) for k, v in mm_by_ym.items()}

    q1_records = []
    for yr in range(2008, 2026):
        q1 = [mm_avg[(yr, m)] for m in [1,2,3] if (yr,m) in mm_avg]
        q4 = [mm_avg[(yr-1, m)] for m in [10,11,12] if (yr-1,m) in mm_avg]
        if len(q1) < 2 or len(q4) < 2: continue
        q1_chg = sum(q1)/len(q1) - sum(q4)/len(q4)
        px_mar = eom.get((yr, 3)); px_sep = eom.get((yr, 9))
        ret = round((px_sep - px_mar) / px_mar * 100, 1) if (px_mar and px_sep and px_mar > 0) else None
        q1_records.append({
            "yr": yr,
            "q1_chg": round(q1_chg, 0),
            "ret_aprsep": ret,
            "acum": q1_chg > 0,
        })

    return jsonify({"month_stats": month_stats, "q1_records": q1_records})


# ── Run ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n  SGT Trading Dashboard")
    print("  -> http://localhost:5000\n")
    app.run(debug=False, port=5000, use_reloader=False, host="0.0.0.0")
