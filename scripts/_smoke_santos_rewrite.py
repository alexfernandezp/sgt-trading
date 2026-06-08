"""Smoke test del santos_port reescrito — corre fetch + verifica DB."""
import sys
import logging
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(name)s - %(message)s")

from database import SessionLocal
from sqlalchemy import text
from ingestion.santos_port import fetch_santos_port, get_latest_snapshot

s = SessionLocal()

print("\n=== fetch_santos_port() ===")
out = fetch_santos_port(s)
print(f"  errors          : {out['errors']}")
print(f"  n_scheduled     : {out['n_scheduled']}")
print(f"  n_berthed       : {out['n_berthed']}")
print(f"  tonnage_berthed : {out['tonnage_berthed']:,}")

print(f"\n  Berthed ships:")
for sh in out["berthed"]:
    print(f"    {sh['ship']:<25} {sh['cargo']:<15} "
          f"load={sh.get('load_qty_t') or 0:>7}  desc={sh.get('desc_qty_t') or 0:>7}  "
          f"terminal={sh['terminal'][:30]}")

print(f"\n  Scheduled (primeros 5):")
for sh in out["scheduled"][:5]:
    print(f"    {sh['ship']:<25} arr={sh['arrival_dt']}  ev={sh.get('evento')}  "
          f"local={sh['terminal'][:20]}")

print("\n=== DB state after write ===")
row = s.execute(text("""
    SELECT COUNT(*), MAX(snapshot_date)
    FROM santos_port_snapshot
""")).fetchone()
print(f"  rows total      : {row[0]}")
print(f"  max snapshot    : {row[1]}")

rows = s.execute(text("""
    SELECT snapshot_date, page, COUNT(*) ships,
           SUM(load_qty_t) as load_total
    FROM santos_port_snapshot
    GROUP BY snapshot_date, page
    ORDER BY snapshot_date DESC, page
    LIMIT 10
""")).fetchall()
print("\n  Latest by day/page:")
for r in rows:
    print(f"    {r[0]} {r[1]:<10} ships={r[2]:>3}  load={r[3] or 0:>10}")

print("\n=== get_latest_snapshot() ===")
snap = get_latest_snapshot(s)
if snap is None:
    print("  None — freshness gate o no data")
else:
    print(f"  snapshot_date    : {snap['snapshot_date']}")
    print(f"  n_expected       : {snap['n_expected']}")
    print(f"  n_scheduled      : {snap['n_scheduled']}")
    print(f"  n_berthed        : {snap['n_berthed']}")
    print(f"  tonnage_berthed  : {snap['tonnage_berthed']:,}")
