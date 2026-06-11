"""
Análisis de estacionalidad — SB No. 11 continuo + COT Money Managers.

Cuatro bloques:

  1. ESTACIONALIDAD MENSUAL DE PRECIOS (2007-2025)
     Por cada mes: retorno promedio, win rate, mediana, y —lo más importante—
     qué pasa el RESTO DEL AÑO según si el mes fue alcista o bajista.

  2. ESTACIONALIDAD MENSUAL DEL COT (Money Managers)
     Posición típica de los fondos por mes. Baseline estacional.
     ¿En qué mes suelen estar más largos? ¿Cuándo más cortos?

  3. TRIGGER Q1: acumulación oct→mar vs retorno abr→sep
     El test central: cuando los fondos acumulan larga en Q1 (anticipando
     cosecha Brasil), ¿el precio sube más en Q2-Q3?
     Por quintil de acumulación + tabla año por año.

  4. SEÑAL DE DESVÍO COT vs BASELINE ESTACIONAL → 4/8 semanas
     Cuando la posición actual de los fondos está POR ENCIMA de lo que
     suele ser normal para esa época del año, ¿qué hace el precio?

Uso:
    py analysis/seasonality.py
"""
import sys, os, math, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.WARNING)

from datetime import date, timedelta
from collections import defaultdict
from database import SessionLocal
from sqlalchemy import text

PRICE_START = date(2007, 1, 1)
PRICE_END   = date(2025, 12, 31)   # años completos
COT_LAG     = 3                     # días report → publicación CFTC

