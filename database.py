from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL, ADMIN_DATABASE_URL, DB_NAME
from models import Base

engine       = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def create_database_if_not_exists():
    admin_engine = create_engine(ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": DB_NAME},
        ).fetchone()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{DB_NAME}"'))
            print(f"Base de datos '{DB_NAME}' creada.")
        else:
            print(f"Base de datos '{DB_NAME}' ya existe.")
    admin_engine.dispose()


def create_all_tables():
    Base.metadata.create_all(bind=engine)
    print("Tablas creadas correctamente.")
