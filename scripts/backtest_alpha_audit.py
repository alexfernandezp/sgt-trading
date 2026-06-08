"""
SGT Trading — Alpha Audit Backtest
====================================
Mide IC (Spearman) de TODAS las senales disponibles a 5d / 10d / 20d.
Resultado: tabla de alpha por senal y horizonte para decidir
  Motor A (semanal/mensual) | Motor B (diario/intraday) | Papelera (IC~0)

Senales auditadas:
  COT-based  : A1_regime, A2_momentum, A3_comm, MM_pct3yr, OI_div
  Precio     : B2_z26w, WP_zscore (white premium)
  Fundamental: A4_mix (Brazil MAPA sugar mix %), A4_yoy (YoY cana), USDA_stu
  Macro diario: BRL (BRL/USD z-score), Brent (z-score)

Look-ahead controls:
  COT        : entrada al cierre del viernes (report_date + 3 BD)
  White prem : precio cierre dia anterior (sin lag adicional)
  Brazil MAPA: usa report_issue_date (PIT migration P3.E)
  USDA STU   : marketing_year disponible a partir del 1-Oct del mismo year
  BRL / Brent: precio cierre dia anterior

Uso:
  py scripts/backtest_alpha_audit.py
  py scripts/backtest_alpha_audit.py --oos 2023-01-01
  py scripts/backtest_alpha_audit.py --equity
"""
import sys, os, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from scipy.stats import spearmanr
from database import SessionLocal
from sqlalchemy import text

# ── Constantes ────────────────────────────────────────────────────────────────
HORIZONS      = [5, 10, 20]
OOS_DEFAULT   = "2023-01-01"
MIN_IC_OBS    = 30          # minimo de observaciones para IC valido
IC_THRESHOLD  = 0.05        # IC |>= 0.05| considerado con alpha
WIN_THRESHOLD = 53.0        # win rate minimo para considerar edge


# ══════════════════════════════════════════════════════════════════════════════
# 1. CARGA DE DATOS BASE
# ══════════════════════════════════════════════════════════════════════════════

def load_sb_prices(session) -> pd.Series:
    rows = session.execute(text(
        "SELECT date, close FROM price_history "
        "WHERE instrument='SB_CONT' AND close IS NOT NULL "
        "ORDER BY date ASC"
    )).fetchall()
    s = pd.Series(
        {pd.Timestamp(r[0]): float(r[1]) for r in rows},
        name="sb_close"
    )
    return s


def load_cot(session) -> pd.DataFrame:
    rows = session.execute(text(
        "SELECT report_date, speculator_net, comm_net, mm_net, total_oi "
        "FROM cot_data ORDER BY report_date ASC"
    )).fetchall()
    df = pd.DataFrame(rows, columns=["date","spec_net","comm_net","mm_net","total_oi"])
    df["date"] = pd.to_datetime(df["date"])
    for c in ["spec_net","comm_net","mm_net","total_oi"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["spec_net"]).reset_index(drop=True)


def load_ws_prices(session) -> pd.Series:
    rows = session.execute(text(
        "SELECT date, close FROM price_history "
        "WHERE instrument='WS_CONT' AND close IS NOT NULL "
        "ORDER BY date ASC"
    )).fetchall()
    if not rows:
        return pd.Series(dtype=float, name="ws_close")
    return pd.Series(
        {pd.Timestamp(r[0]): float(r[1]) for r in rows},
        name="ws_close"
    )


def load_macro_price(session, instrument: str) -> pd.Series:
    rows = session.execute(text(
        "SELECT date, close FROM price_history "
        "WHERE instrument=:inst AND close IS NOT NULL "
        "ORDER BY date ASC"
    ), {"inst": instrument}).fetchall()
    if not rows:
        return pd.Series(dtype=float, name=instrument)
    return pd.Series(
        {pd.Timestamp(r[0]): float(r[1]) for r in rows},
        name=instrument
    )


