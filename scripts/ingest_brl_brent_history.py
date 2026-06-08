"""
One-shot: ingesta histórica de BRLUSD y BRENT (10 años) en price_history.
Uso: py scripts/ingest_brl_brent_history.py
"""
import sys, os, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from database import SessionLocal
from ingestion.prices import fetch_prices

TARGETS    = ["BRLUSD", "BRENT"]
DAYS_BACK  = 3_650   # ~10 años

session = SessionLocal()
try:
    results = fetch_prices(session, instruments=TARGETS, days_back=DAYS_BACK)
    for name, n in results.items():
        print(f"{name}: {n} filas insertadas/actualizadas")
finally:
    session.close()
