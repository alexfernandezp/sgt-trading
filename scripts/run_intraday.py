"""
Loop continuo de barras intraday — SGT Trading.

Refresca barras 1m/5m/30m cada 30 segundos durante la sesión de mercado.
Captura cada barra 1m en cuanto completa (efectivamente "live").
Sale solo al cerrar la sesión — no necesita kill manual.

Sesión:  09:25-19:15 Madrid  (ICE NY Sugar abre 09:30, cierra 19:00)
Fuente:  yfinance (Yahoo Finance, ~10s delay)
Destino: tabla price_bars (upsert idempotente)

Task Scheduler: diario L-V, inicio 09:25
"""
import sys, os, time, logging
from datetime import datetime, time as dtime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(LOG_DIR, f"intraday_{datetime.now().strftime('%Y%m%d')}.log"),
            encoding="utf-8"
        ),
    ]
)
logger = logging.getLogger("intraday_loop")

SESSION_START  = dtime(9, 25)    # 5 min antes apertura azúcar NY
SESSION_END    = dtime(19, 15)   # 15 min después cierre
REFRESH_SEC    = 30              # intervalo loop — 30s es el mínimo seguro con yfinance
MAX_ERRORS     = 10              # abortar si 10 errores seguidos (API caída)

# Solo barras cortas — 1h/4h/1wk/1mo los gestiona run_morning.py (1x/día)
LIVE_INTERVALS = ["1m", "5m", "30m"]


def _in_session() -> bool:
    t = datetime.now().time()
    return SESSION_START <= t <= SESSION_END


def _refresh_once() -> int:
    from database import SessionLocal
    from ingestion.intraday import fetch_intraday
    with SessionLocal() as session:
        result = fetch_intraday(session, intervals=LIVE_INTERVALS)
    return sum(n for ivs in result.values() for n in ivs.values())


def run():
    logger.info("=" * 55)
    logger.info("SGT Intraday Loop  %s", datetime.now().strftime("%Y-%m-%d"))
    logger.info("Ventana: %s-%s  |  refresh cada %ds  |  intervals: %s",
                SESSION_START.strftime("%H:%M"), SESSION_END.strftime("%H:%M"),
                REFRESH_SEC, ",".join(LIVE_INTERVALS))
    logger.info("=" * 55)

    # Esperar si arranca antes de la sesión
    while not _in_session():
        if datetime.now().time() > SESSION_END:
            logger.info("Fuera de horario de sesión — saliendo.")
            return
        logger.info("Pre-sesión (%s) — esperando...", datetime.now().strftime("%H:%M:%S"))
        time.sleep(30)

    consecutive_errors = 0
    while _in_session():
        t0 = time.monotonic()
        try:
            n = _refresh_once()
            consecutive_errors = 0
            logger.info("OK  %d barras  [%s]", n, datetime.now().strftime("%H:%M:%S"))
        except Exception as exc:
            consecutive_errors += 1
            logger.warning("Error #%d: %s", consecutive_errors, exc)
            if consecutive_errors >= MAX_ERRORS:
                logger.error("Demasiados errores consecutivos — abortando.")
                raise

        elapsed = time.monotonic() - t0
        sleep_s = max(1.0, REFRESH_SEC - elapsed)
        time.sleep(sleep_s)

    logger.info("Sesión cerrada (%s) — loop finalizado.", datetime.now().strftime("%H:%M:%S"))


if __name__ == "__main__":
    run()