def load_brazil_pit(session) -> pd.DataFrame:
    """
    Reconstruye senal A4 (mix azucar) punto-en-tiempo usando report_issue_date.
    Devuelve DataFrame con columnas: issue_date, sugar_mix_pct, cane_cumulative,
    harvest_year, fortnight_seq.
    """
    rows = session.execute(text("""
        SELECT report_issue_date, harvest_year, fortnight_seq,
               cane_crushed_t_cumulative, sugar_mix_pct
        FROM brazil_production
        WHERE report_issue_date IS NOT NULL
          AND sugar_mix_pct IS NOT NULL
          AND cane_crushed_t_cumulative IS NOT NULL
        ORDER BY report_issue_date ASC, fortnight_seq ASC
    """)).fetchall()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=[
        "issue_date","harvest_year","fortnight_seq",
        "cane_cumulative","sugar_mix_pct"
    ])
    df["issue_date"]     = pd.to_datetime(df["issue_date"])
    df["sugar_mix_pct"]  = pd.to_numeric(df["sugar_mix_pct"],  errors="coerce")
    df["cane_cumulative"] = pd.to_numeric(df["cane_cumulative"], errors="coerce")

    # YoY cana: compara misma fortnight_seq con la temporada anterior
    # Construye lookup: (harvest_year, seq) -> cane_cumulative (revision mas reciente)
    latest = (df.sort_values("issue_date")
                .groupby(["harvest_year","fortnight_seq"])
                .last()
                .reset_index()[["harvest_year","fortnight_seq","cane_cumulative"]])
    latest.rename(columns={"cane_cumulative":"cane_prev"}, inplace=True)

    def prev_year(hy):
        try:
            parts = hy.split("-")
            return f"{int(parts[0])-1}-{int(parts[1])-1}"
        except Exception:
            return None

    df["prev_harvest"] = df["harvest_year"].apply(prev_year)
    df = df.merge(latest.rename(columns={"harvest_year":"prev_harvest"}),
                  on=["prev_harvest","fortnight_seq"], how="left")

    df["yoy_pct"] = np.where(
        df["cane_prev"].notna() & (df["cane_prev"] > 0),
        (df["cane_cumulative"] - df["cane_prev"]) / df["cane_prev"] * 100,
        np.nan
    )
    # Senal A4b: mix alto (>45%) = mas azucar = bearish -> negativo
    NEUTRAL_MIX = 45.0
    RANGE_MIX   = 10.0
    df["a4b"] = -((df["sugar_mix_pct"] - NEUTRAL_MIX) / RANGE_MIX).clip(-1, 1)

    # Senal A4a: cana YoY alto = mas oferta = bearish -> negativo
    RANGE_YOY = 10.0
    df["a4a"] = -(df["yoy_pct"] / RANGE_YOY).clip(-1, 1)

    df["a4_signal"] = np.where(
        df["yoy_pct"].notna(),
        0.60 * df["a4a"] + 0.40 * df["a4b"],
        df["a4b"]
    )
    return df[["issue_date","a4_signal","a4b","a4a","sugar_mix_pct","yoy_pct"]].dropna(subset=["a4_signal"])


def load_usda_stu(session) -> pd.DataFrame:
    """
    STU mundial por release (marketing_year + pub_month).
    PIT real: cada registro disponible desde el dia 12 del mes de publicacion.
    pub_month >= 10 -> year = marketing_year
    pub_month <  10 -> year = marketing_year + 1  (temporada Oct-Sep)
    """
    rows = session.execute(text("""
        SELECT marketing_year, pub_month,
               SUM(CASE WHEN attribute_name='ending_stocks'    THEN value_1000mt ELSE 0 END) AS end_stocks,
               SUM(CASE WHEN attribute_name='dom_consumption'  THEN value_1000mt ELSE 0 END) AS consumption
        FROM usda_psd
        WHERE country_code='WB'
          AND attribute_name IN ('ending_stocks','dom_consumption')
          AND value_1000mt IS NOT NULL
        GROUP BY marketing_year, pub_month
        HAVING SUM(CASE WHEN attribute_name='ending_stocks'   THEN value_1000mt ELSE 0 END) > 0
           AND SUM(CASE WHEN attribute_name='dom_consumption' THEN value_1000mt ELSE 0 END) > 0
        ORDER BY marketing_year ASC, pub_month ASC
    """)).fetchall()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["marketing_year","pub_month","end_stocks","consumption"])
    df["end_stocks"]  = pd.to_numeric(df["end_stocks"],  errors="coerce")
    df["consumption"] = pd.to_numeric(df["consumption"], errors="coerce")
    df["stu_pct"] = df["end_stocks"] / df["consumption"] * 100

    # PIT: disponible desde el dia 12 del mes de publicacion
    def avail_date(row):
        year = int(row["marketing_year"]) if int(row["pub_month"]) >= 10 else int(row["marketing_year"]) + 1
        return pd.Timestamp(year=year, month=int(row["pub_month"]), day=12)

    df["avail_date"] = df.apply(avail_date, axis=1)
    return df[["avail_date","marketing_year","pub_month","stu_pct"]].dropna().sort_values("avail_date")


