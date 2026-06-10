"""
Comparacion MM (Disaggregated) vs Spec Net (Legacy NC+NonRep) como señal COT.

Corre el mismo modelo viernes->lunes sobre ambas series y compara WR/p-value.
Decide cual usar como señal primaria A1.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from scipy import stats as scipy_stats
from database import SessionLocal
from sqlalchemy import text

WINDOW_3YR  = 156
MIN_HIST    = 52
SURPRISE_Z  = 1.5
OOS_CUT     = pd.Timestamp("2021-01-01")

EXTREME_LOW  = 10
DEPRESSED    = 25
ELEVATED     = 75
EXTREME_HIGH = 90


def load_cot(session):
    rows = session.execute(text(
        "SELECT report_date, speculator_net, mm_net "
        "FROM cot_data ORDER BY report_date ASC"
    )).fetchall()
    df = pd.DataFrame(rows, columns=["report_date", "spec_net", "mm_net"])
    df["report_date"] = pd.to_datetime(df["report_date"])
    df["spec_net"] = pd.to_numeric(df["spec_net"], errors="coerce")
    df["mm_net"]   = pd.to_numeric(df["mm_net"],   errors="coerce")
    return df


def load_prices(session):
    rows = session.execute(text(
        "SELECT date, open, close FROM price_history "
        "WHERE instrument='SB_CONT' ORDER BY date ASC"
    )).fetchall()
    df = pd.DataFrame(rows, columns=["date", "open", "close"])
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").dropna()


def add_signals(df, col):
    """Añade percentil 3yr, cambio semanal z, regime y surprise para la columna col."""
    d = df.copy()
    d["pct3yr"] = d[col].rolling(WINDOW_3YR, min_periods=MIN_HIST).apply(
        lambda x: (x <= x[-1]).mean() * 100, raw=True)
    d["chg1w"] = d[col].diff(1)
    chg_std    = d["chg1w"].rolling(52, min_periods=20).std()
    d["chg_z"] = d["chg1w"] / chg_std

    def regime(p):
        if pd.isna(p): return "SIN_DATOS"
        if p <= EXTREME_LOW:  return "EXTREMO_CORTO"
        if p <= DEPRESSED:    return "DEPRIMIDO"
        if p >= EXTREME_HIGH: return "EXTREMO_LARGO"
        if p >= ELEVATED:     return "ELEVADO"
        return "NEUTRAL"

    def surprise(z):
        if pd.isna(z): return "NORMAL"
        if z < -SURPRISE_Z: return "GRAN_REDUCCION"
        if z > +SURPRISE_Z: return "GRAN_ADICION"
        return "NORMAL"

    d["regime"]   = d["pct3yr"].apply(regime)
    d["surprise"] = d["chg_z"].apply(surprise)
    return d


def align_to_prices(cot, prices):
    pmap   = {d: row for d, row in prices.iterrows()}
    tdays  = sorted(pmap.keys())

    def next_td(d):
        for t in tdays:
            if t >= d: return t
        return None

    def nth_after(d, n):
        after = [t for t in tdays if t > d]
        return after[n - 1] if len(after) >= n else None

    records = []
    for _, row in cot.iterrows():
        pub_fri = row["report_date"] + pd.Timedelta(days=3)
        fri     = next_td(pub_fri)
        if fri is None or fri not in pmap: continue
        mon = nth_after(fri, 1)
        if mon is None or mon not in pmap: continue
        fwd = nth_after(mon, 5)

        fc = float(pmap[fri]["close"])
        mo = float(pmap[mon]["open"])
        mc = float(pmap[mon]["close"])
        fw = float(pmap[fwd]["close"]) if fwd and fwd in pmap else None

        records.append({
            "report_date": row["report_date"],
            "pct3yr":      row["pct3yr"],
            "regime":      row["regime"],
            "surprise":    row["surprise"],
            "chg_z":       row["chg_z"],
            "gap_pct":     (mo - fc) / fc * 100 if fc else None,
            "mon_pct":     (mc - mo) / mo * 100 if mo else None,
            "fwd5d_pct":   (fw - mo) / mo * 100 if fw and mo else None,
        })
    return pd.DataFrame(records).dropna(subset=["gap_pct", "mon_pct"])


def regime_stats(df, label, oos_cut):
    order = ["EXTREMO_CORTO", "DEPRIMIDO", "NEUTRAL", "ELEVADO", "EXTREMO_LARGO"]
    signal = {"EXTREMO_CORTO": 1, "DEPRIMIDO": 1, "NEUTRAL": 0, "ELEVADO": -1, "EXTREMO_LARGO": -1}

    def row_stats(sub, sig):
        if len(sub) < 5:
            return None
        gap  = sub["gap_pct"].values
        wr   = (gap * sig > 0).mean() * 100 if sig != 0 else 0.0
        _, p = scipy_stats.ttest_1samp(gap * sig, 0)
        return len(sub), gap.mean() * sig, wr, p

    print(f"\n{'─'*72}")
    print(f"  {label}")
    print(f"{'─'*72}")
    print(f"  {'REGIMEN':<20}  {'N':>4}  {'IS WR_GAP':>10}  {'OOS WR_GAP':>10}  {'p(OOS)':>8}")
    print(f"  {'─'*64}")

    for reg in order:
        sig = signal[reg]
        if sig == 0:
            print(f"  {'NEUTRAL':<20}  {'---':>4}")
            continue
        sub_is  = df[(df["regime"] == reg) & (df["report_date"] < oos_cut)]
        sub_oos = df[(df["regime"] == reg) & (df["report_date"] >= oos_cut)]
        is_s    = row_stats(sub_is,  sig)
        oos_s   = row_stats(sub_oos, sig)
        is_str  = f"N={is_s[0]:3d} {is_s[2]:.0f}%"  if is_s  else "N/A"
        if oos_s:
            p_mark = "**" if oos_s[3] < 0.01 else ("*" if oos_s[3] < 0.05 else "  ")
            oos_str = f"N={oos_s[0]:3d} {oos_s[2]:.0f}%{p_mark}"
        else:
            oos_str = "N/A"
        print(f"  {reg:<20}  {is_str:>14}  {oos_str:>14}")


def surprise_stats(df, label, oos_cut):
    print(f"\n  {label} — SORPRESA (velocidad)")
    print(f"  {'─'*60}")
    print(f"  {'SORPRESA':<18}  {'IS WR_GAP':>10}  {'OOS WR_GAP':>10}  {'OOS WR_MON':>10}")
    print(f"  {'─'*60}")

    for sur, sig, tag in [
        ("GRAN_REDUCCION",  1,  "→ LONG  (specs liquidan)"),
        ("GRAN_ADICION",   -1,  "→ SHORT (specs acumulan)"),
    ]:
        sub_is  = df[(df["surprise"] == sur) & (df["report_date"] < oos_cut)]
        sub_oos = df[(df["surprise"] == sur) & (df["report_date"] >= oos_cut)]

        def wr(sub, col, s):
            if len(sub) < 5: return "N/A"
            return f"N={len(sub):2d} {(sub[col] * s > 0).mean()*100:.0f}%"

        print(f"  {sur:<18}  {wr(sub_is, 'gap_pct', sig):>14}  "
              f"{wr(sub_oos, 'gap_pct', sig):>14}  {wr(sub_oos, 'mon_pct', sig):>14}  {tag}")


def run():
    with SessionLocal() as session:
        cot_raw = load_cot(session)
        prices  = load_prices(session)

    print("=" * 72)
    print("  COT BACKTEST: MM (Disaggregated) vs SPEC NET (Legacy NC+NonRep)")
    print(f"  {cot_raw['report_date'].min().date()} → {cot_raw['report_date'].max().date()}")
    print(f"  IS: hasta {OOS_CUT.date()}  |  OOS: desde {OOS_CUT.date()}")
    print("=" * 72)

    results = {}
    for col, label in [("mm_net", "MM NET (Disaggregated — hedge funds/CTAs)"),
                        ("spec_net", "SPEC NET (Legacy NC+NonRep — industria)")]:
        sub = cot_raw.dropna(subset=[col]).copy()
        sub = add_signals(sub, col)
        aligned = align_to_prices(sub, prices)
        results[col] = aligned
        print(f"\n{'='*72}")
        print(f"  {label}  N={len(aligned)}")
        print(f"{'='*72}")
        regime_stats(aligned,  f"REGIMEN — {label}", OOS_CUT)
        surprise_stats(aligned, label, OOS_CUT)

    # ── Comparacion directa ────────────────────────────────────────────────────
    print(f"\n{'='*72}")
    print("  COMPARACION DIRECTA: OOS WR gap Vie->Lun")
    print(f"{'='*72}")
    print(f"  {'SEÑAL':<32}  {'MM WR':>8}  {'SPEC WR':>8}  {'GANADOR':>10}")
    print(f"  {'─'*64}")

    comparisons = [
        ("EXTREMO_CORTO → LONG (nivel)",  "EXTREMO_CORTO",  1,  "gap_pct"),
        ("EXTREMO_LARGO → SHORT (nivel)", "EXTREMO_LARGO",  -1, "gap_pct"),
        ("GRAN_REDUCCION → LONG (vel.)",  None,             1,  "gap_pct"),
        ("GRAN_ADICION → SHORT (vel.)",   None,             -1, "gap_pct"),
    ]

    for desc, reg, sig, col in comparisons:
        wrs = {}
        for series in ["mm_net", "spec_net"]:
            df  = results[series]
            oos = df[df["report_date"] >= OOS_CUT]
            if reg:
                sub = oos[oos["regime"] == reg]
            else:
                sur = "GRAN_REDUCCION" if sig == 1 else "GRAN_ADICION"
                sub = oos[oos["surprise"] == sur]
            if len(sub) >= 5:
                wrs[series] = (sub[col] * sig > 0).mean() * 100
            else:
                wrs[series] = None

        mm_wr   = f"{wrs['mm_net']:.0f}%"   if wrs['mm_net']   else "N/A"
        spec_wr = f"{wrs['spec_net']:.0f}%"  if wrs['spec_net'] else "N/A"

        if wrs['mm_net'] and wrs['spec_net']:
            winner = "MM" if wrs['mm_net'] > wrs['spec_net'] else \
                     "SPEC" if wrs['spec_net'] > wrs['mm_net'] else "EMPATE"
        else:
            winner = "─"

        print(f"  {desc:<32}  {mm_wr:>8}  {spec_wr:>8}  {winner:>10}")

    print(f"\n{'='*72}")
    print("  CORRELACION entre series")
    print(f"{'='*72}")
    merged = cot_raw.dropna(subset=["mm_net", "spec_net"])
    corr   = merged["mm_net"].corr(merged["spec_net"])
    print(f"  mm_net vs spec_net  corr={corr:.4f}  N={len(merged)}")

    # Divergencias: cuando los dos percentiles apuntan en dirección opuesta
    mm_s   = add_signals(merged[["report_date","mm_net"]].copy(), "mm_net")
    sp_s   = add_signals(merged[["report_date","spec_net"]].copy(), "spec_net")
    mm_s   = mm_s.set_index("report_date")
    sp_s   = sp_s.set_index("report_date")
    both   = mm_s[["pct3yr"]].rename(columns={"pct3yr":"mm_pct"}).join(
             sp_s[["pct3yr"]].rename(columns={"pct3yr":"sp_pct"})).dropna()

    # Divergencia: MM extremo pero spec no (o viceversa)
    mm_ext  = (both["mm_pct"] <= EXTREME_LOW) | (both["mm_pct"] >= EXTREME_HIGH)
    sp_ext  = (both["sp_pct"] <= EXTREME_LOW) | (both["sp_pct"] >= EXTREME_HIGH)
    div     = mm_ext ^ sp_ext  # XOR: uno extremo pero no el otro
    print(f"  Semanas con divergencia extremo (uno extremo, otro no): {div.sum()} "
          f"({div.mean()*100:.1f}%)")
    if div.sum() > 0:
        print(f"  Ultimas divergencias:")
        for dt, r in both[div].tail(5).iterrows():
            print(f"    {dt.date()}  MM_pct={r['mm_pct']:.0f}%  SPEC_pct={r['sp_pct']:.0f}%")


if __name__ == "__main__":
    run()
