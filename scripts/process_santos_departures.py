"""
Crea la tabla santos_departures y procesa todos los snapshots históricos.
Ejecutar una vez; idempotente en ejecuciones posteriores.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal, engine
from models.market_data import Base
from services.santos_exports import process_all_historical

Base.metadata.create_all(engine)
print("Tabla santos_departures: OK")

session = SessionLocal()
try:
    n = process_all_historical(session)
    print("Partidas históricas registradas: %d" % n)

    # Resumen
    from sqlalchemy import text
    rows = session.execute(text("""
        SELECT departed_date, ship_name, terminal, sugar_tonnes
        FROM santos_departures
        ORDER BY departed_date DESC
        LIMIT 20
    """)).fetchall()
    if rows:
        print("\nÚltimas partidas registradas:")
        for r in rows:
            qty = ("%d t" % r[3]) if r[3] else "s/tonelaje"
            print("  %s  %-32s  %-25s  %s" % (r[0], r[1][:32], (r[2] or "")[:25], qty))
    else:
        print("\nSin partidas registradas (posiblemente no hay snapshots consecutivos aún).")
finally:
    session.close()
