"""Diagnostico Santos port — DB state + read paths."""
import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal
from sqlalchemy import text
from ingestion.santos_port import get_latest_snapshot

s = SessionLocal()

print("=== Santos port snapshot DB state ===")
row = s.execute(text("""
    SELECT COUNT(*), MAX(snapshot_date), MIN(snapshot_date),
           COUNT(DISTINCT snapshot_date)
    FROM santos_port_snapshot
""")).fetchone()
print(f"  rows total      : {row[0]}")
print(f"  max snapshot    : {row[1]}")
print(f"  min snapshot    : {row[2]}")
print(f"  distinct dates  : {row[3]}")

print("\n=== Snapshots por dia (ultimas 10) ===")
rows = s.execute(text("""
    SELECT snapshot_date, page, COUNT(*) as ships, SUM(weight_t) as total_w,
           SUM(load_qty_t) as total_load
    FROM santos_port_snapshot
    GROUP BY snapshot_date, page
    ORDER BY snapshot_date DESC, page
    LIMIT 30
""")).fetchall()
for r in rows:
    print(f"  {r[0]} page={r[1]:<10} ships={r[2]:>4} weight={r[3] or 0:>10} load={r[4] or 0:>10}")

print("\n=== get_latest_snapshot() output (default reference=today) ===")
out = get_latest_snapshot(s)
if out is None:
    print("  RETURNED None — freshness gate o no data")
else:
    print(f"  snapshot_date    : {out.get('snapshot_date')}")
    print(f"  n_expected       : {out.get('n_expected')}")
    print(f"  n_scheduled      : {out.get('n_scheduled')}")
    print(f"  n_berthed        : {out.get('n_berthed')}")
    print(f"  tonnage_expected : {out.get('tonnage_expected')}")
    print(f"  tonnage_berthed  : {out.get('tonnage_berthed')}")
    print(f"  errors           : {out.get('errors')}")

from datetime import date
print(f"\n=== today = {date.today()} ===")
print(f"  freshness threshold: 2d (BUSINESS_LOGIC §4)")