MONTH_NAMES = ["", "Ene", "Feb", "Mar", "Abr", "May", "Jun",
               "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _avg(lst):
    return sum(lst) / len(lst) if lst else float("nan")

def _median(lst):
    if not lst: return float("nan")
    s = sorted(lst)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

def _win_rate(lst):
    if not lst: return float("nan")
    return sum(1 for x in lst if x > 0) / len(lst) * 100

def _sharpe(lst):
    if len(lst) < 3: return float("nan")
    mu = _avg(lst); sd = math.sqrt(sum((x - mu)**2 for x in lst) / (len(lst) - 1))
    return round(mu / sd, 2) if sd > 0 else float("nan")

def _pct(v):
    return ("%+.1f%%" % (v * 100)) if not math.isnan(v) else "  n/a"

def _fmt_n(v, decimals=0):
    if math.isnan(v): return "  n/a"
    fmt = "%+.%df" % decimals
    return fmt % v

def _quintile_label(rank, n):
    q = rank / n * 5
    if q < 1: return "Q1 (min)"
    if q < 2: return "Q2"
    if q < 3: return "Q3"
    if q < 4: return "Q4"
    return "Q5 (max)"


# ── Carga de datos ─────────────────────────────────────────────────────────────

def _load_data():
    with SessionLocal() as s:
        px_rows = s.execute(text(
            "SELECT date, close FROM price_history "
            "WHERE instrument='SB_CONT' AND date BETWEEN :s AND :e ORDER BY date"
        ), {"s": PRICE_START, "e": date(2026, 12, 31)}).fetchall()

        cot_rows = s.execute(text(
            "SELECT report_date, mm_net, ncomm_net, total_oi "
            "FROM cot_data WHERE report_date >= :s ORDER BY report_date"
        ), {"s": PRICE_START}).fetchall()

    # Mapa de precios diarios
    price_map = {}
    for r in px_rows:
        if r.close is not None:
            price_map[r.date] = float(r.close)
    price_dates = sorted(price_map)

    # Precio del último día hábil de cada (año, mes)
    eom = {}   # {(year, month): price}
    for d in price_dates:
        k = (d.year, d.month)
        eom[k] = price_map[d]  # sobrescribe — queda el último del mes

    # Retornos mensuales
    mon_rets = {}  # {(year, month): ret}
    for (yr, mo), px in eom.items():
        prev = (yr, mo - 1) if mo > 1 else (yr - 1, 12)
        if prev in eom and eom[prev] > 0:
            mon_rets[(yr, mo)] = (px - eom[prev]) / eom[prev]

    # COT semanal indexado por fecha de conocimiento (report_date + lag)
    cot_series = []
    for r in cot_rows:
        know_date = r.report_date + timedelta(days=COT_LAG)
        cot_series.append({
            "know_date": know_date,
            "report_date": r.report_date,
            "mm_net":    float(r.mm_net)    if r.mm_net    is not None else None,
            "ncomm_net": float(r.ncomm_net) if r.ncomm_net is not None else None,
            "total_oi":  float(r.total_oi)  if r.total_oi  is not None else None,
        })

    # Precio en o después de una fecha
    def price_on_or_after(d):
        for pd in price_dates:
            if pd >= d:
                return price_map[pd]
        return None

    return price_map, price_dates, eom, mon_rets, cot_series, price_on_or_after


# ── Bloque 1: Estacionalidad mensual de precios ────────────────────────────────

def _bloque1_precios(mon_rets, eom):
    print()
    print("=" * 80)
    print("  BLOQUE 1 — ESTACIONALIDAD MENSUAL DE PRECIOS  (SB_CONT, 2007-2025)")
    print("=" * 80)

    # Por mes: estadísticas básicas
    by_month = defaultdict(list)
    for (yr, mo), ret in mon_rets.items():
        if PRICE_START.year <= yr <= PRICE_END.year:
            by_month[mo].append((yr, ret))

    # Por mes: retorno "resto del año" desde el cierre de ese mes
    ry_all = {}    # {(year, month): resto_anio_ret}
    for (yr, mo), px_start in eom.items():
        if mo == 12 or yr > PRICE_END.year:
            continue
        k_dec = (yr, 12)
        if k_dec in eom and px_start > 0:
            ry_all[(yr, mo)] = (eom[k_dec] - px_start) / px_start

    print()
    print("  %-4s  %4s  %5s  %5s  %5s  %5s  │ Resto del año: si MES↑   si MES↓" % (
        "MES", "n", "avg%", "wr%", "min%", "max%"))
    print("  " + "─" * 74)

    for mo in range(1, 13):
        obs = by_month[mo]
        if not obs:
            continue
        rets = [r for _, r in obs]
        avg  = _avg(rets)
        wr   = _win_rate(rets)
        med  = _median(rets)
        mn   = min(rets); mx = max(rets)

        # Resto del año condicionado
        ry_up = [ry_all[(yr, mo)] for yr, r in obs
                 if r > 0 and (yr, mo) in ry_all]
        ry_dn = [ry_all[(yr, mo)] for yr, r in obs
                 if r <= 0 and (yr, mo) in ry_all]

        ry_up_s = ("%s (wr=%d%%)" % (_pct(_avg(ry_up)), int(_win_rate(ry_up)))) if ry_up else "  —"
        ry_dn_s = ("%s (wr=%d%%)" % (_pct(_avg(ry_dn)), int(_win_rate(ry_dn)))) if ry_dn else "  —"

        print("  %-4s  %3d  %5s  %4.0f%%  %5s  %5s  │  ↑%s / ↓%s" % (
            MONTH_NAMES[mo], len(rets), _pct(avg), wr,
            _pct(mn), _pct(mx), ry_up_s, ry_dn_s))

    # Detalle por año para los meses más relevantes (Abr, May, Sep)
    print()
    print("  DETALLE AÑO × AÑO — Abr, May, Jun (inicio cosecha Brasil)")
    print("  %-5s  %5s  %5s  %5s  │  Retorno Apr→Sep (6 meses desde Abr)" % (
        "AÑO", "Abr", "May", "Jun"))
    print("  " + "─" * 52)
    for yr in range(2007, 2026):
        apr = mon_rets.get((yr, 4)); may = mon_rets.get((yr, 5)); jun = mon_rets.get((yr, 6))
        # retorno abr→sep
        px_mar = eom.get((yr, 3)); px_sep = eom.get((yr, 9))
        apr_sep = ((px_sep - px_mar) / px_mar) if (px_mar and px_sep and px_mar > 0) else None
        print("  %4d   %5s  %5s  %5s  │  %5s" % (
            yr, _pct(apr) if apr else "  n/a",
            _pct(may) if may else "  n/a",
            _pct(jun) if jun else "  n/a",
            _pct(apr_sep) if apr_sep is not None else "  n/a"))


# ── Bloque 2: Estacionalidad mensual del COT ──────────────────────────────────

def _bloque2_cot(cot_series, eom):
    print()
    print("=" * 80)
    print("  BLOQUE 2 — ESTACIONALIDAD MENSUAL DEL COT  (Money Managers, 2007-2025)")
    print("  mm_net = posición neta en contratos (longs − shorts de fondos)")
    print("=" * 80)

    # Por mes: promedio mm_net y ncomm_net
    by_month_mm = defaultdict(list)
    for c in cot_series:
        yr = c["know_date"].year
        mo = c["know_date"].month
        if 2007 <= yr <= 2025 and c["mm_net"] is not None:
            by_month_mm[mo].append(c["mm_net"])

    # Media global de mm_net (para contexto)
    all_mm = [v for lst in by_month_mm.values() for v in lst]
    global_avg = _avg(all_mm)

    print()
    print("  Media global mm_net: %+.0f contratos" % global_avg)
    print()
    print("  %-4s   %6s  %6s  %7s  %5s  posicion tipica" % (
        "MES", "avg_net", "median", "std_dev", "n"))
    print("  " + "─" * 60)

    for mo in range(1, 13):
        vals = by_month_mm[mo]
        if not vals:
            continue
        avg = _avg(vals)
        med = _median(vals)
        std = math.sqrt(sum((v - avg)**2 for v in vals) / max(len(vals) - 1, 1))
        tag = "LARGO vs norm" if avg > global_avg + 5000 else (
              "CORTO vs norm" if avg < global_avg - 5000 else "NEUTRO")
        arrow = "↑" if avg > 0 else "↓"
        print("  %-4s   %+6.0f  %+6.0f  %7.0f  %5d  %s %s" % (
            MONTH_NAMES[mo], avg, med, std, len(vals), arrow, tag))

    # Mostrar curva anualizada de posicionamiento (ASCII)
    print()
    print("  Curva de posicion promedio por mes (cada '|' = 5000 contratos):")
    max_abs = max(abs(_avg(by_month_mm[m])) for m in range(1, 13) if by_month_mm[m])
    for mo in range(1, 13):
        vals = by_month_mm[mo]
        if not vals: continue
        avg = _avg(vals)
        n_bars = int(abs(avg) / 5000)
        bar = ("+" if avg >= 0 else "-") * n_bars
        print("  %-4s  %+7.0f  %s" % (MONTH_NAMES[mo], avg, bar))


# ── Bloque 3: Trigger Q1 ──────────────────────────────────────────────────────

def _bloque3_q1_trigger(cot_series, eom):
    print()
    print("=" * 80)
    print("  BLOQUE 3 — TRIGGER Q1: acumulacion oct→mar vs retorno abr→sep")
    print("  Hipotesis: cuando fondos acumulan largos en Q1, Q2-Q3 es alcista.")
    print("  acumulacion = avg(mm_net, Ene-Mar Y) - avg(mm_net, Oct-Dic Y-1)")
    print("=" * 80)

    # Promedio mm_net por (año, mes)
    mm_by_ym = defaultdict(list)
    for c in cot_series:
        yr = c["know_date"].year
        mo = c["know_date"].month
        if c["mm_net"] is not None:
            mm_by_ym[(yr, mo)].append(c["mm_net"])
    mm_avg_ym = {k: _avg(v) for k, v in mm_by_ym.items()}

    records = []
    for yr in range(2008, 2026):
        # Q1 actual: Ene+Feb+Mar de yr
        q1_vals = [mm_avg_ym[(yr, m)] for m in [1, 2, 3]
                   if (yr, m) in mm_avg_ym]
        # Q4 anterior: Oct+Nov+Dic de yr-1
        q4_vals = [mm_avg_ym[(yr-1, m)] for m in [10, 11, 12]
                   if (yr-1, m) in mm_avg_ym]
        if len(q1_vals) < 2 or len(q4_vals) < 2:
            continue

        q1_avg  = _avg(q1_vals)
        q4_avg  = _avg(q4_vals)
        q1_chg  = q1_avg - q4_avg   # acumulacion: positivo = fondos compraron en Q1

        # Retorno abr→sep (cosecha brasileña)
        px_mar = eom.get((yr, 3)); px_sep = eom.get((yr, 9))
        ret_aprsep = ((px_sep - px_mar) / px_mar) if (px_mar and px_sep and px_mar > 0) else None

        # Retorno mar→dic (resto del año)
        px_dic = eom.get((yr, 12))
        ret_fy  = ((px_dic - px_mar) / px_mar) if (px_mar and px_dic and px_mar > 0) else None

        records.append({
            "yr": yr,
            "q1_avg": q1_avg, "q4_avg": q4_avg, "q1_chg": q1_chg,
            "ret_aprsep": ret_aprsep, "ret_fy": ret_fy,
        })

    if not records:
        print("  Sin datos suficientes.")
        return

    # Tabla año × año
    print()
    print("  %-4s  %7s  %7s  %7s  %7s  %8s" % (
        "AÑO", "Q4prev", "Q1avg", "Q1_chg", "abr-sep", "mar-dic"))
    print("  " + "─" * 58)
    for r in records:
        tag = "ACUM" if r["q1_chg"] > 0 else "DIST"
        print("  %4d  %+7.0f  %+7.0f  %+7.0f  %7s  %7s  [%s]" % (
            r["yr"], r["q4_avg"], r["q1_avg"], r["q1_chg"],
            _pct(r["ret_aprsep"]) if r["ret_aprsep"] is not None else "  n/a",
            _pct(r["ret_fy"])     if r["ret_fy"]     is not None else "  n/a",
            tag))

    # Análisis por quintiles
    print()
    print("  QUINTILES de Q1_chg → retorno abr-sep")
    recs_sorted = sorted([r for r in records if r["ret_aprsep"] is not None],
                         key=lambda r: r["q1_chg"])
    n = len(recs_sorted)
    q_bounds = [n * i // 5 for i in range(6)]   # cortes equitativos

    q_labels = ["Q1 DIST.MAX  (fondos vendieron fuerte)",
                "Q2 DIST      (fondos vendieron)",
                "Q3 NEUTRO    (sin cambio claro)",
                "Q4 ACUM      (fondos compraron)",
                "Q5 ACUM.MAX  (fondos compraron fuerte)"]

    for qi in range(5):
        start = q_bounds[qi]; end = q_bounds[qi + 1]
        chunk = recs_sorted[start:end]
        if not chunk: continue
        chg_avg  = _avg([r["q1_chg"] for r in chunk])
        ret_avg  = _avg([r["ret_aprsep"] for r in chunk])
        ret_fy   = _avg([r["ret_fy"] for r in chunk if r["ret_fy"] is not None])
        wr_as    = _win_rate([r["ret_aprsep"] for r in chunk])
        wr_fy    = _win_rate([r["ret_fy"] for r in chunk if r["ret_fy"] is not None])
        yrs      = ", ".join(str(r["yr"]) for r in chunk)
        print("  %-40s  n=%2d  Q1_chg=%+7.0f" % (q_labels[qi], len(chunk), chg_avg))
        print("    abr-sep: wr=%d%%  avg=%s  │  mar-dic: wr=%d%%  avg=%s" % (
            int(wr_as), _pct(ret_avg), int(wr_fy), _pct(ret_fy)))
        print("    años: %s" % yrs)
        print()

    # Pearson correlation Q1_chg vs ret_aprsep
    xs = [r["q1_chg"] for r in records if r["ret_aprsep"] is not None]
    ys = [r["ret_aprsep"] for r in records if r["ret_aprsep"] is not None]
    if len(xs) > 3:
        mx = _avg(xs); my = _avg(ys)
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / len(xs)
        sx  = math.sqrt(sum((x - mx)**2 for x in xs) / len(xs))
        sy  = math.sqrt(sum((y - my)**2 for y in ys) / len(ys))
        corr = cov / (sx * sy) if sx * sy > 0 else 0
        print()
        print("  Correlacion Pearson Q1_chg vs ret_abr-sep: r = %.3f" % corr)
        if abs(corr) > 0.35:
            print("  → Correlacion MODERADA-FUERTE. La señal tiene valor estadistico.")
        elif abs(corr) > 0.20:
            print("  → Correlacion DEBIL pero presente. Señal auxiliar util.")
        else:
            print("  → Correlacion baja. La hipotesis no se confirma en los datos.")


# ── Bloque 4: COT deviation vs baseline estacional ────────────────────────────

def _bloque4_cot_desvio(cot_series, price_on_or_after):
    print()
    print("=" * 80)
    print("  BLOQUE 4 — DESVIO COT vs BASELINE ESTACIONAL → retorno 4/8 semanas")
    print("  Cuando los fondos tienen MAS/MENOS contratos de lo que es normal")
    print("  para esa epoca del año, ¿qué hace el precio?")
    print("=" * 80)

    # Para cada observacion COT, calcular baseline = avg mm_net mismo mes calendario
    # sobre la ventana rolling 3yr (156 semanas ~)
    WINDOW = 156   # semanas ≈ 3 años

    # Ordenar serie COT
    cot_sorted = [c for c in cot_series if c["mm_net"] is not None]
    cot_sorted.sort(key=lambda c: c["know_date"])

    records = []
    for i, c in enumerate(cot_sorted):
        cal_month = c["know_date"].month
        ref_date  = c["know_date"]
        cutoff    = ref_date - timedelta(days=WINDOW * 7)

        # Rolling mismo mes calendario
        same_month_vals = [
            x["mm_net"] for j, x in enumerate(cot_sorted[:i])
            if x["know_date"] >= cutoff
            and x["know_date"].month == cal_month
            and x["mm_net"] is not None
        ]
        if len(same_month_vals) < 6:   # mínimo 6 obs (2 años de ese mes)
            continue

        baseline = _avg(same_month_vals)
        std_base = math.sqrt(sum((v - baseline)**2 for v in same_month_vals) / len(same_month_vals))
        if std_base < 1000:
            continue
        deviation = c["mm_net"] - baseline
        dev_z     = deviation / std_base   # cuántas sigmas

        # Precio futuro
        px_now  = price_on_or_after(ref_date)
        px_4w   = price_on_or_after(ref_date + timedelta(weeks=4))
        px_8w   = price_on_or_after(ref_date + timedelta(weeks=8))
        px_13w  = price_on_or_after(ref_date + timedelta(weeks=13))

        if not px_now:
            continue

        ret4w  = (px_4w  - px_now) / px_now if px_4w  else None
        ret8w  = (px_8w  - px_now) / px_now if px_8w  else None
        ret13w = (px_13w - px_now) / px_now if px_13w else None

        records.append({
            "date":    ref_date,
            "dev_z":   dev_z,
            "mm_net":  c["mm_net"],
            "ret4w":   ret4w,
            "ret8w":   ret8w,
            "ret13w":  ret13w,
        })

    if not records:
        print("  Sin datos suficientes.")
        return

    # Quintiles por dev_z
    recs_sorted = sorted(records, key=lambda r: r["dev_z"])
    n = len(recs_sorted)
    q_size = max(n // 5, 1)

    print()
    print("  n total observaciones: %d" % n)
    print()
    print("  %-22s  %4s  %6s  %7s  %7s  %7s  %7s  %7s  %7s" % (
        "DESVIO vs ESTACIONAL", "n",
        "dev_z", "wr_4w", "avg_4w", "wr_8w", "avg_8w", "wr_13w", "avg_13w"))
    print("  " + "─" * 88)

    labels = ["Q1 MUY CORTO vs norm", "Q2 ALGO CORTO",
              "Q3 NEUTRO", "Q4 ALGO LARGO", "Q5 MUY LARGO vs norm"]

    for qi in range(5):
        start = qi * q_size
        end   = (qi + 1) * q_size if qi < 4 else n
        chunk = recs_sorted[start:end]
        if not chunk: continue

        dz_avg = _avg([r["dev_z"] for r in chunk])
        r4  = [r["ret4w"]  for r in chunk if r["ret4w"]  is not None]
        r8  = [r["ret8w"]  for r in chunk if r["ret8w"]  is not None]
        r13 = [r["ret13w"] for r in chunk if r["ret13w"] is not None]

        print("  %-22s  %4d  %+5.2f  %6.0f%%  %7s  %6.0f%%  %7s  %6.0f%%  %7s" % (
            labels[qi], len(chunk), dz_avg,
            _win_rate(r4),  _pct(_avg(r4)),
            _win_rate(r8),  _pct(_avg(r8)),
            _win_rate(r13), _pct(_avg(r13))))

    # Insight: ¿la señal es lineal? ¿Q5 largo → precio sube (momentum) o baja (contrarian)?
    q1_chunk = recs_sorted[:q_size]
    q5_chunk = recs_sorted[n - q_size:]
    q1_r8 = [r["ret8w"] for r in q1_chunk if r["ret8w"] is not None]
    q5_r8 = [r["ret8w"] for r in q5_chunk if r["ret8w"] is not None]

    print()
    if q1_r8 and q5_r8:
        diff = _avg(q5_r8) - _avg(q1_r8)
        if diff > 0.01:
            insight = "MOMENTUM: cuando fondos estan muy largos vs norma, precio tiende a SUBIR → seguir la posicion"
        elif diff < -0.01:
            insight = "CONTRARIAN: cuando fondos estan muy largos vs norma, precio tiende a BAJAR → señal de techo"
        else:
            insight = "SIN SEÑAL CLARA en desvio vs baseline estacional"
        print("  Interpretacion: %s" % insight)
        print("  Q5 avg 8w: %s  vs  Q1 avg 8w: %s  (diferencia: %s)" % (
            _pct(_avg(q5_r8)), _pct(_avg(q1_r8)), _pct(diff)))


# ── Main ───────────────────────────────────────────────────────────────────────

def run():
    print()
    print("=" * 80)
    print("  ESTACIONALIDAD SGT TRADING — SB_CONT + COT Money Managers")
    print("  Período: 2007-2025  |  Precios: SB No.11 continuo  |  COT: CFTC MMAP")
    print("=" * 80)

    price_map, price_dates, eom, mon_rets, cot_series, price_on_or_after = _load_data()

    print("\n  Precios: %d días  |  COT: %d semanas" % (len(price_map), len(cot_series)))

    _bloque1_precios(mon_rets, eom)
    _bloque2_cot(cot_series, eom)
    _bloque3_q1_trigger(cot_series, eom)
    _bloque4_cot_desvio(cot_series, price_on_or_after)

    print()
    print("=" * 80)
    print("  FIN DEL ANALISIS")
    print("=" * 80)
    print()


if __name__ == "__main__":
    run()
