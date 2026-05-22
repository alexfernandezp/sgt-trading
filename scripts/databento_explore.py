"""
Explora datasets adicionales de Databento relevantes para Sugar No.11:
- IFEU.IMPACT: White Sugar No.5 London (SW)
- GLBX.MDP3: BRL/USD futuros (6L), Brent (BB), Ethanol (EH)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import databento as db
from config import DATABENTO_API_KEY

START = "2019-01-01"
END   = "2026-05-19"

client = db.Historical(DATABENTO_API_KEY)

CANDIDATES = [
    # (dataset, symbol, stype, descripcion)
    ("IFEU.IMPACT", "SF.c.0",  "continuous", "White Sugar No.5 London continuo"),
    ("IFEU.IMPACT", "SF.FUT",  "parent",     "White Sugar No.5 parent (todos contratos)"),
    ("GLBX.MDP3",  "6L.c.0",  "continuous", "BRL/USD futuros CME continuo"),
    ("GLBX.MDP3",  "BZ.c.0",  "continuous", "Brent crude CME continuo"),
    ("GLBX.MDP3",  "BB.c.0",  "continuous", "Brent crude ICE via CME continuo"),
    ("GLBX.MDP3",  "CL.c.0",  "continuous", "WTI crude CME continuo"),
    ("GLBX.MDP3",  "EH.c.0",  "continuous", "Ethanol CME continuo"),
]

print("%-12s  %-12s  %-40s  %10s  %10s" % ("DATASET", "SYMBOL", "DESCRIPCION", "COSTE_1d", "COSTE_1h"))
print("-" * 90)

for dataset, symbol, stype, desc in CANDIDATES:
    costs = {}
    for schema in ("ohlcv-1d", "ohlcv-1h"):
        try:
            c = client.metadata.get_cost(
                dataset=dataset,
                symbols=[symbol],
                stype_in=stype,
                schema=schema,
                start=START,
                end=END,
            )
            costs[schema] = "$%.4f" % c
        except Exception as e:
            err = str(e)
            if "no_data_found" in err or "symbol" in err.lower():
                costs[schema] = "N/A"
            elif "422" in err:
                costs[schema] = "N/A"
            else:
                costs[schema] = "ERR"

    print("%-12s  %-12s  %-40s  %10s  %10s" % (
        dataset, symbol, desc, costs.get("ohlcv-1d","?"), costs.get("ohlcv-1h","?")))
