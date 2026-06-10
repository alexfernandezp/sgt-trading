"""
Pipeline DIARIO SGT Trading — solo fuentes de actualización diaria.

Llamado desde run_morning.py (07:00 L-V automatizado).
Puede ejecutarse también manualmente: py scripts/daily_pipeline.py

Fuentes incluidas (actualización diaria):
  - Precios diarios OHLCV + macro (SB_CONT, BRLUSD, Brent, WS, spreads)
  - Barras intraday históricas 1h/4h/1wk/1mo
  - CEPEA precios físicos etanol/azúcar Brasil
  - Santos port snapshot
  - Paranaguá port snapshot
  - INPE fires SP+PR
  - ERA5 clima BR
  - Signal logger forward returns

Fuentes EXCLUIDAS (están en sus propios scripts):
  - COT         → run_weekly_cot.py  (viernes 21:35)
  - NDVI GEE    → run_weekly.py      (lunes 06:30)
  - ONI / Comex / CONAB → run_monthly.py  (días 1 y 10)
  - USDA / MAPA → run_monthly.py     (días 8-15)
"""
import sys, os, logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("daily_pipeline")

from database import SessionLocal
from ingestion.prices import fetch_prices
from ingestion.intraday import fetch_intraday
from ingestion.santos_port import fetch_santos_port
from ingestion.cepea import fetch_cepea
from ingestion.climate_openmeteo import fetch_climate
from ingestion.inpe_fires import fetch_fires
from ingestion.paranagua_port import fetch_paranagua_port

_DAILY_INTERVALS = ["1h", "4h", "1wk", "1mo"]


def run():
    start = datetime.now()
    logger.info("=" * 55)
    logger.info("SGT Trading - Pipeline diario")
    logger.info("Fecha: %s", start.strftime("%Y-%m-%d %H:%M"))
    logger.info("=" * 55)

    with SessionLocal() as session:

        logger.info("Precios diarios (5 dias)...")
        try:
            for instr, count in fetch_prices(session, days_back=5).items():
                logger.info("  %-15s %s", instr, f"{count} filas" if count else "sin nuevos")
        except Exception as e:
            logger.error("fetch_prices: %s", e)

        logger.info("Barras históricas 1h/4h/1wk/1mo...")
        try:
            for instr, ivs in fetch_intraday(session, intervals=_DAILY_INTERVALS).items():
                for iv, n in ivs.items():
                    logger.info("  %-10s %-5s %d", instr, iv, n)
        except Exception as e:
            logger.error("fetch_intraday: %s", e)

        logger.info("CEPEA — precios físicos Brasil...")
        try:
            cepea = fetch_cepea(session)
            logger.info("  etanol=%d  azúcar=%d", cepea["ethanol_rows"], cepea["sugar_rows"])
        except Exception as e:
            logger.warning("CEPEA: %s", e)

        logger.info("Santos port...")
        try:
            s = fetch_santos_port(session)
            logger.info("  berthed=%d barcos  %d t", s["n_berthed"], s["tonnage_berthed"])
        except Exception as e:
            logger.warning("Santos: %s", e)

        logger.info("Paranaguá port...")
        try:
            p = fetch_paranagua_port(session)
            logger.info("  atracados=%d  programados=%d", p["n_atracados"], p["n_programados"])
        except Exception as e:
            logger.warning("Paranaguá: %s", e)

        logger.info("INPE fires SP+PR...")
        try:
            f = fetch_fires(session, days_back=7)
            logger.info("  filas=%d  último=%s", f["rows_upserted"], f["latest_date"])
        except Exception as e:
            logger.warning("INPE fires: %s", e)

        logger.info("ERA5 clima...")
        try:
            cl = fetch_climate(session, days_back=10)
            logger.info("  filas=%d  estaciones=%s", cl["rows_upserted"], ", ".join(cl["stations"]))
        except Exception as e:
            logger.warning("ERA5: %s", e)

        logger.info("Signal logger — forward returns...")
        try:
            from services.signal_logger import fill_forward_returns
            filled = fill_forward_returns(session, instrument="SBN26")
            if sum(filled.values()) > 0:
                logger.info("  5d=%d  10d=%d  20d=%d", filled[5], filled[10], filled[20])
        except Exception as e:
            logger.warning("signal_logger: %s", e)

        logger.info("GEE metrics (cache DB)...")
        try:
            from ingestion.gee_crops import get_latest_gee_metrics
            gee = get_latest_gee_metrics(session)
            if gee:
                for poi_id, metrics in gee.items():
                    parts = [f"{m}={d.get('value',0):.3f}" for m, d in metrics.items()
                             if d.get("value") is not None]
                    logger.info("  %-25s %s", poi_id, "  ".join(parts))
            else:
                logger.info("  Sin datos GEE")
        except Exception as e:
            logger.warning("GEE: %s", e)

    logger.info("Pipeline completado en %.1fs", (datetime.now() - start).total_seconds())


if __name__ == "__main__":
    run()
