"""
Pipeline mensual SGT — fuentes de actualización infrecuente.
Ejecutar días 1-5 y 8-15 de cada mes a las 07:00.

El script detecta qué día es y ejecuta el bloque correspondiente:
  Días  1-5 : ONI + Comex Stat + CONAB check
  Días 8-15 : USDA WASDE + MAPA Brazil Production

Si se lanza fuera de esas ventanas, informa y sale limpiamente.

Log: logs/monthly_YYYYMMDD.log
"""
import sys, os, logging
from datetime import datetime, date

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
            os.path.join(LOG_DIR, f"monthly_{datetime.now().strftime('%Y%m%d')}.log"),
            encoding="utf-8"
        ),
    ]
)
logger = logging.getLogger("monthly")


def _run_early_month(session):
    """Días 1-5: ONI + Comex Stat + CONAB."""
    from ingestion.oni import fetch_oni
    from ingestion.comex_stat import fetch_comex_stat
    from ingestion.conab_cana import get_latest_conab, fetch_conab

    logger.info("[1/3] ONI — ENSO NOAA/CPC...")
    try:
        oni = fetch_oni(session)
        logger.info("  filas=%d  %s %s = %+.2f [%s]",
                    oni["rows_upserted"],
                    oni.get("latest_season", ""), oni.get("latest_year", ""),
                    oni.get("latest_oni") or 0.0,
                    oni.get("classification", ""))
        for err in oni.get("errors", []):
            logger.warning("  ONI: %s", err)
    except Exception as e:
        logger.warning("ONI: %s", e)

    logger.info("[2/3] Comex Stat — exportaciones azúcar Brasil...")
    try:
        cx = fetch_comex_stat(session)
        yoy = f"{cx['yoy_change_pct']:+.1f}%" if cx.get("yoy_change_pct") is not None else "N/A"
        logger.info("  filas=%d  periodo=%s  YoY=%s",
                    cx["rows_upserted"], cx.get("latest_period", ""), yoy)
        for err in cx.get("errors", []):
            logger.warning("  Comex: %s", err)
    except Exception as e:
        logger.warning("Comex Stat: %s", e)

    logger.info("[3/3] CONAB — check nuevo levantamento...")
    try:
        latest = get_latest_conab(session)
        if not latest:
            logger.info("  Sin datos CONAB previos en DB")
            return
        today = date.today()
        cur_year = today.year if today.month >= 4 else today.year - 1
        result_c = fetch_conab(session, cur_year, latest["levantamento"] + 1, None)
        if result_c.get("errors"):
            logger.info("  Lev %d no disponible aún (normal)", latest["levantamento"] + 1)
        else:
            rev = f"{result_c['revision_sugar_pct']:+.1f}%" if result_c.get("revision_sugar_pct") else "N/A"
            logger.info("  NUEVO lev %d: azúcar=%.2f Mt  rev=%s",
                        result_c["levantamento"], result_c.get("sugar_total_mt") or 0, rev)
    except Exception as e:
        logger.warning("CONAB: %s", e)


def _run_mid_month(session):
    """Días 8-15: USDA + MAPA."""
    logger.info("[1/2] USDA WASDE — balance mundial azúcar...")
    try:
        import subprocess as _sp
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fetch_usda.py")
        r = _sp.run([sys.executable, script], capture_output=True, text=True,
                    cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        out = (r.stdout or "").strip()
        if out:
            for line in out.splitlines()[:5]:
                logger.info("  %s", line)
        if r.returncode != 0:
            logger.warning("  fetch_usda exitcode=%d: %s", r.returncode, r.stderr[:300])
        else:
            logger.info("  USDA OK")
    except Exception as e:
        logger.warning("USDA: %s", e)

    logger.info("[2/2] MAPA Brazil Production — check nueva quincena...")
    try:
        from ingestion.brazil_mapa import fetch_brazil_production
        result = fetch_brazil_production(session)
        logger.info("  ingested=%d  revisions=%d  duplicates=%d",
                    result.get("n_inserted", 0),
                    result.get("n_revisions", 0),
                    result.get("n_duplicates", 0))
    except Exception as e:
        logger.warning("MAPA: %s", e)


def run():
    today_day = date.today().day
    logger.info("=" * 60)
    logger.info("SGT Monthly Pipeline  %s  (día %d del mes)",
                datetime.now().strftime("%Y-%m-%d %H:%M"), today_day)
    logger.info("=" * 60)

    from database import SessionLocal
    with SessionLocal() as session:
        if 1 <= today_day <= 5:
            logger.info("Bloque inicio de mes (días 1-5)")
            _run_early_month(session)
        elif 8 <= today_day <= 15:
            logger.info("Bloque mitad de mes (días 8-15)")
            _run_mid_month(session)
        else:
            logger.info("Día %d — fuera de ventanas mensuales. Nada que hacer.", today_day)

    logger.info("Monthly pipeline OK")


if __name__ == "__main__":
    run()
