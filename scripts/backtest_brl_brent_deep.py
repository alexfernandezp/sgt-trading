"""
Análisis profundo de correlación Brent/BRL vs Sugar No.11.

Testea tres hipótesis que el alpha_audit NO cubre:
  H1: co-movimiento de retornos diarios (no nivel z-score)
  H2: correlación en horizonte corto T+1/T+2 (no solo T+5/10/20)
  H3: ruptura de régimen reciente (post US-Iran, feb 2026)

Uso: py scripts/backtest_brl_brent_deep.py
"""
import sys, os, warnings
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np
from scipy import stats
from sqlalchemy import text
from database import SessionLocal

# ── Carga ─────────────────────────────────────────────────────────────────────

def load_prices(session, instrument: str) -> pd.Series:
    rows = session.execute(text(
        "SELECT date, close FROM price_history "
        "WHERE instrument=:i AND close IS NOT NULL ORDER BY date ASC"
    ), {"i": instrument}).fetchall()
    return pd.Series(
        {pd.Timestamp(r[0]): float(r[1]) for r in rows},
        name=instrument
    )


# ── IC helper ─────────────────────────────────────────────────────────────────

def ic(signal: pd.Series, fwd_ret: pd.Series) -> tuple[float, float, int]:
    """Spearman IC entre señal y retorno forward. Devuelve (ic, p-value, n)."""
    s = pd.concat([signal, fwd_ret], axis=1).dropna()
    if len(s) < 20:
        return float("nan"), float("nan"), 0
    r, p = stats.spearmanr(s.iloc[:, 0], s.iloc[:, 1])
    return round(r, 4), round(p, 4), len(s)


def sig_star(p):
    if p < 0.01: return "**"
    if p < 0.05: return "* "
    return "  "


# ── Análisis ──────────────────────────────────────────────────────────────────

def analyse(name: str, asset: pd.Series, sugar: pd.Series):
    """
    Para un activo (Brent o BRL), corre análisis completo:
      1. Retorno diario asset vs retorno forward sugar (T+0, T+1, T+2, T+5, T+10)
      2. Rolling 60d Pearson corr(ret_asset, ret_sugar) — ver régimen
      3. Pre/Post 28-feb-2026 split
      4. Z-score nivel vs retorno forward (lo que hacía el backtest original)
    """
    print(f"\n{'='*72}")
    print(f"  ANÁLISIS {name}")
    print(f"{'='*72}")

    # Alinear en días comunes
    df = pd.DataFrame({
        "asset":  asset,
        "sugar":  sugar,
    }).dropna()

    # Retornos diarios
    df["ret_asset"] = df["asset"].pct_change()
    df["ret_sugar"] = df["sugar"].pct_change()
    df = df.dropna()

    print(f"  Muestra completa: {df.index[0].date()} -> {df.index[-1].date()}  (N={len(df)})")

    # ── 1. IC de retornos a distintos horizontes ─────────────────────────────
    print(f"\n  [H1+H2] IC retorno diario {name} vs retorno sugar (varios horizontes)")
    print(f"  {'Horizon':<10} {'IC':>8} {'p-val':>8} {'sig':>4} {'N':>6}")
    print(f"  {'-'*42}")

    horizons = [0, 1, 2, 5, 10, 20]
    for h in horizons:
        if h == 0:
            # Contemporánea: ret_asset[T] vs ret_sugar[T]
            ic_v, p_v, n = ic(df["ret_asset"], df["ret_sugar"])
            label = "T+0 (contemp)"
        else:
            fwd = df["ret_sugar"].shift(-h)
            ic_v, p_v, n = ic(df["ret_asset"], fwd)
            label = f"T+{h}d"
        if np.isnan(ic_v):
            print(f"  {label:<14} {'N/A':>8}")
        else:
            print(f"  {label:<14} {ic_v:>8.4f} {p_v:>8.4f} {sig_star(p_v):>4} {n:>6}")

    # ── 2. Rolling 60d Pearson correlation ───────────────────────────────────
    print(f"\n  [H3] Rolling 60d Pearson corr(ret_{name}, ret_sugar) — evolución temporal")
    print(f"  (muestra los percentiles 5/50/95 y el valor post-feb-2026)")

    rolling_corr = df["ret_asset"].rolling(60).corr(df["ret_sugar"])
    rolling_corr = rolling_corr.dropna()

    p5  = rolling_corr.quantile(0.05)
    p50 = rolling_corr.quantile(0.50)
    p95 = rolling_corr.quantile(0.95)
    print(f"    Distribución histórica de corr 60d:  P5={p5:.3f}  mediana={p50:.3f}  P95={p95:.3f}")

    # Último valor
    last_corr = rolling_corr.iloc[-1]
    last_date = rolling_corr.index[-1].date()
    pct_rank  = (rolling_corr < last_corr).mean() * 100
    print(f"    Hoy ({last_date}):  corr60d={last_corr:.3f}  (percentil {pct_rank:.0f}% histórico)")

    # Máximo reciente
    last_90d = rolling_corr[rolling_corr.index >= rolling_corr.index[-1] - pd.Timedelta(days=90)]
    if not last_90d.empty:
        max_90d = last_90d.max()
        max_date = last_90d.idxmax().date()
        print(f"    Máximo últimos 90d: corr={max_90d:.3f} el {max_date}")

    # ── 3. Pre/Post 28-feb-2026 ──────────────────────────────────────────────
    BREAK = pd.Timestamp("2026-02-28")
    pre  = df[df.index <= BREAK]
    post = df[df.index >  BREAK]

    print(f"\n  [H3] Pre/Post US-Iran (28-feb-2026)")
    print(f"  {'Periodo':<22} {'N':>5} {'corr contemp':>14} {'IC T+1':>8} {'IC T+5':>8}")
    print(f"  {'-'*60}")

    for label, subset in [("Pre (hasta 28-feb)", pre), ("Post (desde 1-mar)", post)]:
        n = len(subset)
        if n < 15:
            print(f"  {label:<22} {n:>5}  insuficiente")
            continue
        r_c, _, _ = ic(subset["ret_asset"], subset["ret_sugar"])
        fwd1 = subset["ret_sugar"].shift(-1)
        r1, p1, _ = ic(subset["ret_asset"], fwd1)
        fwd5 = subset["ret_sugar"].shift(-5)
        r5, p5_v, _ = ic(subset["ret_asset"], fwd5)
        s1 = sig_star(p1) if not np.isnan(p1) else "  "
        s5 = sig_star(p5_v) if not np.isnan(p5_v) else "  "
        print(f"  {label:<22} {n:>5}  {r_c:>12.4f}  {r1:>6.4f}{s1}  {r5:>6.4f}{s5}")

    # ── 4. Comparación: nivel z-score vs retorno (por qué el backtest fallaba) ─
    print(f"\n  [Control] z-score nivel (lo que hacía el backtest original)")
    window = 252
    df["zscore"] = (df["asset"] - df["asset"].rolling(window).mean()) / df["asset"].rolling(window).std()
    for h in [5, 10, 20]:
        fwd = df["ret_sugar"].shift(-h)
        ic_z, p_z, n_z = ic(df["zscore"], fwd)
        print(f"    z-score nivel vs T+{h}d sugar ret:  IC={ic_z:.4f}  p={p_z:.4f}  N={n_z}")

    # ── 5. Retorno acumulado 5d asset como señal ──────────────────────────────
    print(f"\n  [Extra] Retorno acumulado 5d {name} como señal predictiva")
    df["ret5d_asset"] = df["asset"].pct_change(5)
    for h in [1, 5, 10]:
        fwd = df["ret_sugar"].shift(-h)
        ic_5r, p_5r, n_5r = ic(df["ret5d_asset"], fwd)
        s = sig_star(p_5r) if not np.isnan(p_5r) else "  "
        print(f"    ret5d {name} vs T+{h}d sugar:  IC={ic_5r:.4f}{s}  p={p_5r:.4f}  N={n_5r}")


