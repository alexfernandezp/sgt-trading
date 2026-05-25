import sys, os
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import numpy as np
from sqlalchemy import text
from database import SessionLocal
import yfinance as yf

ATR_FACTOR = 1.4966; LBS = 2204.62

session = SessionLocal()
rows = session.execute(text(
    "SELECT price_date, price_usd FROM cepea_prices "
    "WHERE series_name='hydrous_paulinia_usd_m3' ORDER BY price_date"
)).fetchall()
session.close()

cepea = pd.DataFrame(rows, columns=["date","hydrous"])
cepea["date"]    = pd.to_datetime(cepea["date"])
cepea["hydrous"] = cepea["hydrous"].astype(float)
cepea = cepea.set_index("date")
cepea["eth_clb"] = cepea["hydrous"] * 100 / (ATR_FACTOR * LBS)

sb = yf.download("SB=F", start="2010-01-01", interval="1d", progress=False, auto_adjust=True)
sb.columns = [c[0] if isinstance(c, tuple) else c for c in sb.columns]
sb = sb[["Close"]].rename(columns={"Close": "ice"})

df = cepea[["eth_clb"]].join(sb[["ice"]], how="inner").dropna()
df["ratio"]  = df["eth_clb"] / df["ice"]
df["spread"] = df["eth_clb"] - df["ice"]

print("--- DISTRIBUCIÓN DEL RATIO ethanol_c_lb / ICE_c_lb ---")
pcts = [5, 10, 15, 20, 25, 30, 33, 40, 50, 60, 67, 70, 75, 80, 85, 90, 95]
for p in pcts:
    v = float(np.percentile(df["ratio"], p))
    print("  P%2d: %.4f" % (p, v))

print()
print("--- DISTRIBUCIÓN DEL SPREAD (c/lb) ---")
for p in pcts:
    v = float(np.percentile(df["spread"], p))
    print("  P%2d: %+.3f c/lb" % (p, v))

print()
print("  Mean ratio : %.4f" % df["ratio"].mean())
print("  Std  ratio : %.4f" % df["ratio"].std())
print("  Mean spread: %+.3f" % df["spread"].mean())
print("  Std  spread: %.3f"  % df["spread"].std())