# ══════════════════════════════════════════════════════════════════════════════
# 2. CONSTRUCCION DE SENALES EN FRAME DIARIO
# ══════════════════════════════════════════════════════════════════════════════

def build_daily_frame(sb: pd.Series) -> pd.DataFrame:
    """Frame diario con forward returns."""
    df = pd.DataFrame({"close": sb})
    df.index = pd.DatetimeIndex(df.index)
    for h in HORIZONS:
        df[f"fwd_{h}d"] = df["close"].shift(-h) / df["close"] - 1
    return df


def add_cot_signals(df: pd.DataFrame, cot: pd.DataFrame) -> pd.DataFrame:
    """Agrega senales COT al frame diario con lag de publicacion (3 BD)."""
    c = cot.copy()
    c["entry_date"] = c["date"] + pd.offsets.BusinessDay(3)

    # Percentiles
    c["spec_pct_all"] = c["spec_net"].expanding(min_periods=2).apply(
        lambda x: (x <= x[-1]).mean() * 100, raw=True)
    c["spec_pct_13w"] = c["spec_net"].rolling(13, min_periods=4).apply(
        lambda x: (x <= x[-1]).mean() * 100, raw=True)
    c["spec_ma4"]     = c["spec_net"].rolling(4, min_periods=2).mean()
    c["spec_trend4"]  = c["spec_ma4"] - c["spec_ma4"].shift(1)
    c["comm_13w"]     = c["comm_net"].rolling(13, min_periods=4).mean()
    c["oi_trend4"]    = c["total_oi"] - c["total_oi"].shift(4)

    # MM 3yr percentile (156 semanas)
    c["mm_pct3yr"] = c["mm_net"].rolling(156, min_periods=52).apply(
        lambda x: (x <= x[-1]).mean() * 100, raw=True)

    # Señales binarias
    p, p13, t = c["spec_pct_all"], c["spec_pct_13w"], c["spec_trend4"]
    c["a1"] = np.select(
        [p <= 5, (t < 0) & (p13 <= 40),
         p >= 95, (p >= 85) & (t < 0), (t > 0) & (p13 >= 60)],
        [1, 1, -1, -1, -1], default=0
    )
    c["a2"]    = np.where(c["spec_net"].diff(1) < 0, 1,
                 np.where(c["spec_net"].diff(1) > 0, -1, 0))
    c["a3"]    = np.where(c["comm_net"] > c["comm_13w"], 1,
                 np.where(c["comm_net"] < c["comm_13w"], -1, 0))

    # MM pct3yr como senal continua (-1 a +1)
    c["mm_signal"] = np.where(c["mm_pct3yr"].notna(),
        -((c["mm_pct3yr"] - 50) / 50).clip(-1, 1), np.nan)

    # OI divergencia
    price_up = c["spec_net"].notna()  # placeholder; usamos precio via merge
    c["oi_div"] = np.where(
        c["oi_trend4"] < 0, 0.5, np.where(c["oi_trend4"] > 0, -0.5, 0)
    )

    # Merge en frame diario (ffill: la senal persiste hasta el siguiente COT)
    cot_daily = c.set_index("entry_date")[
        ["a1","a2","a3","mm_signal","oi_div","mm_pct3yr","spec_pct_all","spec_pct_13w"]
    ]
    df = df.merge(cot_daily.reindex(df.index, method="ffill"),
                  left_index=True, right_index=True, how="left")
    return df


