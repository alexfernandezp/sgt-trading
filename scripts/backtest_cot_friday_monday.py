"""
Backtest COT Friday->Monday gap + follow-through.

Pregunta: cuando el COT sale el viernes 21:30 Madrid con lectura extrema,
que hace el precio desde el cierre del viernes hasta el lunes y la semana?

Metricas por semana COT:
  gap_pct    : (monday_open - friday_close) / friday_close * 100
  monday_pct : (monday_close - monday_open) / monday_open * 100
  full_mon   : (monday_close - friday_close) / friday_close * 100
  fwd_5d     : (close[+5td from monday_open] - monday_open) / monday_open * 100

Regimenes (basados en percentil rolling 3yr = 156 semanas):
  EXTREMO_CORTO  pct3yr <= 10  -> senal LONG contrarian
  DEPRIMIDO      pct3yr <= 25  -> sesgo LONG moderado
  NEUTRAL        25 < pct3yr < 75
  ELEVADO        pct3yr >= 75  -> sesgo SHORT moderado
  EXTREMO_LARGO  pct3yr >= 90  -> senal SHORT contrarian

Tambien analiza el cambio semanal (sorpresa COT):
  GRAN_REDUCCION : cambio mm_net < -1.5 std  (specs liquidando largos / cubriendo cortos)
  GRAN_ADICION   : cambio mm_net > +1.5 std  (specs acumulando)

Uso:
  py scripts/backtest_cot_friday_monday.py
  py scripts/backtest_cot_friday_monday.py --oos 2021-01-01
  py scripts/backtest_cot_friday_monday.py --detail
"""
import sys, os, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from scipy import stats as scipy_stats
from database import SessionLocal
from sqlalchemy import text

# ── Configuracion ─────────────────────────────────────────────────────────────

WINDOW_3YR   = 156   # semanas ventana rolling percentil
MIN_HIST     = 52    # semanas minimas antes de usar el percentil
OOS_DEFAULT  = "2021-01-01"

# Umbrales regimen (sobre pct3yr)
EXTREME_LOW  = 10
DEPRESSED    = 25
ELEVATED     = 75
EXTREME_HIGH = 90

# Umbral sorpresa (z-score cambio semanal mm_net)
SURPRISE_Z   = 1.5


# ── Carga de datos ────────────────────────────────────────────────────────────

def load_cot(session) -> pd.DataFrame:
    rows = session.execute(text(
        "SELECT report_date, speculator_net, mm_net, comm_net "
        "FROM cot_data ORDER BY report_date ASC"
    )).fetchall()
    df = pd.DataFrame(rows, columns=["report_date", "spec_net", "mm_net", "comm_net"])
    df["report_date"] = pd.to_datetime(df["report_date"])
    for c in ["spec_net", "mm_net", "comm_net"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["spec_net"]).reset_index(drop=True)


