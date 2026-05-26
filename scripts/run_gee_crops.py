"""
GEE Crop Intelligence — script de actualización.

Ejecutar 2x por semana (Sentinel-2 revisita ~5 días).
NO incluir en daily_pipeline.py — puede tardar 3-8 minutos.

Uso:
  py scripts/run_gee_crops.py
  py scripts/run_gee_crops.py --weeks 4    # backfill 4 semanas

Métricas calculadas por POI (config/gee_pois.json):
  ndvi  — ritmo cosecha (Sentinel-2 SR)
  ndwi  — agua en vegetación (Sentinel-2 SR)
  lst   — temperatura superficial °C (MODIS MOD11A2)
  spi90 — SPI-90 precipitación acumulada (CHIRPS)
"""
import sys, os, logging, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_gee_crops")


def run(weeks_back: int = 2):
    from datetime import datetime
    start = datetime.now()
    logger.info("=" * 60)
    logger.info("SGT Trading — GEE Crop Intelligence")
    logger.info("POIs: BR São Paulo | TH Kanchanaburi | IN UP+Maharashtra")
    logger.info("=" * 60)

    from database import SessionLocal
    from ingestion.gee_crops import fetch_gee_crops

    session = SessionLocal()
    try:
        result = fetch_gee_crops(session, weeks_back=weeks_back)
        logger.info("Métricas almacenadas: %d", result["rows_upserted"])
        logger.info("POIs procesados: %s", ", ".join(result["pois_processed"]))
        for err in result.get("errors", []):
            logger.warning("Error: %s", err)
    except Exception as e:
        logger.error("Error fatal: %s", e, exc_info=True)
        session.rollback()
        raise
    finally:
        session.close()

    elapsed = (datetime.now() - start).total_seconds()
    logger.info("Completado en %.1fs", elapsed)
    logger.info("Señales disponibles en score_today.py:")
    logger.info("  Harvest Pace BR+TH+IN | Crop Stress | Rainfall SPI-90")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--weeks", type=int, default=2,
                        help="Semanas hacia atrás a calcular (default: 2)")
    args = parser.parse_args()
    run(weeks_back=args.weeks)
