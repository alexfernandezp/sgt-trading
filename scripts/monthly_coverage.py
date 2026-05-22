import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
from database import SessionLocal
from sqlalchemy import text

session = SessionLocal()
result = session.execute(text(
    "SELECT TO_CHAR(datetime, 'YYYY-MM') AS mes, COUNT(*) AS n "
    "FROM price_bars "
    "WHERE instrument = 'SB_CONT' AND interval = '1m' "
    "AND datetime >= '2019-01-01' AND datetime < '2026-01-01' "
    "GROUP BY 1 ORDER BY 1"
)).fetchall()

print("Barras 1m por mes (SB_CONT continuo):")
print("%-10s  %s" % ("MES", "BARRAS"))
print("-" * 25)
for row in result:
    marker = " <<< HUECO" if int(row[1]) < 100 else ""
    print("  %-10s  %5d%s" % (row[0], row[1], marker))
session.close()