def add_b2_signal(df: pd.DataFrame) -> pd.DataFrame:
    """Z-score precio vs media 26 semanas (130 dias)."""
    mu  = df["close"].rolling(130, min_periods=52).mean()
    std = df["close"].rolling(130, min_periods=52).std()
    df["b2_z26"] = ((df["close"] - mu) / std).replace([np.inf, -np.inf], np.nan)
    return df


def add_white_premium(df: pd.DataFrame, sb: pd.Series, ws: pd.Series) -> pd.DataFrame:
    """White premium z-score. No lag adicional (datos del cierre anterior)."""
    if ws.empty:
        df["wp_signal"] = np.nan
        return df
    LBS_PER_MT = 2204.62
    conv = LBS_PER_MT / 100.0
    wp = (ws - sb * conv).rename("wp")
    wp = wp.reindex(df.index, method="ffill")
    # Z-score rolling 1 año (260 sesiones)
    mu  = wp.rolling(260, min_periods=52).mean()
    std = wp.rolling(260, min_periods=52).std()
    df["wp_signal"] = -((wp - mu) / std).replace([np.inf, -np.inf], np.nan)
    # Invertido: WP alto -> bearish para SB -> senal negativa
    return df


def add_brazil_signal(df: pd.DataFrame, br: pd.DataFrame) -> pd.DataFrame:
    """Senal A4 PIT. Step function: usa el dato mas reciente con issue_date <= fecha."""
    if br.empty:
        df["a4_signal"] = np.nan
        df["a4b_mix"]   = np.nan
        return df
    # Dedup: multiples filas por issue_date (distintas quincenas). Tomamos la media
    # del dia (refleja el conjunto de datos publicados ese dia).
    br_day = (br.groupby("issue_date")[["a4_signal","a4b"]]
                .mean()
                .sort_index())
    a4  = br_day["a4_signal"].reindex(df.index, method="ffill")
    a4b = br_day["a4b"].reindex(df.index, method="ffill")
    df["a4_signal"] = a4
    df["a4b_mix"]   = a4b
    return df


def add_usda_signal(df: pd.DataFrame, usda: pd.DataFrame) -> pd.DataFrame:
    """STU como senal continua. Step function anual."""
    if usda.empty:
        df["usda_stu"] = np.nan
        return df
    usda_idx = usda.set_index("avail_date")["stu_pct"].sort_index()
    stu = usda_idx.reindex(df.index, method="ffill")
    # Convertir a senal: STU bajo = LONG (+1), STU alto = SHORT (-1)
    # Normalizar alrededor de 35% (media historica)
    df["usda_stu"] = -((stu - 35) / 10).clip(-1, 1)
    return df


def add_macro_signals(df: pd.DataFrame, brl: pd.Series, brent: pd.Series) -> pd.DataFrame:
    """BRL y Brent: z-score rolling 1 año."""
    for series, col in [(brl, "brl_signal"), (brent, "brent_signal")]:
        if series.empty:
            df[col] = np.nan
            continue
        s   = series.reindex(df.index, method="ffill")
        mu  = s.rolling(260, min_periods=52).mean()
        std = s.rolling(260, min_periods=52).std()
        z   = ((s - mu) / std).replace([np.inf, -np.inf], np.nan)
        # BRL fuerte (z > 0) = bullish SB -> senal positiva (sin invertir)
        # Brent alto (z > 0) = bullish SB -> senal positiva (sin invertir)
        df[col] = z
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 3. CALCULO DE IC (INFORMATION COEFFICIENT)
# ══════════════════════════════════════════════════════════════════════════════

def compute_ic(df: pd.DataFrame, signal_col: str) -> dict:
    """IC (Spearman) por horizonte. Retorna dict horizon -> (IC, p-value, N)."""
    results = {}
    for h in HORIZONS:
        fwd_col = f"fwd_{h}d"
        sub = df[[signal_col, fwd_col]].dropna()
        if len(sub) < MIN_IC_OBS:
            results[h] = (np.nan, np.nan, len(sub))
            continue
        ic, pval = spearmanr(sub[signal_col], sub[fwd_col])
        results[h] = (round(ic, 4), round(pval, 4), len(sub))
    return results


