"""
Cross-correlación CEPEA parity ratio vs MAPA sugar_mix_pct.

Hipótesis: la paridad CEPEA (precio diario) puede liderar la
decisión de mezcla azúcar/etanol que reporta MAPA cada 2 semanas,
porque los ingenios ajustan su mix antes de que el dato aparezca
en el informe MAPA.

Metodología:
  1. Ratio diario CEPEA = ethanol_c_lb / ice_c_lb (de DB + yfinance)
  2. Para cada fecha de informe MAPA: mean ratio en las N semanas previas
  3. Pearson entre mean_ratio_Nw_before y sugar_mix_pct al lag 0/+1/+2 fortnights
  4. Interpretación: lag positivo = CEPEA lidera MAPA

Nota: ratio alto (etanol > azúcar) → mills producen más etanol → sugar_mix_pct baja
      Se espera correlación NEGATIVA entre mean_ratio y sugar_mix_pct
"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
import yfinance as yf
from sqlalchemy import text
from scipy import stats
from database import SessionLocal

ATR_FACTOR  = 1.4966
LBS_PER_TON = 2204.62

print("=" * 70)
print("  CROSS-CORRELACIÓN CEPEA RATIO vs MAPA SUGAR MIX %")
print("=" * 70)

session = SessionLocal()

# ─── 1. CEPEA hydrous diario desde DB ────────────────────────────────────────
cepea_rows = session.execute(text("""
    SELECT price_date, price_usd FROM cepea_prices
    WHERE series_name = 'hydrous_paulinia_usd_m3'
    ORDER BY price_date
""")).fetchall()

# ─── 2. MAPA sugar_mix_pct desde brazil_production ───────────────────────────
mapa_rows = session.execute(text("""
    SELECT report_date, sugar_mix_pct FROM brazil_production
    WHERE sugar_mix_pct IS NOT NULL
    ORDER BY report_date
