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
from datetime import datetime

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
    with SessionLocal() as s:
        rows = s.execute(text("""
            SELECT safra, quinzena_num, quinzena_date, region,
                   cane_net_mt, sugar_net_mt, ethanol_net_m3,
                   sugar_mix_pct, eth_mix_pct, atr_kg_ton
            FROM unica_biweekly
            WHERE region = 'CS'
            ORDER BY safra, quinzena_num
        """)).fetchall()

    safras = {}
    for r in rows:
        k = r.safra
        if k not in safras:
            safras[k] = []
        safras[k].append({
            "q":      int(r.quinzena_num),
            "date":   str(r.quinzena_date) if r.quinzena_date else None,
            "cane":   _safe_float(r.cane_net_mt),
            "sugar":  _safe_float(r.sugar_net_mt),
            "eth_m3": _safe_float(r.ethanol_net_m3),
            "mix":    _safe_float(r.sugar_mix_pct),
            "atr":    _safe_float(r.atr_kg_ton),
        })

    safra_list = sorted(safras.keys(), reverse=True)
    bd = _backdrop()

    # Compute cumulative sugar per safra up to current quincena for chart
    cum_by_safra = {}
    for sk, qs in safras.items():
        cum = 0
        series = []
        for q in sorted(qs, key=lambda x: x["q"]):
            if q["sugar"] is not None:
                cum += q["sugar"]
            series.append(round(cum, 3))
        cum_by_safra[sk] = series

    return jsonify({
        "safras":       safras,
        "safra_list":   safra_list[:5],
        "cum_by_safra": cum_by_safra,
        "backdrop":     bd,
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