def print_crosscorr_matrix(sugar_ret, brl_ret, brent_ret):
    """Matriz de correlaciones a distintos lags para ver estructura de dependencia."""
    print(f"\n{'='*72}")
    print("  MATRIZ DE CORRELACIONES — sugar_ret vs asset_ret a distintos lags")
    print(f"{'='*72}")
    print(f"  {'Lag':<8} {'BRL contemp':>14} {'BRL lag':>10} {'Brent contemp':>15} {'Brent lag':>11}")
    print(f"  {'-'*62}")

    df = pd.DataFrame({
        "sugar": sugar_ret,
        "brl":   brl_ret,
        "brent": brent_ret,
    }).dropna()

    for lag in [0, 1, 2, 3, 5]:
        if lag == 0:
            r_brl,   _ = stats.pearsonr(df["brl"],   df["sugar"])
            r_brent, _ = stats.pearsonr(df["brent"], df["sugar"])
            label = "contemp"
        else:
            shifted = df["sugar"].shift(-lag)
            common = pd.concat([df, shifted.rename("sugar_fwd")], axis=1).dropna()
            r_brl,   _ = stats.pearsonr(common["brl"],   common["sugar_fwd"])
            r_brent, _ = stats.pearsonr(common["brent"], common["sugar_fwd"])
            label = f"T+{lag}"
        print(f"  {label:<8} {r_brl:>14.4f} {'':>10} {r_brent:>15.4f}")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Cargando datos...", end=" ", flush=True)
    session = SessionLocal()
    try:
        sugar = load_prices(session, "SB_CONT")
        brl   = load_prices(session, "BRLUSD")
        brent = load_prices(session, "BRENT")
    finally:
        session.close()
    print("OK")

    print(f"  SB_CONT: {len(sugar)} dias  [{sugar.index[0].date()} -> {sugar.index[-1].date()}]")
    print(f"  BRLUSD:  {len(brl)}  [{brl.index[0].date()} -> {brl.index[-1].date()}]")
    print(f"  BRENT:   {len(brent)}  [{brent.index[0].date()} -> {brent.index[-1].date()}]")

    analyse("BRENT", brent, sugar)
    analyse("BRL/USD", brl, sugar)

    # Matriz cruzada con retornos contemporáneos
    s_ret = sugar.pct_change().dropna()
    b_ret = brl.pct_change().dropna()
    brt_ret = brent.pct_change().dropna()
    print_crosscorr_matrix(s_ret, b_ret, brt_ret)

    print(f"\n{'='*72}")
    print("  NOTA INTERPRETATIVA")
    print(f"{'='*72}")
    print("  - IC contemp T+0: mide si se mueven juntos el MISMO dia (no usable como señal)")
    print("  - IC T+1: brent de HOY puede predecir sugar de MAÑANA (señal real)")
    print("  - Rolling corr 60d: si el percentil actual >> mediana historica = régimen nuevo")
    print("  - Pre/Post feb-2026: cuantifica si el conflicto US-Iran cambió la correlación")