def win_rate(df: pd.DataFrame, signal_col: str, h: int,
             long_thresh=0, short_thresh=0) -> tuple:
    """Win rate para senales de direccion. signal > thresh = LONG, < -thresh = SHORT."""
    fwd = f"fwd_{h}d"
    sub = df[[signal_col, fwd]].dropna()
    if len(sub) < 10:
        return np.nan, np.nan, 0
    long_sub  = sub[sub[signal_col] > long_thresh]
    short_sub = sub[sub[signal_col] < -abs(short_thresh)]
    wr_long  = (long_sub[fwd]  > 0).mean() * 100 if len(long_sub)  >= 5 else np.nan
    wr_short = (short_sub[fwd] < 0).mean() * 100 if len(short_sub) >= 5 else np.nan
    return wr_long, wr_short, len(sub)


# ══════════════════════════════════════════════════════════════════════════════
# 4. EQUITY CURVE COMBINADA
# ══════════════════════════════════════════════════════════════════════════════

def simulate_equity(df: pd.DataFrame, signal_cols: list, hold_days: int = 5,
                    min_signals: int = 2) -> pd.Series:
    """
    Simulacion simple equal-weight:
    Suma de senales activas / n_senales -> posicion [-1, +1].
    Solo entra si |score_norm| >= min_signals / n_senales.
    Sale despues de hold_days dias.
    """
    fwd_col = f"fwd_{hold_days}d"
    sigs = df[signal_cols].dropna(how="all")
    score = sigs.mean(axis=1)   # promedio, ya estan en [-1, +1]
    threshold = (min_signals / len(signal_cols))
    position = np.sign(score) * (score.abs() >= threshold).astype(float)

    pnl = (position.shift(1) * df[fwd_col]).dropna()
    equity = (1 + pnl).cumprod()
    return equity


def sharpe(equity: pd.Series, periods_per_year: int = 252) -> float:
    ret = equity.pct_change().dropna()
    if ret.std() == 0:
        return 0.0
    return float(ret.mean() / ret.std() * np.sqrt(periods_per_year))


def max_drawdown(equity: pd.Series) -> float:
    roll_max = equity.cummax()
    dd = (equity - roll_max) / roll_max
    return float(dd.min())


# ══════════════════════════════════════════════════════════════════════════════
# 5. DISPLAY
# ══════════════════════════════════════════════════════════════════════════════

SIGNAL_META = {
    "a1":          ("A1  COT regime",           "weekly",  "COT"),
    "a2":          ("A2  COT momentum",          "weekly",  "COT"),
    "a3":          ("A3  Comm vs 13w mean",       "weekly",  "COT"),
    "mm_signal":   ("MM  MM pct 3yr rolling",    "weekly",  "COT"),
    "b2_z26":      ("B2  Price Z-score 26w",     "daily",   "PRICE"),
    "wp_signal":   ("WP  White premium z1yr",    "daily",   "PRICE"),
    "a4_signal":   ("A4  Brazil mix+YoY (MAPA)", "biweekly","FUND"),
    "a4b_mix":     ("A4b Brazil mix only",       "biweekly","FUND"),
    "usda_stu":    ("USDA STU ratio mundial",    "monthly", "FUND"),
    "brl_signal":  ("BRL  BRL/USD z-score",      "daily",   "MACRO"),
    "brent_signal":("BRENT Brent z-score",       "daily",   "MACRO"),
}

VERDICT_MOTOR = {
    "COT":   "Motor A",
    "PRICE": "Motor B",
    "FUND":  "Motor A",
    "MACRO": "Motor B",
}


