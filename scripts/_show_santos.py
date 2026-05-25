"""Script temporal para mostrar la sección Santos limpia."""
import sys, os
from datetime import datetime
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal
from ingestion.santos_port import get_latest_snapshot
from services.santos_signal import compute_santos_signal

session = SessionLocal()
snap    = get_latest_snapshot(session)
signal  = compute_santos_signal(session, snap)
session.close()

print()
print("  -- A5 PUERTO DE SANTOS (cola exportacion azucar) --")
print("  Snapshot:", snap.get("snapshot_date"))
print()
print("  %-16s  %-8s  %-12s" % ("Pagina", "Barcos", "Tonelaje"))
print("  " + "-" * 44)
print("  %-16s  %-8d  %-12s" % ("Expected(Long)", snap["n_expected"], str(snap["tonnage_expected"]) + " t"))
print("  %-16s  %-8d"        % ("Scheduled",      snap["n_scheduled"]))
print("  %-16s  %-8d  %-12s" % ("Berthed",         snap["n_berthed"],  str(snap["tonnage_berthed"]) + " t"))
print("  " + "-" * 44)
total_ships = snap["n_expected"] + snap["n_scheduled"] + snap["n_berthed"]
total_t     = snap["tonnage_expected"] + snap["tonnage_berthed"]
print("  %-16s  %-8d  %-12s" % ("TOTAL", total_ships, str(total_t) + " t"))

print()
print("  Cargando ahora (berthed):")
for s in snap.get("berthed", []):
    qty = ("  %d t" % s["load_qty_t"]) if s.get("load_qty_t") else ""
    print("    %-32s  %-22s%s" % (s["ship"][:32], s["terminal"][:22], qty))

print()
print("  Proximas llegadas ACUCAR Long (exportacion):")
_today   = datetime.today().date()
exp_long = sorted(
    [
        s for s in snap.get("expected", [])
        if (s.get("nav_type") or "").strip() == "Long"
        and (s.get("arrival_dt") is None or
             (hasattr(s["arrival_dt"], "date") and s["arrival_dt"].date() >= _today))
    ],
    key=lambda x: x.get("arrival_dt") or datetime.max,
)
for s in exp_long[:12]:
    arr = s["arrival_dt"].strftime("%d/%m  %H:%M") if s.get("arrival_dt") else "?"
    qty = ("  %d t" % s["weight_t"]) if s.get("weight_t") else ""
    print("    %-32s  %s  %-22s%s" % (s["ship"][:32], arr, s["terminal"][:22], qty))

print()
if signal:
    sig  = signal.get("signal_a5", 0)
    bias = signal.get("bias", "NEUTRAL")
    z    = signal.get("z_combined", 0)
    bar_pos = int((sig + 1) / 2 * 20)
    bar_str = "[" + "-" * max(0, bar_pos - 1) + "|" + "-" * max(0, 19 - bar_pos) + "]"
    print("  A5 signal : %+.2f  [%s]" % (sig, bias))
    print("  Escala    : -1.0 %s +1.0" % bar_str)
    print("  Z-score   : %.2f  (util a partir de 5 dias de snapshots)" % z)
    print()
    print("  NOTA: Santos es indicador TACTICO (semanal), mismo horizonte")
    print("  que COT y MAPA. Alimenta sesgo Capa 1, NO trigger de Capa 2.")
