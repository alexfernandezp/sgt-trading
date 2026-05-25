"""
Análisis de cross-correlación paridad etanol CEPEA vs SB11 continuo.

Calcula:
  1. Serie diaria ethanol_c_lb (CEPEA hidratado Paulínia en c/lb azúcar equiv)
  2. Serie diaria ICE No.11 continuo (SB=F yfinance)
  3. Spread = ethanol_c_lb - ice_c_lb
  4. Cross-correlación entre delta_spread (semanal) y retorno_SB11 a lags 0-8 semanas
  5. Estadísticas descriptivas y período de mayor correlación

Factor Consecana-SP: 1.4966 ton azúcar / m³ etanol hidratado
Conversión: ethanol_c_lb = hydrous_usd_m3 * 100 / (1.4966 * 2204.62)
"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
import yfinance as yf
from sqlalchemy import text
from database import SessionLocal

ATR_FACTOR  = 1.4966
LBS_PER_TON = 2204.62
C_PER_USD   = 100.0

print("=" * 70)
print("  ANÁLISIS PARIDAD ETANOL CEPEA vs ICE No.11")
print("=" * 70)

# ─── 1. Cargar CEPEA hydrous Paulínia desde DB ────────────────────────────
session = SessionLocal()
rows = session.execute(text("""
    SELECT price_date, price_usd
    FROM cepea_prices
    WHERE series_name = 'hydrous_paulinia_usd_m3'
    ORDER BY price_date