def print_alpha_table(df: pd.DataFrame, label: str = "FULL SAMPLE"):
    print()
    print("=" * 95)
    print(f"  ALPHA AUDIT — {label}")
    print("  IC threshold para alpha: |IC| >= %.2f  |  Win rate threshold: >= %.0f%%" % (
        IC_THRESHOLD, WIN_THRESHOLD))
    print("=" * 95)
    print("  %-28s %-9s %-5s | %7s %5s | %7s %5s | %7s %5s | WR5L  WR5S  VEREDICTO" % (
        "SENAL", "FREQ", "TYPE",
        "IC_5d", "p5d",
        "IC_10d", "p10d",
        "IC_20d", "p20d",
    ))
    print("  " + "-" * 93)

    verdicts = {}
    for col, (label_s, freq, stype) in SIGNAL_META.items():
        if col not in df.columns:
            continue
        ic_res = compute_ic(df, col)
        wrl5, wrs5, n = win_rate(df, col, 5)

        parts = [f"  {label_s:<28} {freq:<9} {stype:<5} |"]
        max_ic = 0.0
        for h in HORIZONS:
            ic, pv, nn = ic_res[h]
            if np.isnan(ic):
                parts.append("    N/A   N/A |"[:15])
            else:
                star = "**" if pv < 0.01 else ("*" if pv < 0.05 else "  ")
                parts.append(f" {ic:+.4f}{star} {pv:.3f} |")
                max_ic = max(max_ic, abs(ic))

        wrl_s = f"{wrl5:.0f}%" if not np.isnan(wrl5 or np.nan) else "  N/A"
        wrs_s = f"{wrs5:.0f}%" if not np.isnan(wrs5 or np.nan) else "  N/A"

        # Veredicto
        has_alpha_5  = abs(ic_res[5][0] or 0)  >= IC_THRESHOLD
        has_alpha_10 = abs(ic_res[10][0] or 0) >= IC_THRESHOLD
        has_alpha_20 = abs(ic_res[20][0] or 0) >= IC_THRESHOLD

        if not (has_alpha_5 or has_alpha_10 or has_alpha_20):
            verdict = "PAPELERA"
        elif has_alpha_5:
            verdict = VERDICT_MOTOR[stype] + " (short-horizon)"
        elif has_alpha_10 or has_alpha_20:
            verdict = VERDICT_MOTOR[stype] + " (long-horizon)"
        else:
            verdict = "MARGINAL"

        verdicts[col] = verdict
        print("".join(parts) + f" {wrl_s:>5} {wrs_s:>5}  {verdict}")

    print()
    return verdicts


def print_oos_comparison(df_is: pd.DataFrame, df_oos: pd.DataFrame):
    print("=" * 95)
    print("  IS vs OOS — IC a 5 dias")
    print("  %-28s | %10s | %10s | %8s  DEGRADACION" % (
        "SENAL", "IS IC_5d", "OOS IC_5d", "DELTA"))
    print("  " + "-" * 75)
    for col, (label_s, *_) in SIGNAL_META.items():
        if col not in df_is.columns:
            continue
        ic_is,  _, n_is  = compute_ic(df_is,  col)[5]
        ic_oos, _, n_oos = compute_ic(df_oos, col)[5]
        if np.isnan(ic_is):
            continue
        if np.isnan(ic_oos):
            delta_s = "N/A (sin datos OOS)"
            deg = ""
        else:
            delta = ic_oos - ic_is
            delta_s = f"{delta:+.4f}"
            if ic_oos >= IC_THRESHOLD and delta >= -0.05:
                deg = "OK"
            elif ic_oos > 0:
                deg = "marginal"
            else:
                deg = "FALLA (IC invertido OOS)"
        print(f"  {label_s:<28} | {ic_is:+.4f} N={n_is:<4} | "
              f"{'N/A' if np.isnan(ic_oos) else f'{ic_oos:+.4f} N={n_oos}':<12}| "
              f"{delta_s:<8}  {deg}")
    print()


def print_equity_summary(df_all: pd.DataFrame, df_oos: pd.DataFrame,
                         signal_cols: list, hold_days: int = 5):
    print("=" * 60)
    print("  EQUITY CURVE — score equal-weight (hold %dd)" % hold_days)

    for label, ddf in [("Full sample", df_all), ("OOS 2023+", df_oos)]:
        avail = [c for c in signal_cols if c in ddf.columns and ddf[c].notna().sum() >= MIN_IC_OBS]
        if len(avail) < 2:
            continue
        eq = simulate_equity(ddf, avail, hold_days=hold_days)
        if eq.empty or len(eq) < 20:
            continue
        total_ret = float(eq.iloc[-1] - 1) * 100
        ann_sh    = sharpe(eq)
        mdd       = max_drawdown(eq) * 100
        n_days    = len(eq)
        print(f"\n  [{label}]  N={n_days}d")
        print(f"    Total return : {total_ret:+.1f}%")
        print(f"    Sharpe       : {ann_sh:.2f}")
        print(f"    Max drawdown : {mdd:.1f}%")
        print(f"    Signals used : {', '.join(avail)}")
    print()