def load_prices(session) -> pd.DataFrame:
    rows = session.execute(text(
        "SELECT date, open, high, low, close "
        "FROM price_history WHERE instrument='SB_CONT' ORDER BY date ASC"
    )).fetchall()
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close"])
    df["date"] = pd.to_datetime(df["date"])
    for c in ["open", "high", "low", "close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.set_index("date").dropna(subset=["close"])


# ── Construccion de señales COT ───────────────────────────────────────────────

def add_cot_signals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # Percentil rolling 3yr
    df["pct3yr"] = df["mm_net"].rolling(WINDOW_3YR, min_periods=MIN_HIST).apply(
        lambda x: (x <= x[-1]).mean() * 100, raw=True
    )
    # Percentil all-time (expanding)
    df["pct_all"] = df["mm_net"].expanding(min_periods=MIN_HIST).apply(
        lambda x: (x <= x[-1]).mean() * 100, raw=True
    )

    # Cambio semanal mm_net
    df["mm_chg1w"] = df["mm_net"].diff(1)

    # Z-score del cambio semanal (rolling 52w para normalizar)
    chg_std = df["mm_chg1w"].rolling(52, min_periods=20).std()
    df["mm_chg_z"] = df["mm_chg1w"] / chg_std

    # Regimen por nivel
    def classify_regime(p):
        if pd.isna(p):
            return "SIN_DATOS"
        if p <= EXTREME_LOW:
            return "EXTREMO_CORTO"
        if p <= DEPRESSED:
            return "DEPRIMIDO"
        if p >= EXTREME_HIGH:
            return "EXTREMO_LARGO"
        if p >= ELEVATED:
            return "ELEVADO"
        return "NEUTRAL"

    df["regime"] = df["pct3yr"].apply(classify_regime)

    # Sorpresa semanal
    def classify_surprise(z):
        if pd.isna(z):
            return "NORMAL"
        if z < -SURPRISE_Z:
            return "GRAN_REDUCCION"   # specs vendiendo -> señal LONG
        if z > +SURPRISE_Z:
            return "GRAN_ADICION"     # specs comprando -> señal SHORT
        return "NORMAL"

    df["surprise"] = df["mm_chg_z"].apply(classify_surprise)

    return df


# ── Alineacion de fechas COT -> precios ──────────────────────────────────────

def build_price_map(prices: pd.DataFrame) -> dict:
    """Mapa fecha -> (open, high, low, close) + lista ordenada de trading days."""
    pmap = {d: row for d, row in prices.iterrows()}
    tdays = sorted(pmap.keys())
    return pmap, tdays


def next_trading_day(d: pd.Timestamp, tdays: list) -> pd.Timestamp | None:
    """Primer dia habil >= d."""
    for td in tdays:
        if td >= d:
            return td
    return None


def nth_trading_day_after(d: pd.Timestamp, n: int, tdays: list) -> pd.Timestamp | None:
    """n-esimo dia habil estrictamente despues de d."""
    after = [td for td in tdays if td > d]
    return after[n - 1] if len(after) >= n else None


def align_cot_to_prices(cot: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """
    Para cada fila COT (report_date = martes):
      pub_friday     = report_date + 3 dias calendar (= viernes publicacion)
      friday_close   = cierre del dia habil mas cercano >= pub_friday
      action_monday  = 1er dia habil despues de friday
      monday_open    = apertura del action_monday
      monday_close   = cierre del action_monday
      fwd_5d         = cierre 5 dias habiles despues de action_monday
    """
    pmap, tdays = build_price_map(prices)
    tdays_ts    = [pd.Timestamp(d) for d in tdays]

    records = []
    for _, row in cot.iterrows():
        pub_friday    = row["report_date"] + pd.Timedelta(days=3)
        friday_day    = next_trading_day(pub_friday, tdays_ts)
        if friday_day is None or friday_day not in pmap:
            continue

        action_monday = nth_trading_day_after(friday_day, 1, tdays_ts)
        if action_monday is None or action_monday not in pmap:
            continue

        fwd_5d_day = nth_trading_day_after(action_monday, 5, tdays_ts)

        fri_close  = float(pmap[friday_day]["close"])
        mon_open   = float(pmap[action_monday]["open"])
        mon_close  = float(pmap[action_monday]["close"])
        fwd_close  = float(pmap[fwd_5d_day]["close"]) if fwd_5d_day and fwd_5d_day in pmap else None

        records.append({
            "report_date":    row["report_date"],
            "pub_friday":     friday_day,
            "action_monday":  action_monday,
            "pct3yr":         row["pct3yr"],
            "pct_all":        row["pct_all"],
            "mm_net":         row["mm_net"],
            "mm_chg1w":       row["mm_chg1w"],
            "mm_chg_z":       row["mm_chg_z"],
            "regime":         row["regime"],
            "surprise":       row["surprise"],
            "fri_close":      fri_close,
            "mon_open":       mon_open,
            "mon_close":      mon_close,
            "fwd_5d_close":   fwd_close,
            # Retornos
            "gap_pct":        (mon_open  - fri_close) / fri_close * 100,
            "monday_pct":     (mon_close - mon_open)  / mon_open  * 100,
            "full_mon_pct":   (mon_close - fri_close) / fri_close * 100,
            "fwd_5d_pct":     (fwd_close - mon_open)  / mon_open  * 100 if fwd_close else None,
        })

    return pd.DataFrame(records)


# ── Estadisticas ──────────────────────────────────────────────────────────────

def stats_for(series: pd.Series, direction: int = 1):
    """
    direction: +1 para LONG (esperamos retornos positivos), -1 para SHORT.
    Retorna (N, mean, median, win_rate, t_pval).
    """
    s = series.dropna()
    n = len(s)
    if n < 5:
        return n, None, None, None, None
    mean    = s.mean()
    median  = s.median()
    win_rt  = (s * direction > 0).mean() * 100
    t_stat, pval = scipy_stats.ttest_1samp(s * direction, 0)
    return n, mean, median, win_rt, pval


def print_regime_table(df: pd.DataFrame, label: str):
    regimes = [
        ("EXTREMO_CORTO",  +1, "LONG   (contrarian)"),
        ("DEPRIMIDO",      +1, "LONG   (sesgo mod.) "),
        ("NEUTRAL",         0, "---                 "),
        ("ELEVADO",        -1, "SHORT  (sesgo mod.) "),
        ("EXTREMO_LARGO",  -1, "SHORT  (contrarian) "),
    ]

    print(f"\n{'='*80}")
    print(f"  {label}")
    print(f"{'='*80}")
    print(f"  {'REGIMEN':<20} {'SEÑAL':<22} {'N':>4}  "
          f"{'GAP%':>7}  {'WR_GAP':>7}  "
          f"{'MON%':>7}  {'WR_MON':>7}  "
          f"{'5D%':>7}  {'WR_5D':>7}  {'p':>6}")
    print("  " + "-" * 78)

    for regime, direction, signal_lbl in regimes:
        sub = df[df["regime"] == regime]
        if len(sub) == 0:
            print(f"  {regime:<20} {signal_lbl:<22} {'0':>4}")
            continue

        ng, mean_g, _, wr_g, pval_g = stats_for(sub["gap_pct"],      direction)
        nm, mean_m, _, wr_m, pval_m = stats_for(sub["monday_pct"],   direction)
        n5, mean_5, _, wr_5, pval_5 = stats_for(sub["fwd_5d_pct"],   direction)

        def fmt(mean, wr, pval):
            if mean is None:
                return f"{'N/D':>7}  {'N/D':>7}"
            sig = "**" if pval and pval < 0.01 else ("*" if pval and pval < 0.05 else "  ")
            return f"{mean:>+6.2f}%  {wr:>5.1f}%{sig}"

        print(f"  {regime:<20} {signal_lbl:<22} {ng:>4}  "
              f"{fmt(mean_g, wr_g, pval_g)}  "
              f"{fmt(mean_m, wr_m, pval_m)}  "
              f"{fmt(mean_5, wr_5, pval_5)}")

    # Baseline (sin filtro)
    ng, mean_g, _, wr_g, _ = stats_for(df["gap_pct"], +1)
    nm, mean_m, _, wr_m, _ = stats_for(df["monday_pct"], +1)
    n5, mean_5, _, wr_5, _ = stats_for(df["fwd_5d_pct"], +1)
    print("  " + "-" * 78)
    print(f"  {'BASELINE (todo)':<20} {'sin filtro':<22} {ng:>4}  "
          f"{mean_g:>+6.2f}%  {wr_g:>5.1f}%    "
          f"{mean_m:>+6.2f}%  {wr_m:>5.1f}%    "
          f"{mean_5:>+6.2f}%  {wr_5:>5.1f}%  ")
    print(f"\n  Columnas: GAP=viernes_cierre->lunes_apertura | "
          f"MON=sesion_lunes | 5D=5 dias desde apertura_lunes")
    print(f"  WR = % semanas en que el precio fue en la direccion esperada | ** p<.01  * p<.05")


def print_surprise_table(df: pd.DataFrame, label: str):
    """Analiza el impacto del CAMBIO semanal (sorpresa) independientemente del nivel."""
    surprises = [
        ("GRAN_REDUCCION", +1, "LONG  (specs liquidan)"),
        ("NORMAL",          0, "---                  "),
        ("GRAN_ADICION",   -1, "SHORT (specs acumulan)"),
    ]
    print(f"\n{'='*80}")
    print(f"  SORPRESA SEMANAL (cambio mm_net > 1.5 std) — {label}")
    print(f"{'='*80}")
    print(f"  {'SORPRESA':<18} {'SEÑAL':<22} {'N':>4}  "
          f"{'GAP%':>7}  {'WR_GAP':>7}  "
          f"{'MON%':>7}  {'WR_MON':>7}  "
          f"{'5D%':>7}  {'WR_5D':>7}")
    print("  " + "-" * 74)

    for surprise, direction, lbl in surprises:
        sub = df[df["surprise"] == surprise]
        if len(sub) == 0:
            continue
        ng, mean_g, _, wr_g, _ = stats_for(sub["gap_pct"],    direction)
        nm, mean_m, _, wr_m, _ = stats_for(sub["monday_pct"], direction)
        n5, mean_5, _, wr_5, _ = stats_for(sub["fwd_5d_pct"], direction)

        def fmt(mean, wr):
            return f"{mean:>+6.2f}%  {wr:>5.1f}%" if mean is not None else f"{'N/D':>7}  {'N/D':>7}"

        print(f"  {surprise:<18} {lbl:<22} {ng:>4}  "
              f"{fmt(mean_g, wr_g)}  {fmt(mean_m, wr_m)}  {fmt(mean_5, wr_5)}")


def print_extreme_detail(df: pd.DataFrame):
    """Distribucion detallada del gap para extremos COT."""
    print(f"\n{'='*80}")
    print(f"  DISTRIBUCION GAP viernes->lunes (EXTREMOS COT solamente)")
    print(f"{'='*80}")

    for regime, direction, desc in [
        ("EXTREMO_CORTO", +1, "gap POSITIVO = buena señal LONG"),
        ("EXTREMO_LARGO", -1, "gap NEGATIVO = buena señal SHORT"),
    ]:
        sub = df[df["regime"] == regime]["gap_pct"].dropna()
        if len(sub) < 3:
            continue
        buckets = [
            ("> +0.5%",  (sub  >  0.5).sum()),
            ("+0.1 a +0.5%", ((sub > 0.1) & (sub <= 0.5)).sum()),
            ("-0.1 a +0.1%", ((sub >= -0.1) & (sub <= 0.1)).sum()),
            ("-0.5 a -0.1%", ((sub >= -0.5) & (sub < -0.1)).sum()),
            ("< -0.5%",  (sub  < -0.5).sum()),
        ]
        print(f"\n  {regime} (N={len(sub)}, {desc})")
        for lbl, cnt in buckets:
            bar = "█" * cnt
            pct_val = cnt / len(sub) * 100
            print(f"    {lbl:<18}  {cnt:>3}  ({pct_val:.0f}%)  {bar}")
        in_dir = (sub * direction > 0).sum()
        print(f"    Gap en direccion esperada: {in_dir}/{len(sub)} ({in_dir/len(sub)*100:.0f}%)")


def print_combo_table(df: pd.DataFrame, label: str):
    """Extremo de nivel + sorpresa de cambio: la combinacion mas potente."""
    print(f"\n{'='*80}")
    print(f"  COMBINACION NIVEL + SORPRESA — {label}")
    print(f"{'='*80}")
    combos = [
        ("EXTREMO_CORTO",  "GRAN_REDUCCION", +1, "EXTREMO_CORTO + reduccion MM (LONG max)"),
        ("EXTREMO_LARGO",  "GRAN_ADICION",   -1, "EXTREMO_LARGO + adicion MM  (SHORT max)"),
        ("DEPRIMIDO",      "GRAN_REDUCCION", +1, "DEPRIMIDO + reduccion MM     (LONG mod)"),
        ("ELEVADO",        "GRAN_ADICION",   -1, "ELEVADO + adicion MM         (SHORT mod)"),
    ]
    for regime, surprise, direction, desc in combos:
        sub = df[(df["regime"] == regime) & (df["surprise"] == surprise)]
        n = len(sub)
        if n == 0:
            print(f"  {desc:<44}  N=0")
            continue
        ng, mean_g, _, wr_g, pval_g = stats_for(sub["gap_pct"],    direction)
        n5, mean_5, _, wr_5, pval_5 = stats_for(sub["fwd_5d_pct"], direction)
        sig_g = "**" if pval_g and pval_g < 0.01 else ("*" if pval_g and pval_g < 0.05 else "  ")
        sig_5 = "**" if pval_5 and pval_5 < 0.01 else ("*" if pval_5 and pval_5 < 0.05 else "  ")
        print(f"  {desc:<44}  N={n:>3}  "
              f"GAP={mean_g:>+5.2f}%({wr_g:.0f}%{sig_g})  "
              f"5D={mean_5:>+5.2f}%({wr_5:.0f}%{sig_5})" if mean_g is not None else
              f"  {desc:<44}  N={n:>3}  datos insuficientes")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--oos",    default=OOS_DEFAULT,
                        help="Fecha corte IS/OOS (default: %s)" % OOS_DEFAULT)
    parser.add_argument("--detail", action="store_true",
                        help="Mostrar tabla detallada de extremos")
    args = parser.parse_args()
    oos_cutoff = pd.Timestamp(args.oos)

    print("Cargando datos...", end=" ", flush=True)
    session = SessionLocal()
    try:
        cot    = load_cot(session)
        prices = load_prices(session)
    finally:
        session.close()
    print(f"OK  COT={len(cot)} semanas  SB_CONT={len(prices)} dias")

    print("Calculando señales COT...", end=" ", flush=True)
    cot = add_cot_signals(cot)
    print("OK")

    print("Alineando fechas COT->precios...", end=" ", flush=True)
    df = align_cot_to_prices(cot, prices)
    df = df.dropna(subset=["gap_pct", "monday_pct"])
    print(f"OK  [{len(df)} semanas con precios completos, "
          f"{df['report_date'].min().date()} -> {df['report_date'].max().date()}]")

    df_is  = df[df["report_date"] <  oos_cutoff]
    df_oos = df[df["report_date"] >= oos_cutoff]

    print(f"\n  IN-SAMPLE  (hasta {oos_cutoff.date()}): {len(df_is)} semanas")
    print(f"  OUT-OF-SAMPLE ({oos_cutoff.date()}+):   {len(df_oos)} semanas")
    print(f"\n  Regimenes en muestra completa:")
    for r in ["EXTREMO_CORTO","DEPRIMIDO","NEUTRAL","ELEVADO","EXTREMO_LARGO"]:
        n = (df["regime"] == r).sum()
        print(f"    {r:<20} N={n:>4}  ({n/len(df)*100:.0f}%)")

    # ── Tablas principales ────────────────────────────────────────────────
    print_regime_table(df,     "MUESTRA COMPLETA")
    print_regime_table(df_is,  f"IN-SAMPLE (hasta {oos_cutoff.date()})")
    print_regime_table(df_oos, f"OUT-OF-SAMPLE ({oos_cutoff.date()}+)")

    print_surprise_table(df,     "MUESTRA COMPLETA")
    print_surprise_table(df_oos, f"OOS {oos_cutoff.date()}+")

    print_combo_table(df,     "MUESTRA COMPLETA")
    print_combo_table(df_oos, f"OOS {oos_cutoff.date()}+")

    if args.detail:
        print_extreme_detail(df)

    # ── Resumen ejecutivo ─────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print("  RESUMEN EJECUTIVO")
    print(f"{'='*80}")

    # ¿Cuanto vale el gap en extremos?
    for regime, direction, lbl in [("EXTREMO_CORTO", +1, "LONG"), ("EXTREMO_LARGO", -1, "SHORT")]:
        sub = df[df["regime"] == regime]
        sub_oos = df_oos[df_oos["regime"] == regime]
        n_g, mean_g, _, wr_g, pval_g = stats_for(sub["gap_pct"], direction)
        n_5, mean_5, _, wr_5, pval_5 = stats_for(sub["fwd_5d_pct"], direction)
        no_g, mean_go, _, wr_go, _ = stats_for(sub_oos["gap_pct"], direction)
        sig_g = "**" if pval_g and pval_g < 0.01 else ("*" if pval_g and pval_g < 0.05 else "")
        sig_5 = "**" if pval_5 and pval_5 < 0.01 else ("*" if pval_5 and pval_5 < 0.05 else "")
        if mean_g is not None:
            print(f"\n  {regime} -> {lbl} (N={n_g}):")
            print(f"    Gap Vie->Lun  : {mean_g:>+5.2f}%  WR={wr_g:.0f}%{sig_g}  "
                  f"(OOS N={no_g}: {mean_go:>+5.2f}% WR={wr_go:.0f}%)")
            print(f"    Follow-through 5d: {mean_5:>+5.2f}%  WR={wr_5:.0f}%{sig_5}")
            if mean_g is not None and wr_g > 55:
                print(f"    >>> EDGE DETECTADO: {wr_g:.0f}% de los viernes extremos "
                      f"abren el lunes en la direccion esperada")
            else:
                print(f"    Sin edge claro en el gap (WR={wr_g:.0f}%)")

    print()


if __name__ == "__main__":
    main()
