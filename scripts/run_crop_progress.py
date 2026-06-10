"""
Brazil Crop Progress runner — ejecutar manualmente cuando UNICA publica (~cada 15 días).

Workflow:
  1. Descarga el PDF quinzenal más reciente de UNICA y lo parsea.
  2. Persiste los datos en unica_biweekly (region=CS, source=pdf_unica).
  3. Calcula señales z-score / percentil vs baseline histórico 2012-2024.
  4. Imprime reporte en consola y guarda en logs/crop_progress_YYYYMMDD.log.

Uso:
    python scripts/run_crop_progress.py
    python scripts/run_crop_progress.py --no-fetch   # solo recalcula señales sin bajar PDF nuevo
"""
import sys, os, logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
_today = datetime.now().strftime("%Y%m%d")

LOG_FILE = os.path.join(LOG_DIR, f"crop_progress_{_today}.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("crop_progress")


def run():
    no_fetch = "--no-fetch" in sys.argv

    from database import SessionLocal
    from services.brazil_crop_progress import compute_crop_progress, format_crop_progress_report

    t0 = datetime.now()
    logger.info("=" * 60)
    logger.info("Brazil Crop Progress  %s", t0.strftime("%Y-%m-%d %H:%M"))
    logger.info("=" * 60)

    with SessionLocal() as session:

        # 1. Fetch y persist nuevo PDF UNICA (si no se omite)
        if not no_fetch:
            logger.info("[1] Descargando último reporte quinzenal UNICA...")
            try:
                from ingestion.unica import get_latest_unica, save_unica_to_db
                data = get_latest_unica()
                if data:
                    saved = save_unica_to_db(session, data)
                    logger.info(
                        "  UNICA idM=%s  safra=%s  Q%d/%d  acucar=%.3f Mt  proyeccion=%s Mt  saved=%s",
                        data.get("idm_source", "?"),
                        data.get("safra", "?"),
                        data.get("quinzena_num", 0),
                        data.get("ref_month", 0),
                        data.get("sugar_cumulative_mt", 0),
                        data.get("projected_full_year_mt", "N/D"),
                        saved,
                    )
                else:
                    logger.warning("  UNICA: sin datos disponibles")
            except Exception as e:
                logger.error("  UNICA fetch/save: %s", e)
        else:
            logger.info("[1] Skipping UNICA fetch (--no-fetch)")

        # 2. Calcular señales crop progress
        logger.info("[2] Calculando señales Brazil Crop Progress...")
        try:
            signals = compute_crop_progress(session, region="CS")
            report = format_crop_progress_report(signals)
            logger.info("\n%s", report)

            # Resumen clave
            if not signals.get("error"):
                logger.info(
                    "SIGNALS: crushing_z=%s  sugar_mix_z=%s  atr_delta=%s  yoy_cane=%s%%  proj=%s Mt",
                    signals.get("crushing_pace_z"),
                    signals.get("sugar_mix_pct_z"),
                    signals.get("atr_delta"),
                    signals.get("yoy_cane_pct"),
                    signals.get("projected_sugar_mt"),
                )
        except Exception as e:
            logger.error("  compute_crop_progress: %s", e)

    elapsed = (datetime.now() - t0).total_seconds()
    logger.info("Crop Progress completado en %.1fs  →  %s", elapsed, LOG_FILE)


if __name__ == "__main__":
    run()
