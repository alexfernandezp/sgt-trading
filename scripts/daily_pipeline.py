"""
Pipeline diario SGT Trading. Ejecutar cada manana antes de operar.
Uso: py scripts/daily_pipeline.py
"""
import sys, os, logging
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s - %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("daily_pipeline")

from database import SessionLocal
from ingestion.prices import fetch_prices
from ingestion.cot import fetch_cot
from ingestion.intraday import fetch_intraday


def run():
    start = datetime.now()
    logger.info("=" * 55)
    logger.info("SGT Trading - Pipeline diario")
    logger.info(f"Fecha: {start.strftime('%Y-%m-%d %H:%M')}")
    logger.info("=" * 55)

    session = SessionLocal()
    try:
        logger.info("Precios diarios (30 dias)...")
        for instr, count in fetch_prices(session, days_back=30).items():
            logger.info(f"  {instr:<15} {count} filas" if count else f"  {instr:<15} sin datos")

        logger.info("COT (8 semanas)...")
        logger.info(f"  SUGAR_NO11_ICE  {fetch_cot(session, limit=8)} filas")

        logger.info("Barras intraday (5m, 30m, 1h, 4h, 1wk, 1mo)...")
        for instr, ivs in fetch_intraday(session).items():
            for iv, n in ivs.items():
                logger.info(f"  {instr:<12} {iv:<5} {n} filas")

    except Exception as exc:
        logger.error(f"Error: {exc}", exc_info=True)
        session.rollback()
        raise
    finally:
        session.close()

    logger.info(f"Pipeline completado en {(datetime.now()-start).total_seconds():.1f}s")


if __name__ == "__main__":
    run()