""")).fetchall()

session.close()

if not cepea_rows:
    print("ERROR: sin datos CEPEA en DB"); sys.exit(1)
if not mapa_rows:
    print("ERROR: sin datos MAPA en DB (brazil_production.sugar_mix_pct)"); sys.exit(1)

cepea = pd.DataFrame(cepea_rows, columns=["date", "hydrous"])
cepea["date"]    = pd.to_datetime(cepea["date"])
cepea["hydrous"] = cepea["hydrous"].astype(float)
cepea = cepea.set_index("date").sort_index()
cepea["eth_clb"] = cepea["hydrous"] * 100 / (ATR_FACTOR * LBS_PER_TON)

print(f"\nCEPEA: {cepea.index[0].date()} → {cepea.index[-1].date()}  ({len(cepea)} días)")

mapa = pd.DataFrame(mapa_rows, columns=["date", "sugar_mix_pct"])
mapa["date"]          = pd.to_datetime(mapa["date"])
mapa["sugar_mix_pct"] = mapa["sugar_mix_pct"].astype(float)
mapa = mapa.set_index("date").sort_index()

print(f"MAPA:  {mapa.index[0].date()} → {mapa.index[-1].date()}  ({len(mapa)} informes)")

# ─── 3. Descargar SB=F para construir ratio diario ────────────────────────────
start = str(cepea.index[0].date())
print(f"\nDescargando SB=F desde {start}...")
sb = yf.download("SB=F", start=start, interval="1d",
                 progress=False, auto_adjust=True)
if sb is None or len(sb) < 50:
    print("ERROR: no se pudo descargar SB=F"); sys.exit(1)
sb.columns = [c[0] if isinstance(c, tuple) else c for c in sb.columns]
sb = sb[["Close"]].rename(columns={"Close": "ice"}).sort_index()
sb.index = pd.to_datetime(sb.index)

daily = cepea[["eth_clb"]].join(sb[["ice"]], how="inner").dropna()
daily["ratio"] = daily["eth_clb"] / daily["ice"]
print(f"Serie conjunta CEPEA+ICE: {len(daily)} días")

# ─── 4. Para cada fecha MAPA: media del ratio en las N semanas previas ────────
lookback_weeks_list = [1, 2, 4, 8]  # ventanas de lookback

records = []
for dt, row in mapa.iterrows():
    mix = row["sugar_mix_pct"]
    rec = {"date": dt, "sugar_mix_pct": mix}
    for w in lookback_weeks_list:
        window_start = dt - pd.Timedelta(weeks=w)
        sub = daily.loc[(daily.index >= window_start) & (daily.index < dt), "ratio"]
        rec[f"mean_ratio_{w}w"] = sub.mean() if len(sub) >= 3 else np.nan
    records.append(rec)

panel = pd.DataFrame(records).set_index("date").sort_index().dropna(
    subset=[f"mean_ratio_{w}w" for w in lookback_weeks_list], how="all"
)

print(f"\nPanel MAPA enriquecido: {len(panel)} observaciones")
print(f"  Rango sugar_mix_pct: {panel['sugar_mix_pct'].min():.1f}% – {panel['sugar_mix_pct'].max():.1f}%")

# ─── 5. Cross-correlación: mean_ratio_Nw_before vs sugar_mix_pct a lag M ─────
print(f"\n{'='*70}")
print(f"  PEARSON: mean_ratio_Nw_previas  vs  sugar_mix_pct a lag M informes")
print(f"  (correlación esperada NEGATIVA: ratio alto → menos azúcar)")
print(f"{'='*70}")
print(f"  {'Lookback':>10}  {'Lag M':>6}  {'r':>8}  {'p':>8}  {'N':>5}  Sig")
print(f"  {'─'*52}")

best = {"abs_r": 0.0, "lookback": 0, "lag": 0, "r": 0.0, "p": 1.0}

for w in lookback_weeks_list:
    col = f"mean_ratio_{w}w"
    for lag in [0, 1, 2, 3]:
        if lag == 0:
            x = panel[col].dropna()
            y = panel["sugar_mix_pct"].reindex(x.index).dropna()
            x = x.reindex(y.index)
        else:
            # lag > 0: x = ratio at time T, y = sugar_mix at T+lag reports
            x = panel[col].iloc[:-lag]
            y = panel["sugar_mix_pct"].iloc[lag:]
            x, y = x.align(y, join="inner")
            x = x.dropna(); y = y.reindex(x.index).dropna(); x = x.reindex(y.index)

        if len(x) < 8:
            continue
        r, p = stats.pearsonr(x, y)
        sig = "**" if p < 0.05 else ("*" if p < 0.10 else "")
        lag_s = f"+{lag} inf" if lag > 0 else "contemp"
        print(f"  {w:>7}w prev  {lag_s:>6}  {r:>+8.4f}  {p:>8.4f}  {len(x):>5}  {sig}")
        if abs(r) > best["abs_r"]:
            best = {"abs_r": abs(r), "lookback": w, "lag": lag, "r": r, "p": p}
    print()

print(f"  Mejor combinación: {best['lookback']}w lookback / lag={best['lag']} informes")
print(f"  r = {best['r']:+.4f}  p = {best['p']:.4f}")

if best["lag"] == 0:
    print("  → Señal contemporánea: CEPEA y MAPA se mueven juntos (ingenios ya decidieron)")
elif best["r"] < 0:
    print(f"  → CEPEA lidera MAPA por {best['lag']} informes (~{best['lag']*2}sem): "
          f"ratio alto predice menos azúcar en MAPA  ✓ señal predictiva")
else:
    print(f"  → Correlación positiva inesperada al lag {best['lag']}: revisar datos")

# ─── 6. Análisis por régimen de ratio ─────────────────────────────────────────
print(f"\n{'='*70}")
print(f"  SUGAR MIX % MEDIO SEGÚN RÉGIMEN DE RATIO (lookback 4w)")
print(f"{'='*70}")
col = "mean_ratio_4w"
if col in panel.columns:
    p_sub = panel[[col, "sugar_mix_pct"]].dropna()
    p_sub["regime"] = "neutral"
    p_sub.loc[p_sub[col] > 1.028, "regime"] = "LONG (ratio>P75)"
    p_sub.loc[p_sub[col] < 0.788, "regime"] = "SHORT (ratio<P25)"

    print(f"  {'Régimen':<20}  {'N':>5}  {'Mix% medio':>11}  {'Mix% mediana':>13}")
    for regime in ["LONG (ratio>P75)", "neutral", "SHORT (ratio<P25)"]:
        g = p_sub[p_sub["regime"] == regime]["sugar_mix_pct"]
        if len(g) == 0:
            continue
        print(f"  {regime:<20}  {len(g):>5}  {g.mean():>11.1f}%  {g.median():>13.1f}%")

# ─── 7. Últimos 8 informes MAPA ───────────────────────────────────────────────
print(f"\n{'='*70}")
print(f"  ÚLTIMOS 8 INFORMES MAPA")
print(f"{'='*70}")
print(f"  {'Fecha':12}  {'Mix%':>6}  {'Ratio1w':>8}  {'Ratio2w':>8}  {'Ratio4w':>8}")
print(f"  {'─'*50}")
for dt, row in panel.tail(8).iterrows():
    print(f"  {str(dt.date()):12}  {row['sugar_mix_pct']:>6.1f}%"
          f"  {row.get('mean_ratio_1w', float('nan')):>8.4f}"
          f"  {row.get('mean_ratio_2w', float('nan')):>8.4f}"
          f"  {row.get('mean_ratio_4w', float('nan')):>8.4f}")

print()
