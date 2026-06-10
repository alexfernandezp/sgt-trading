"""
Backtest Layer 1 v2 — 100% vectorizado, corre en <3 segundos.

Señales: A1 (COT regime), A2 (momentum), A3 (comerciales), MM (Managed Money),
         B2 (Z-score 26w), OI (divergencia precio-OI)
Horizontes: 5d / 10d / 20d

Uso:
  py scripts/backtest_layer1.py
  py scripts/backtest_layer1.py --detail
"""
import sys, os, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from database import SessionLocal
from sqlalchemy import text


def load_data(session):
    cot = pd.DataFrame(session.execute(text(
        "SELECT report_date, speculator_net, comm_net, mm_net, total_oi "
        "FROM cot_data ORDER BY report_date ASC"
    )).fetchall(), columns=["date","spec_net","comm_net","mm_net","total_oi"])
    cot["date"] = pd.to_datetime(cot["date"])
    for c in ["spec_net","comm_net","mm_net","total_oi"]:
        cot[c] = pd.to_numeric(cot[c], errors="coerce")

    sb = pd.DataFrame(session.execute(text(
        "SELECT date, close FROM price_history WHERE instrument='SB_CONT' ORDER BY date ASC"
    )).fetchall(), columns=["date","close"])
    sb["date"] = pd.to_datetime(sb["date"])
    sb = sb.set_index("date")["close"].astype(float)

    return cot, sb


def rolling_percentile(series):
    """Percentil expanding all-time — raw=True es ~100x mas rapido."""
    return series.expanding(min_periods=2).apply(
        lambda x: (x <= x[-1]).mean() * 100, raw=True)


