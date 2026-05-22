import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import create_database_if_not_exists, create_all_tables, engine
from sqlalchemy import text

if __name__ == "__main__":
    print("SGT Trading - Inicializacion de base de datos")
    create_database_if_not_exists()
    create_all_tables()
    with engine.connect() as conn:
        tables = [r[0] for r in conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ))]
    print(f"Tablas ({len(tables)}): {', '.join(tables)}")
