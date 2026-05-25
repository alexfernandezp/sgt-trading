"""
Descarga el ship tracker de Puerto de Santos y almacena snapshot en DB.
Ejecutar a diario (incluido en daily_pipeline.py).

Uso: py scripts/run_santos.py
"""
import sys, os, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from database import SessionLocal
from ingestion.santos_port import fetch_santos_port

def run():
    session = SessionLocal()
    try:
        result = fetch_santos_port(session)
        print("\n=== Puerto de Santos — barcos ACUCAR ===")
        print("  Expected (Long): %d barcos  %d t" % (result["n_expected"], result["tonnage_expected"]))
        print("  Scheduled      : %d barcos" % result["n_scheduled"])
        print("  Berthed        : %d barcos  %d t cargando" % (result["n_berthed"], result["tonnage_berthed"]))
        if result["errors"]:
            print("  Errores: %s" % "; ".join(result["errors"]))

        print("\n  -- Berthed ships (cargando ahora) --")
        for s in result["berthed"]:
            qty = ("  %d t" % s["load_qty_t"]) if s.get("load_qty_t") else ""
            print("    %-32s  %-20s%s" % (s["ship"][:32], s["terminal"][:20], qty))

        print("\n  -- Expected ACUCAR Long (proximas llegadas) --")
        exp_long = sorted(
            [s for s in result["expected"] if (s.get("nav_type") or "").strip() == "Long"],
            key=lambda x: x.get("arrival_dt") or "9999"
        )
        for s in exp_long[:15]:
            arr = s["arrival_dt"].strftime("%d/%m %H:%M") if s.get("arrival_dt") else "?"
            qty = ("  %d t" % s["weight_t"]) if s.get("weight_t") else ""
            print("    %-32s  %s  %-20s%s" % (s["ship"][:32], arr, s["terminal"][:20], qty))
    finally:
        session.close()

if __name__ == "__main__":
    run()
