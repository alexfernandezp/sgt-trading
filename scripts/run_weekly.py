"""
Pipeline semanal SGT — fuentes de actualización ~semanal.
Ejecutar lunes 06:30 (Sentinel-2 tiene latencia 5-7 días, el lunes captura semana anterior).

Fuentes:
  [1] NDVI Sentinel-2 GEE  (cinturón azucarero SP + India + Tailandia)
  [2] GEE crops metrics  (leer cache DB — anomalías LST/NDWI/SPI)

Log: logs/weekly_YYYYMMDD.log
"""
import sys, os, logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(LOG_DIR, f"weekly_{datetime.now().strftime('%Y%m%d')}.log"),
            encoding="utf-8"
        ),
    ]
)
logger = logging.getLogger("weekly")


def run():
    logger.info("=" * 60)
    logger.info("SGT Weekly Pipeline  %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    logger.info("=" * 60)

    from database import SessionLocal
    from ingestion.ndvi_gee import fetch_ndvi

    with SessionLocal() as session:

        logger.info("[1/2] NDVI — Sentinel-2 GEE...")
        try:
            ndvi = fetch_ndvi(session, weeks_back=2)
            if ndvi.get("latest_ndvi") is not None:
                logger.info("  NDVI=%.4f  fecha=%s  filas=%d",
                            ndvi["latest_ndvi"], ndvi["latest_date"], ndvi["rows_upserted"])
            else:
                logger.info("  Sin datos nuevos NDVI (normal si API GEE sin actualizar)")
            for err in ndvi.get("errors", []):
                logger.warning("  %s", err)
        except Exception as e:
            logger.warning("NDVI GEE: %s", e)

        logger.info("[2/2] GEE crops metrics (cache DB)...")
        try:
            from ingestion.gee_crops import get_latest_gee_metrics
            gee = get_latest_gee_metrics(session)
            if gee:
                for poi_id, metrics in gee.items():
                    parts = []
                    for m, d in metrics.items():
                        v = d.get("value")
                        z = d.get("z_score")
                        anom = "⚠" if d.get("anomaly") else ""
                        if v is not None:
                            parts.append(f"{m}={v:.3f}(z={z:+.2f}{anom})" if z else f"{m}={v:.3f}")
                    logger.info("  %-25s %s", poi_id, "  ".join(parts))
            else:
                logger.info("  Sin datos GEE — ejecutar: py scripts/run_gee_crops.py")
        except Exception as e:
            logger.warning("GEE crops: %s", e)

    logger.info("Weekly pipeline OK")


if __name__ == "__main__":
    run()