""")).fetchall()
session.close()

cepea = pd.DataFrame(rows, columns=["date", "hydrous_usd_m3"])
cepea["date"] = pd.to_datetime(cepea["date"])
cepea["hydrous_usd_m3"] = cepea["hydrous_usd_m3"].astype(float)
cepea = cepea.set_index("date").sort_index()
cepea["ethanol_c_lb"] = cepea["hydrous_usd_m3"] * C_PER_USD / (ATR_FACTOR * LBS_PER_TON)

print(f"\nCEPEA hidratado Paulínia: {cepea.index[0].date()} → {cepea.index[-1].date()}")
print(f"  Observaciones: {len(cepea)}")
print(f"  Rango ethanol_c_lb: {cepea['ethanol_c_lb'].min():.2f} – {cepea['ethanol_c_lb'].max():.2f} c/lb")
print(f"  Media: {cepea['ethanol_c_lb'].mean():.2f} c/lb")

# ─── 2. Descargar SB11 continuo (SB=F) desde yfinance ────────────────────
print("\nDescargando SB=F histórico...")
sb = yf.download("SB=F", start="2010-01-01", interval="1d",
                 progress=False, auto_adjust=True)
if sb is None or len(sb) < 100:
    print("ERROR: no se pudo descargar SB=F"); sys.exit(1)
sb.columns = [c[0] if isinstance(c, tuple) else c for c in sb.columns]
sb = sb[["Close"]].rename(columns={"Close": "ice_c_lb"})
sb.index = pd.to_datetime(sb.index)
sb = sb.sort_index().dropna()

print(f"SB=F: {sb.index[0].date()} → {sb.index[-1].date()}")
print(f"  Observaciones: {len(sb)}")
print(f"  Rango: {sb['ice_c_lb'].min():.2f} – {sb['ice_c_lb'].max():.2f} c/lb")

# ─── 3. Alinear y calcular spread ─────────────────────────────────────────
df = cepea[["ethanol_c_lb"]].join(sb[["ice_c_lb"]], how="inner")
df = df.dropna()
df["spread_c_lb"] = df["ethanol_c_lb"] - df["ice_c_lb"]
df["ratio"]       = df["ethanol_c_lb"] / df["ice_c_lb"]

print(f"\nSerie conjunta (días con ambos datos): {len(df)}")
print(f"  Período: {df.index[0].date()} → {df.index[-1].date()}")
print(f"\nEstadísticas del spread (ethanol_c_lb − ICE c/lb):")
print(f"  Media   : {df['spread_c_lb'].mean():+.3f} c/lb")
print(f"  Mediana : {df['spread_c_lb'].median():+.3f} c/lb")
print(f"  Std     : {df['spread_c_lb'].std():.3f} c/lb")
print(f"  Min     : {df['spread_c_lb'].min():+.3f} c/lb  ({df['spread_c_lb'].idxmin().date()})")
print(f"  Max     : {df['spread_c_lb'].max():+.3f} c/lb  ({df['spread_c_lb'].idxmax().date()})")
print(f"\nEstadísticas del ratio (ethanol/ICE):")
print(f"  Media   : {df['ratio'].mean():.4f}")
print(f"  % días ratio>1.05 (LONG signal): {(df['ratio']>1.05).mean()*100:.1f}%")
print(f"  % días ratio<0.95 (SHORT signal): {(df['ratio']<0.95).mean()*100:.1f}%")
print(f"  % días neutro [0.95-1.05]      : {((df['ratio']>=0.95)&(df['ratio']<=1.05)).mean()*100:.1f}%")

# ─── 4. Resample semanal y cross-correlación ─────────────────────────────
# Usar cierre del viernes para cada semana
wk = df.resample("W-FRI").last().dropna()
wk["d_spread"] = wk["spread_c_lb"].diff()   # cambio semanal en spread
wk["ret_sb"]   = wk["ice_c_lb"].pct_change() * 100  # retorno % semanal SB11
wk = wk.dropna()

print(f"\n{'='*70}")
print(f"  CROSS-CORRELACIÓN: Δ_spread_semanal  vs  Retorno_SB11 a N semanas")
print(f"  (+ correlación = spread sube → ICE sube N semanas después = LONG bias)")
print(f"{'='*70}")
print(f"  Lag   Corr     p-valor  Interpretación")
print(f"  {'─'*60}")

from scipy import stats

best_lag  = 0
best_corr = 0.0
for lag in range(0, 9):
    if lag == 0:
        x = wk["d_spread"]
        y = wk["ret_sb"]
    else:
        x = wk["d_spread"].iloc[:-lag]
        y = wk["ret_sb"].iloc[lag:]
    x, y = x.align(y, join="inner")
    if len(x) < 30:
        continue
    r, p = stats.pearsonr(x, y)
    sig   = "**" if p < 0.05 else ("*" if p < 0.10 else "  ")
    lag_s = "contemporáneo" if lag == 0 else f"+{lag}s adelante"
    print(f"  {lag:>3}s  {r:+.4f}  {p:.4f}   {sig}  {lag_s}")
    if abs(r) > abs(best_corr):
        best_corr = r
        best_lag  = lag

print(f"\n  Lag óptimo: {best_lag} semanas  (r={best_corr:+.4f})")
if best_lag == 0:
    print("  → El spread cotempráneo con SB11 (mercado lo incorpora rápido)")
elif best_corr > 0:
    print(f"  → Spread CEPEA lidera SB11 por {best_lag} semanas: LONG cuando etanol sube")
else:
    print(f"  → Spread CEPEA lidera SB11 por {best_lag} semanas en sentido INVERSO")

# ─── 5. Análisis por régimen ──────────────────────────────────────────────
print(f"\n{'='*70}")
print(f"  RETORNO MEDIO SB11 SEGÚN RÉGIMEN DE PARIDAD (semana siguiente)")
print(f"{'='*70}")

if best_lag >= 1:
    wk["signal"] = "neutral"
    wk.loc[wk["ratio"] > 1.05, "signal"] = "LONG"
    wk.loc[wk["ratio"] < 0.95, "signal"] = "SHORT"
    wk["fwd_ret"] = wk["ret_sb"].shift(-best_lag)
    grp = wk.dropna(subset=["fwd_ret"]).groupby("signal")["fwd_ret"]
    print(f"  (retorno SB11 a {best_lag} semanas vista según señal de paridad)")
    print(f"  {'Señal':<10}  {'N':>5}  {'Media %':>8}  {'Mediana %':>10}  {'% positivos':>12}")
    for sig in ["LONG", "neutral", "SHORT"]:
        if sig in grp.groups:
            g = grp.get_group(sig)
            print(f"  {sig:<10}  {len(g):>5}  {g.mean():>+8.2f}%  {g.median():>+10.2f}%  {(g>0).mean()*100:>11.1f}%")
else:
    print("  Lag=0: señal contemporánea, análisis predictivo no aplicable")

# ─── 6. Últimos 52 datos semanales ───────────────────────────────────────
print(f"\n{'='*70}")
print(f"  ÚLTIMAS 12 SEMANAS")
print(f"{'='*70}")
print(f"  {'Fecha':<12}  {'Etanol c/lb':>11}  {'ICE c/lb':>9}  {'Spread':>8}  {'Ratio':>7}  Señal")
print(f"  {'─'*62}")
for dt, row in wk.tail(12).iterrows():
    sig = "LONG   " if row["ratio"] > 1.05 else ("SHORT  " if row["ratio"] < 0.95 else "neutral")
    print(f"  {str(dt.date()):<12}  {row['ethanol_c_lb']:>11.4f}  {row['ice_c_lb']:>9.4f}  "
          f"{row['spread_c_lb']:>+8.4f}  {row['ratio']:>7.4f}  {sig}")

print()
