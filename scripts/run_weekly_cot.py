"""
Ingesta semanal COT + resumen de señal — SGT Trading.

CFTC publica los martes (reference date) cada viernes 15:30 ET = 21:30 Madrid.
Ejecutar viernes 21:35 Madrid (5 min de margen).

Acciones:
  1. fetch_cot  — ingesta Legacy + Disaggregated últimas 8 semanas
  2. get_cot_signal  — computa estado compuesto nivel×velocidad
  3. Log resumen legible (para revisar el viernes por la noche)

Log: logs/cot_YYYYMMDD.log
"""
import sys, os, logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(LOG_DIR, f"cot_{datetime.now().strftime('%Y%m%d')}.log"),
            encoding="utf-8"
        ),
    ]
)
logger = logging.getLogger("cot_weekly")

_STARS = {3: "★★★  MAX CONVICCION", 2: "★★   ALTA", 1: "★    MODERADA", 0: "     NEUTRO"}


def run():
    logger.info("=" * 60)
    logger.info("SGT Weekly COT  %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    logger.info("CFTC Legacy + Disaggregated — SUGAR NO.11 ICE FUTURES U.S.")
    logger.info("=" * 60)

    from database import SessionLocal
    from ingestion.cot import fetch_cot
    from services.cot_signal import get_cot_signal

    with SessionLocal() as session:

        # Ingesta
        logger.info("Ingesting COT...")
        try:
            n = fetch_cot(session, limit=8)
            logger.info("  %d filas ingested/updated", n)
        except Exception as e:
            logger.error("fetch_cot FAILED: %s", e)
            return

        # Señal compuesta
        logger.info("Computing COT signal...")
        try:
            sig = get_cot_signal(session)
        except Exception as e:
            logger.error("get_cot_signal FAILED: %s", e)
            return

        # Resumen legible
        logger.info("")
        logger.info("─" * 60)
        logger.info("  RESUMEN COT — %s", datetime.now().strftime("%Y-%m-%d"))
        logger.info("─" * 60)
        logger.info("  MM net (hedge funds)  : %s", f"{sig.mm_net:+,}")
        logger.info("  Spec net (NC+NonRep)  : %s  (idioma sector)", f"{sig.spec_net:+,}")
        logger.info("  Rango 3yr             : %s … %s", f"{sig.mm_3yr_min:+,}", f"{sig.mm_3yr_max:+,}")
        logger.info("  Percentil 3yr         : %.0f%%  (1yr: %.0f%%  all-time: %.0f%%)",
                    sig.mm_pct_3yr, sig.mm_pct_1yr, sig.mm_pct_alltime)
        logger.info("")
        logger.info("  Spec delta 1 semana   : %s", f"{sig.spec_change_1wk:+,}")
        logger.info("  MM  delta 1 semana    : %s", f"{sig.mm_change_1wk:+,}")
        logger.info("  z velocidad (spec)    : %+.2f  ->  %s", sig.mm_weekly_z, sig.velocity_class)
        logger.info("  Tendencia MA4s (mm)   : %s", f"{int(sig.mm_trend_4wk):+,}")
        logger.info("")
        logger.info("  Nivel                 : %s", sig.level_regime)
        logger.info("  Estado compuesto      : %s", sig.composite_state)
        logger.info("  Convicción            : %s", _STARS.get(sig.conviction, str(sig.conviction)))
        logger.info("")
        logger.info("  >>> %s", sig.context_str)
        logger.info("─" * 60)

        if sig.conviction >= 2:
            direction = "LONG" if sig.signal_long else "SHORT"
            logger.info("")
            logger.info("  *** SEÑAL ACTIVA: %s — %s ***", direction, sig.composite_state)
            logger.info("  Edge esperado: 71-85%% WR gap apertura lunes (backtest 18yr OOS)")
            logger.info("  Acción: monitorear apertura lunes. Confirmar con L2 antes de entrar.")
        else:
            logger.info("  Estado NEUTRO — sin señal contrarian accionable esta semana.")

    logger.info("")
    logger.info("COT weekly OK")


if __name__ == "__main__":
    run()