def rolling_pct_n(series, n):
    """Percentil rolling ventana n semanas — raw=True."""
    return series.rolling(window=n, min_periods=max(4, n // 2)).apply(
        lambda x: (x <= x[-1]).mean() * 100, raw=True)


def build_signals_vectorized(cot, sb, min_hist=52):
    df = cot.copy().reset_index(drop=True)

    # --- COT percentiles (vectorizado con expanding) ---
    df["spec_pct_all"] = rolling_percentile(df["spec_net"])
    df["spec_pct_13w"] = rolling_pct_n(df["spec_net"], 13)
    df["spec_ma4"]     = df["spec_net"].rolling(4, min_periods=2).mean()
    df["spec_ma4_lag"] = df["spec_ma4"].shift(1)
    df["spec_trend4"]  = df["spec_ma4"] - df["spec_ma4_lag"]
    df["comm_mean"]    = df["comm_net"].expanding(min_periods=4).mean()   # all-time (referencia)
    df["comm_mean_13w"] = df["comm_net"].rolling(13, min_periods=4).mean() # 13s (señal activa)

    # MM vs 13w mean (contrarian)
    df["mm_mean_13w"] = df["mm_net"].rolling(13, min_periods=4).mean()

    # OI trend
    df["oi_trend4"]  = df["total_oi"] - df["total_oi"].shift(4)

    # --- Z-score 26w (130d) para B2 — calculado sobre precios diarios antes del merge ---
    _sb_mean130 = sb.rolling(130, min_periods=52).mean()
    _sb_std130  = sb.rolling(130, min_periods=52).std()
    _sb_z26     = (sb - _sb_mean130) / _sb_std130

    # --- Precio SB alineado con fechas COT (merge_asof: precio mas reciente <= fecha COT) ---
    sb_df = sb.reset_index().rename(columns={"date":"sb_date","close":"sb_close"})
    df_m  = pd.merge_asof(df.sort_values("date"), sb_df, left_on="date", right_on="sb_date")
    df_m["sb_ma20"] = sb.rolling(20, min_periods=20).mean().reindex(
        df_m["sb_date"], method="ffill").values
    df_m["sb_z26"]  = _sb_z26.reindex(df_m["sb_date"], method="ffill").values

    has_mm = df_m["mm_net"].notna().sum() > 10

    # --- Precio 4s atras para OI ---
    df_m["sb_4w_ago"] = df_m["sb_close"].shift(4)

    # --- Forward returns con lag de publicacion COT ---
    # Posiciones as-of martes; publicacion CFTC el viernes siguiente (~3 business days)
    # p0 = precio del viernes (primera vez que podemos actuar con la informacion)
    entry_dti    = pd.DatetimeIndex(df_m["date"].values) + pd.offsets.BusinessDay(3)
    df_m["sb_entry"] = sb.reindex(entry_dti, method="ffill").values
    for h in [5, 10, 20]:
        fwd = sb.shift(-h)
        p0  = df_m["sb_entry"].values
        df_m["fwd_%dd" % h] = np.where(
            (p0 > 0) & ~np.isnan(p0),
            (fwd.reindex(entry_dti, method="ffill").values - p0) / p0 * 100,
            np.nan)

    # --- Aplicar señales ---
    # A1: COT regime
    p  = df_m["spec_pct_all"]
    p13 = df_m["spec_pct_13w"]
    t  = df_m["spec_trend4"]
    # Prioridad: extremos absolutos primero, luego crowded, luego contrarian
    conditions_long  = [p <= 5,                           # EXTREMO_CORTO → rebote inminente
                        (t < 0) & (p13 <= 40)]            # CROWDED_SHORT  → specs muy cortos, contrarian LONG
    conditions_short = [p >= 95,                          # EXTREMO_LARGO  → liquidacion inminente
                        (p >= 85) & (t < 0),              # CONTRARIAN_SHORT → extremo + revirtiendo
                        (t > 0) & (p13 >= 60)]            # CROWDED_LONG   → specs muy largos, contrarian SHORT
    labels_long  = ["EXTREMO_CORTO_ABSOLUTO", "CROWDED_SHORT"]
    labels_short = ["EXTREMO_LARGO_ABSOLUTO", "CONTRARIAN_SHORT", "CROWDED_LONG"]

    df_m["regime"] = "NEUTRAL"
    for cond, lbl in zip(conditions_long, labels_long):
        df_m.loc[cond & (df_m["regime"] == "NEUTRAL"), "regime"] = lbl
    for cond, lbl in zip(conditions_short, labels_short):
        df_m.loc[cond & (df_m["regime"] == "NEUTRAL"), "regime"] = lbl

    regime_long  = {"EXTREMO_CORTO_ABSOLUTO", "CROWDED_SHORT"}
    regime_short = {"EXTREMO_LARGO_ABSOLUTO", "CONTRARIAN_SHORT", "CROWDED_LONG"}
    df_m["a1_long"]  = df_m["regime"].isin(regime_long).astype(int)
    df_m["a1_short"] = df_m["regime"].isin(regime_short).astype(int)

    # A2: momentum especulador INVERTIDO (contrarian: specs comprando = señal bajista)
    # Cuando specs aumentan posicion esta semana → se equivocan (top/bottom cercano) → SHORT
    # Cuando specs reducen posicion esta semana → contrarian LONG
    chg1 = df_m["spec_net"].diff(1)
    df_m["a2_long"]  = (chg1 < 0).astype(int)   # spec baja → LONG
    df_m["a2_short"] = (chg1 > 0).astype(int)   # spec sube → SHORT

    # A3: comerciales vs media 13 semanas (no all-time — backtest muestra 52% LONG / 57% SHORT)
    # comm > 13w mean → comerciales menos hedgeados de lo reciente → LONG
    # comm < 13w mean → comerciales añadiendo hedges vs reciente → SHORT
    df_m["a3_long"]  = (df_m["comm_net"] > df_m["comm_mean_13w"]).astype(int)
    df_m["a3_short"] = (df_m["comm_net"] < df_m["comm_mean_13w"]).astype(int)
    df_m.loc[df_m["comm_mean_13w"].isna(), ["a3_long","a3_short"]] = np.nan

    # MM: vs 13w mean (contrarian: mm > 13w mean → overextended → SHORT 57.8%; mm < 13w → LONG 53.6%)
    df_m["mm_long"]  = (df_m["mm_net"] < df_m["mm_mean_13w"]).astype(float)
    df_m["mm_short"] = (df_m["mm_net"] > df_m["mm_mean_13w"]).astype(float)
    df_m.loc[df_m["mm_net"].isna() | df_m["mm_mean_13w"].isna(), ["mm_long","mm_short"]] = np.nan
    if not has_mm:
        df_m["mm_long"] = np.nan; df_m["mm_short"] = np.nan

    # B2: Z-score precio vs media 26w (130d) — más riguroso que extensión MA20
    # z < -1.5 → LONG 57.4% (N=68), z > +1.5 → SHORT 59.0% (N=61)
    df_m["b2_z26"]   = df_m["sb_z26"]
    df_m["b2_long"]  = (df_m["b2_z26"] < -1.5).astype(float)
    df_m["b2_short"] = (df_m["b2_z26"] > +1.5).astype(float)
    df_m.loc[df_m["b2_z26"].isna(), ["b2_long","b2_short"]] = np.nan

    # OI divergencia precio-OI (capitulación / distribución)
    # OI↓ + precio↓ = capitulación → LONG 57.7%; OI↓ + precio↑ = distribución → SHORT 58.6%
    price_up = df_m["sb_close"] > df_m["sb_4w_ago"]
    oi_fall  = df_m["oi_trend4"] < 0
    df_m["oi_long"]  = (oi_fall & ~price_up).astype(float)
    df_m["oi_short"] = (oi_fall &  price_up).astype(float)
    df_m.loc[df_m["oi_trend4"].isna() | df_m["sb_4w_ago"].isna(),
             ["oi_long","oi_short"]] = np.nan

    # Filtrar min_hist y filas sin retorno
    df_out = df_m.iloc[min_hist:].copy()
    df_out = df_out[df_out["fwd_5d"].notna()].copy()
    return df_out, has_mm


# ---------------------------------------------------------------------------
# Analisis
# ---------------------------------------------------------------------------

def win_stats(sub, fwd_col, sign):
    s = sub[fwd_col].dropna() * sign
    if len(s) == 0: return 0, None, None
    return len(s), (s > 0).mean() * 100, s.mean()


def win_pval(n, wr_pct):
    """Binomial one-tailed p-value H0: WR <= 50%. Usa scipy si disponible."""
    if n < 5: return 1.0
    k = int(round(n * wr_pct / 100))
    try:
        from scipy.stats import binomtest
        return binomtest(k, n, 0.5, alternative='greater').pvalue
    except ImportError:
        import math
        z = (wr_pct / 100 - 0.5) / math.sqrt(0.25 / n)
        return 0.5 * math.erfc(z / math.sqrt(2))


def print_criteria(df, horizons, criteria):
    hdr = ("  %-10s  %-6s" % ("CRITERIO","DIR")) + "".join(["  %5s  %9s  %7s" % ("N","WIN%d"%h,"AVG%") for h in horizons])
    print(hdr)
    print("  " + "-" * (18 + 23 * len(horizons)))
    for label, lc, sc in criteria:
        for col, direction, sign in [(lc,"LONG",1),(sc,"SHORT",-1)]:
            sub   = df[df[col] == 1]
            parts = [label, direction]
            for h in horizons:
                n, wr, mr = win_stats(sub, "fwd_%dd" % h, sign)
                if wr is None: parts += ["-","-","-"]
                else:
                    mk  = "✓" if wr >= 55 else ("✗" if wr <= 45 else " ")
                    sm  = "**" if win_pval(n, wr) < 0.01 else ("*" if win_pval(n, wr) < 0.05 else "")
                    parts += [str(n), "%.1f%%%s%s"%(wr, mk, sm), "%+.3f%%"%mr]
            fmt = "  %-10s  %-6s" + "".join(["  %5s  %9s  %7s"]*len(horizons))
            print(fmt % tuple(parts))
    print()


def print_score(df, horizons, lc, sc, title):
    print("=== %s ===" % title)
    for direction, score_col, sign in [("LONG",lc,1),("SHORT",sc,-1)]:
        print("  %s:" % direction)
        hdr = ("  %-5s" % "SCORE") + "".join(["  %5s  %6s  %7s" % ("N","WIN%d"%h,"AVG%") for h in horizons])
        print(hdr)
        for score in range(5):
            sub   = df[df[score_col] == score]
            parts = [str(score)]
            for h in horizons:
                n, wr, mr = win_stats(sub, "fwd_%dd" % h, sign)
                if wr is None: parts += ["-","-","-"]
                else:
                    mk = " <<<" if score >= 3 else ""
                    parts += [str(n), "%.1f%%"%wr+mk, "%+.3f%%"%mr]
            fmt = "  %-5s" + "".join(["  %5s  %6s  %7s"]*len(horizons))
            print(fmt % tuple(parts))
        print()


def print_oos_comparison(df_is, df_oos, criteria):
    """Tabla IS vs OOS en horizonte 5d. ** p<.01, * p<.05."""
    print("  %-10s  %-5s  |  %-18s  |  %-18s  | DELTA    VEREDICTO" % (
        "CRITERIO","DIR","IS 2017-22","OOS 2023+"))
    print("  " + "-" * 76)
    for label, lc, sc in criteria:
        for col, direction, sign in [(lc,"LONG",1),(sc,"SHORT",-1)]:
            ni, wri, _ = win_stats(df_is[df_is[col]==1],   "fwd_5d", sign)
            no, wro, _ = win_stats(df_oos[df_oos[col]==1], "fwd_5d", sign)
            if wri is None or ni < 5: continue
            smi = "**" if win_pval(ni,wri) < 0.01 else ("*" if win_pval(ni,wri) < 0.05 else "  ")
            smo = ("**" if win_pval(no,wro) < 0.01 else ("*" if win_pval(no,wro) < 0.05 else "  ")) if wro and no >= 5 else "  "
            is_str  = "%5.1f%% %-2s N=%-3d" % (wri, smi, ni)
            oos_str = ("%5.1f%% %-2s N=%-3d" % (wro, smo, no)) if wro and no >= 5 else "N/A"
            if not wro or no < 5:
                delta, verdict = "N/A", "sin datos"
            else:
                d = wro - wri
                delta = "%+.1f%%" % d
                verdict = "OK" if wro >= 53 and d >= -7 else ("marginal" if wro >= 50 else "FALLA")
            print("  %-10s  %-5s  |  %-18s  |  %-18s  | %-8s %s" % (
                label, direction, is_str, oos_str, delta, verdict))
    print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--detail", action="store_true")
    args = parser.parse_args()
    horizons = [5, 10, 20]

    session = SessionLocal()
    cot, sb = load_data(session)
    session.close()

    print("Calculando señales (vectorizado)...", end=" ", flush=True)
    df, has_mm = build_signals_vectorized(cot, sb)
    print("OK  [%d semanas, %s -> %s]" % (
        len(df), df["date"].iloc[0].date(), df["date"].iloc[-1].date()))
    print("  Managed Money disagg: %s" % ("SI" if has_mm else "NO"))
    print()

    criteria = [
        ("A1_COT",  "a1_long",  "a1_short"),
        ("A2_INV",  "a2_long",  "a2_short"),
        ("A3_13w",  "a3_long",  "a3_short"),
        ("B2_Z26w", "b2_long",  "b2_short"),
    ]
    if has_mm: criteria.append(("MM_13w", "mm_long", "mm_short"))
    criteria.append(("OI_DIV",  "oi_long", "oi_short"))

    print("=== WIN RATE POR CRITERIO (5d / 10d / 20d) ===")
    print_criteria(df, horizons, criteria)

    # Score L1: LONG=A1+A2+A3+B2 (B2 válido LONG 20d=59.3% en 18 años)
    #           SHORT=A1+A2+A3    (B2 SHORT falla: 47.6% en muestra 18 años)
    df["l1_long"]  = df[["a1_long","a2_long","a3_long","b2_long"]].sum(axis=1, min_count=1)
    df["l1_short"] = df[["a1_short","a2_short","a3_short"]].sum(axis=1, min_count=1)
    print_score(df, horizons, "l1_long", "l1_short",
                "SCORE L1 (LONG=A1+A2+A3+B2 | SHORT=A1+A2+A3)")

    # B2 LONG como señal swing de horizonte extendido (20d)
    sub_b2l = df[df["b2_long"] == 1]
    n20, wr20, mr20 = win_stats(sub_b2l, "fwd_20d", 1)
    print("  [B2_Z26w LONG = señal SWING 20d]  N=%d  WIN20d=%.1f%%  Avg=%+.3f%%\n" % (
        n20, wr20 if wr20 else 0, mr20 if mr20 else 0))

    # Score L1v2 (A1+MM+OI — WP eliminado por falta de edge)
    nl = ["a1_long"];  ns = ["a1_short"]
    if has_mm: nl.append("mm_long");  ns.append("mm_short")
    nl.append("oi_long");  ns.append("oi_short")
    df["l1v2_long"]  = df[nl].sum(axis=1, min_count=1)
    df["l1v2_short"] = df[ns].sum(axis=1, min_count=1)
    lbl = "SCORE L1v2 (A1+MM+OI)" if has_mm else "SCORE L1v2 (A1+OI)"
    print_score(df, horizons, "l1v2_long", "l1v2_short", lbl)

    # Regimenes
    regime_sign = {
        "EXTREMO_CORTO_ABSOLUTO": 1,
        "CROWDED_SHORT":          1,
        "EXTREMO_LARGO_ABSOLUTO": -1,
        "CONTRARIAN_SHORT":       -1,
        "CROWDED_LONG":           -1,
        "NEUTRAL":                 0,
    }
    print("=== REGIMEN COT — WIN RATE por horizonte ===")
    print("  %-28s  %-7s  %5s  %7s  %7s  %7s" % ("REGIMEN","DIR","N","WIN_5d","WIN_10d","WIN_20d"))
    print("  " + "-" * 70)
    for regime, sign in regime_sign.items():
        sub = df[df["regime"] == regime]
        if len(sub) == 0: continue
        direction = {1:"LONG",-1:"SHORT",0:"NEUTRAL"}[sign]
        stats = []
        for h in [5,10,20]:
            n, wr, _ = win_stats(sub, "fwd_%dd"%h, sign if sign!=0 else 1)
            stats.append("%.0f%%"%wr if wr is not None else "-")
        print("  %-28s  %-7s  %5d  %7s  %7s  %7s" % (
            regime, direction, len(sub), stats[0], stats[1], stats[2]))
    print()

    print("=== BASELINE (sin filtro) ===")
    for h in [5,10,20]:
        s = df["fwd_%dd"%h].dropna()
        print("  %2dd: N=%d  Win_LONG=%.1f%%  Mean=%.3f%%" % (h,len(s),(s>0).mean()*100,s.mean()))
    print()

    # -----------------------------------------------------------------------
    # VALIDACION OUT-OF-SAMPLE
    # -----------------------------------------------------------------------
    cutoff = pd.Timestamp("2023-01-01")
    df_is  = df[df["date"] <  cutoff].copy()
    df_oos = df[df["date"] >= cutoff].copy()

    print("=" * 76)
    print("=== VALIDACION OUT-OF-SAMPLE ===")
    print("  IN-SAMPLE  2017-2022 : %d semanas" % len(df_is))
    print("  OUT-OF-SAMPLE 2023+  : %d semanas (incluye 2023, 2024, 2025)" % len(df_oos))
    print("  OK=OOS WR>=53% y no cae >7pp vs IS  |  marginal=[50-53%]  |  FALLA=<50%")
    print("  ** p<.01  * p<.05  (test binomial unilateral WR>50%)")
    print()
    print_oos_comparison(df_is, df_oos, criteria)

    print("--- SCORE L1 OOS (LONG=A1+A2+A3 | SHORT=A1+A2+A3+B2) ---")
    print_score(df_oos, [5, 10], "l1_long", "l1_short",
                "SCORE L1 OUT-OF-SAMPLE (2023+)")
    lbl_v2 = ("SCORE L1v2 OOS (A1+MM+OI) 2023+" if has_mm else "SCORE L1v2 OOS (A1+OI) 2023+")
    print_score(df_oos, [5, 10], "l1v2_long", "l1v2_short", lbl_v2)
    print("=" * 76)
    print()

    # --- Resumen de confianza por direccion ---
    # Basado en validacion OOS: cuantas senales aguantan por direccion
    oos_ok_short = sum(1 for lbl, lc, sc in criteria
                       if win_stats(df_oos[df_oos[sc]==1], "fwd_5d", -1)[1] is not None
                       and win_stats(df_oos[df_oos[sc]==1], "fwd_5d", -1)[1] >= 53)
    oos_ok_long  = sum(1 for lbl, lc, sc in criteria
                       if win_stats(df_oos[df_oos[lc]==1], "fwd_5d",  1)[1] is not None
                       and win_stats(df_oos[df_oos[lc]==1], "fwd_5d",  1)[1] >= 53)
    n_crit = len(criteria)
    print("=== DIAGNOSTICO DE CONFIANZA (OOS 2023+) ===")
    print("  SHORT: %d/%d senales con WR>=53%% OOS  ->  %s" % (
        oos_ok_short, n_crit,
        "ALTA CONFIANZA" if oos_ok_short >= 4 else ("MODERADA" if oos_ok_short >= 2 else "BAJA")))
    print("  LONG : %d/%d senales con WR>=53%% OOS  ->  %s" % (
        oos_ok_long, n_crit,
        "ALTA CONFIANZA" if oos_ok_long >= 4 else ("MODERADA" if oos_ok_long >= 2 else "BAJA")))
    print("  Senales LONG  validas OOS: B2_Z26w (swing 20d), OI_DIV")
    print("  Senales SHORT validas OOS: A1, A2, A3, MM, OI_DIV  [B2 excluido: falla 18 años]")
    print()

    if args.detail:
        cols = ["date","regime","a1_long","a2_long","a3_long","b2_long","b2_short"]
        if has_mm: cols.insert(4,"mm_long")
        cols += ["fwd_5d","fwd_10d","fwd_20d"]
        print("\n=== DETALLE ===")
        print(df[cols].to_string(index=False))


if __name__ == "__main__":
    main()