# ══════════════════════════════════════════════════════════════════════════════
# 6. MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--oos",    default=OOS_DEFAULT,
                        help="Fecha corte IS/OOS (default: %s)" % OOS_DEFAULT)
    parser.add_argument("--equity", action="store_true",
                        help="Mostrar simulacion equity curve combinada")
    args = parser.parse_args()
    oos_cutoff = pd.Timestamp(args.oos)

    print("Cargando datos...", end=" ", flush=True)
    session = SessionLocal()
    try:
        sb    = load_sb_prices(session)
        cot   = load_cot(session)
        ws    = load_ws_prices(session)
        br_df = load_brazil_pit(session)
        usda  = load_usda_stu(session)
        brl   = load_macro_price(session, "BRLUSD")
        brent = load_macro_price(session, "BRENT")
    finally:
        session.close()
    print("OK")

    # Disponibilidad de datos
    print(f"  SB_CONT   : {len(sb)} dias  [{sb.index[0].date()} → {sb.index[-1].date()}]")
    print(f"  COT       : {len(cot)} semanas")
    print(f"  WS_CONT   : {len(ws)} dias" + (" [sin datos]" if ws.empty else ""))
    print(f"  Brazil PIT: {len(br_df)} reportes" + (" [sin datos]" if br_df.empty else ""))
    print(f"  USDA STU  : {len(usda)} años" + (" [sin datos]" if usda.empty else ""))
    print(f"  BRLUSD    : {len(brl)} dias" + (" [sin datos]" if brl.empty else ""))
    print(f"  BRENT     : {len(brent)} dias" + (" [sin datos]" if brent.empty else ""))
    print()

    print("Construyendo frame diario y señales...", end=" ", flush=True)
    df = build_daily_frame(sb)
    df = add_cot_signals(df, cot)
    df = add_b2_signal(df)
    df = add_white_premium(df, sb, ws)
    df = add_brazil_signal(df, br_df)
    df = add_usda_signal(df, usda)
    df = add_macro_signals(df, brl, brent)
    df = df.dropna(subset=["close"])
    print("OK  [%d dias utiles, %s → %s]" % (
        len(df), df.index[0].date(), df.index[-1].date()))
    print()

    # Separar IS / OOS
    df_is  = df[df.index <  oos_cutoff]
    df_oos = df[df.index >= oos_cutoff]
    print(f"  IN-SAMPLE  : {len(df_is)} dias  (hasta {oos_cutoff.date()})")
    print(f"  OUT-OF-SAMPLE: {len(df_oos)} dias  ({oos_cutoff.date()} → hoy)")

    # Tablas de IC
    print_alpha_table(df,     "MUESTRA COMPLETA")
    print_alpha_table(df_is,  f"IN-SAMPLE (hasta {oos_cutoff.date()})")
    print_alpha_table(df_oos, f"OUT-OF-SAMPLE ({oos_cutoff.date()} → hoy)")
    print_oos_comparison(df_is, df_oos)

    if args.equity:
        all_signal_cols = [c for c in SIGNAL_META if c in df.columns]
        print_equity_summary(df, df_oos, all_signal_cols, hold_days=5)

    # Resumen ejecutivo
    print("=" * 60)
    print("  RESUMEN EJECUTIVO")
    print("  Señal tiene alpha si |IC| >= %.2f en muestra completa" % IC_THRESHOLD)
    print()
    verdicts = {}
    for col, (label_s, freq, stype) in SIGNAL_META.items():
        if col not in df.columns:
            continue
        ics = [abs(compute_ic(df, col)[h][0] or 0) for h in HORIZONS]
        if max(ics) >= IC_THRESHOLD:
            hz = HORIZONS[ics.index(max(ics))]
            print(f"  ✓  {label_s:<30} → max IC={max(ics):.4f} @ {hz}d → {VERDICT_MOTOR[stype]}")
        else:
            print(f"  ✗  {label_s:<30} → max IC={max(ics):.4f}  → PAPELERA")
    print()


if __name__ == "__main__":
    main()
