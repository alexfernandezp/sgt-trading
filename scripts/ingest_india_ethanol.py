"""
Ingesta manual de datos de desvío de azúcar a etanol en India.

Uso:
  py scripts/ingest_india_ethanol.py --season 2024 --mt 3.4 --type actual
  py scripts/ingest_india_ethanol.py --season 2025 --lmt 31.0 --type estimate
  py scripts/ingest_india_ethanol.py --seed
  py scripts/ingest_india_ethanol.py --list

Fuentes para rellenar datos:
  - ISMA press releases: buscar "India ethanol diversion sugar equivalent YYYY"
  - MoPNG/PIB: aprobación precios ESY (CCEA)
  - ChiniMandi: estadísticas anuales
"""
import sys
import os
import argparse
import logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ingest_india_ethanol")

LAKH_TO_MT = 0.1

# Datos históricos confirmados para --seed
_SEED = [
    {"season_year": 2021, "diversion_mt": 2.0,  "data_type": "actual",   "source": "ISMA 2020-21 season-end"},
    {"season_year": 2022, "diversion_mt": 3.4,  "data_type": "actual",   "source": "ISMA 2021-22 season-end"},
    {"season_year": 2023, "diversion_mt": 3.8,  "data_type": "actual",   "source": "ISMA 2022-23 (cap applied Dec23)"},
    {"season_year": 2024, "diversion_mt": 3.4,  "data_type": "actual",   "source": "ISMA 2024-25 season-end"},
    {"season_year": 2025, "diversion_mt": 3.1,  "data_type": "estimate", "source": "ISMA 2025-26 3rd advance Feb26"},
]


def _upsert(session, season_year: int, diversion_mt: float, data_type: str,
            source: str, esy_target_lmt=None, sugar_route_pct=None, notes=None):
    from models.market_data import IndiaEthanolDiversion
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    diversion_lmt = round(diversion_mt * 10, 2)
    esy_year = season_year   # ESY Nov season_year → Oct season_year+1

    stmt = (
        pg_insert(IndiaEthanolDiversion)
        .values(
            season_year     = season_year,
            esy_year        = esy_year,
            diversion_lmt   = diversion_lmt,
            diversion_mt    = diversion_mt,
            esy_target_lmt  = esy_target_lmt,
            sugar_route_pct = sugar_route_pct,
            data_type       = data_type,
            source          = source,
            notes           = notes,
            updated_at      = datetime.utcnow(),
        )
        .on_conflict_do_update(
            constraint = "uq_india_ethanol_season",
            set_ = {
                "diversion_lmt":   diversion_lmt,
                "diversion_mt":    diversion_mt,
                "data_type":       data_type,
                "source":          source,
                "notes":           notes,
                "updated_at":      datetime.utcnow(),
            },
        )
    )
    session.execute(stmt)
    session.commit()


def cmd_list(session):
    from models.market_data import IndiaEthanolDiversion
    rows = session.query(IndiaEthanolDiversion).order_by(IndiaEthanolDiversion.season_year).all()
    if not rows:
        print("\n  (No hay datos en india_ethanol_diversion)\n")
        return
    header = f"{'Season':8}  {'Diversion Mt':>12}  {'LMT':>6}  {'Type':10}  Fuente"
    print("\n  " + header)
    print("  " + "-" * len(header))
    for r in rows:
        print(f"  {r.season_year:8}  {float(r.diversion_mt):>12.3f}  "
              f"{float(r.diversion_lmt):>6.1f}  {r.data_type:10}  {r.source or ''}")
    print(f"\n  Total: {len(rows)} registros\n")


def cmd_seed(session):
    logger.info("Cargando seed data de desvío etanol India...")
    for entry in _SEED:
        _upsert(session, **entry)
        logger.info("  season %d: %.2f Mt (%s) — %s",
                    entry["season_year"], entry["diversion_mt"],
                    entry["data_type"], entry["source"])
    logger.info("Seed completado.")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true")
    mode.add_argument("--seed", action="store_true")

    parser.add_argument("--season", type=int, metavar="YYYY")
    parser.add_argument("--mt",  type=float, help="Desvío en Mt azúcar equiv.")
    parser.add_argument("--lmt", type=float, help="Desvío en LMT (alternativa a --mt)")
    parser.add_argument("--type", choices=["actual", "estimate", "formula"], default="actual")
    parser.add_argument("--source", type=str, default="manual_cli")
    parser.add_argument("--target-lmt", type=float, dest="target_lmt")
    parser.add_argument("--sugar-pct", type=float, dest="sugar_pct")
    parser.add_argument("--notes", type=str)

    args = parser.parse_args()

    from database import SessionLocal, create_all_tables
    create_all_tables()
    session = SessionLocal()

    try:
        if args.list:
            cmd_list(session)
            return
        if args.seed:
            cmd_seed(session)
            return

        if not args.season:
            parser.error("--season requerido (o usa --list / --seed)")
        if args.mt is None and args.lmt is None:
            parser.error("Se requiere --mt o --lmt")

        diversion_mt = args.mt if args.mt is not None else args.lmt * LAKH_TO_MT
        _upsert(session, args.season, diversion_mt, args.type, args.source,
                esy_target_lmt=args.target_lmt, sugar_route_pct=args.sugar_pct,
                notes=args.notes)
        logger.info("Guardado: season %d → %.3f Mt (%s)", args.season, diversion_mt, args.type)

    except Exception as e:
        logger.error("Error: %s", e, exc_info=True)
        session.rollback()
        sys.exit(1)
    finally:
        session.close()


if __name__ == "__main__":
    main()
