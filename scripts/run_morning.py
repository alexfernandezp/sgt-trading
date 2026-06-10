"""
Pipeline matutino SGT Trading — lanzar 07:00 L-V.

Orden de ejecución:
  [1] Precios diarios  (SB_CONT, spreads, macro — yfinance)
  [2] Barras históricas 1h/4h/1wk/1mo  (historia larga, 1x/día es suficiente)
  [3] CEPEA  (precios físicos etanol/azúcar Brasil)
  [3b] UNICADATA  (preço ao produtor SP hidratado semanal ex-mill)
  [4] Santos port  (snapshot berthed/scheduled)
  [5] Paranaguá port  (snapshot)
  [6] INPE fires  (focos SP+PR, rolling 7d)
  [7] ERA5 clima  (precipitación/temperatura BR, rolling 10d)
  [8] Signal logger  (rellenar forward returns SBN26)
  [8b] Parity snapshot  (guardar paridad etanol/azúcar SP en ethanol_parity_daily)
  [9] score_today.py  (scoring completo — output a logs/score_YYYYMMDD.log)

Log: logs/morning_YYYYMMDD.log
     logs/score_YYYYMMDD.log  (output completo del score)
"""
import sys, os, logging, subprocess
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
_today = datetime.now().strftime("%Y%m%d")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(LOG_DIR, f"morning_{_today}.log"), encoding="utf-8"),
    ]
)
logger = logging.getLogger("morning")

# Barras históricas largas — solo necesitan refresh 1x/día (no en el loop intraday)
_DAILY_INTERVALS = ["1h", "4h", "1wk", "1mo"]


def run():
    t0 = datetime.now()
    logger.info("=" * 60)
    logger.info("SGT Morning Pipeline  %s", t0.strftime("%Y-%m-%d %H:%M"))
    logger.info("=" * 60)

    from database import SessionLocal
    from ingestion.prices import fetch_prices
    from ingestion.intraday import fetch_intraday
    from ingestion.cepea import fetch_cepea
    from ingestion.santos_port import fetch_santos_port
    from ingestion.paranagua_port import fetch_paranagua_port
    from ingestion.inpe_fires import fetch_fires
    from ingestion.climate_openmeteo import fetch_climate
    from services.signal_logger import fill_forward_returns

    with SessionLocal() as session:

        # [1] Precios diarios
        logger.info("[1/8] Precios diarios...")
        try:
            for instr, count in fetch_prices(session, days_back=5).items():
                logger.info("  %-15s %s", instr, f"{count} filas" if count else "sin nuevos")
        except Exception as e:
            logger.error("fetch_prices: %s", e)

        # [2] Barras históricas largas
        logger.info("[2/8] Barras históricas 1h/4h/1wk/1mo...")
        try:
            for instr, ivs in fetch_intraday(session, intervals=_DAILY_INTERVALS).items():
                for iv, n in ivs.items():
                    logger.info("  %-10s %-5s  %d filas", instr, iv, n)
        except Exception as e:
            logger.error("fetch_intraday histórico: %s", e)

        # [3] CEPEA
        logger.info("[3/9] CEPEA...")
        try:
            c = fetch_cepea(session)
            lat = c.get("latest", {})
            logger.info("  etanol=%d filas  azúcar=%d filas", c["ethanol_rows"], c["sugar_rows"])
            if lat.get("hydrous_paulinia_usd_m3"):
                logger.info("  Etanol Paulínia: %.2f US$/m³", lat["hydrous_paulinia_usd_m3"])
        except Exception as e:
            logger.warning("CEPEA: %s", e)

        # [3b] UNICADATA — desactivado (scraper HTML/Flash no funciona)
        # Paridad usa CEPEA Paulínia como fuente diaria (~0.7c offset vs Green Pool ex-mill)
        logger.debug("[3b] UNICADATA omitido — usando CEPEA Paulínia para paridad")

        # [4] Santos
        logger.info("[4/8] Santos port...")
        try:
            s = fetch_santos_port(session)
            logger.info("  berthed=%d barcos  %d t  |  scheduled=%d",
                        s["n_berthed"], s["tonnage_berthed"], s["n_scheduled"])
        except Exception as e:
            logger.warning("Santos: %s", e)

        # [5] Paranaguá
        logger.info("[5/8] Paranaguá port...")
        try:
            p = fetch_paranagua_port(session)
            logger.info("  atracados=%d  programados=%d  esperados=%d",
                        p["n_atracados"], p["n_programados"], p["n_esperados"])
        except Exception as e:
            logger.warning("Paranaguá: %s", e)

        # [6] INPE fires
        logger.info("[6/8] INPE fires...")
        try:
            f = fetch_fires(session, days_back=7)
            logger.info("  filas=%d  último=%s", f["rows_upserted"], f["latest_date"])
        except Exception as e:
            logger.warning("INPE fires: %s", e)

        # [7] ERA5 clima
        logger.info("[7/8] ERA5 clima...")
        try:
            cl = fetch_climate(session, days_back=10)
            logger.info("  filas=%d  estaciones=%s",
                        cl["rows_upserted"], ", ".join(cl["stations"]))
        except Exception as e:
            logger.warning("ERA5: %s", e)

        # [8] Signal logger
        logger.info("[8/9] Signal logger — forward returns...")
        try:
            filled = fill_forward_returns(session, instrument="SBN26")
            if sum(filled.values()) > 0:
                logger.info("  5d=%d  10d=%d  20d=%d", filled[5], filled[10], filled[20])
            else:
                logger.info("  Sin retornos pendientes")
        except Exception as e:
            logger.warning("signal_logger: %s", e)

        # [8b] Parity snapshot — guardar paridad diaria en DB
        logger.info("[8b] Parity snapshot etanol/azúcar SP...")
        try:
            from services.ethanol_parity import compute_ethanol_parity_v2
            from services.parity_store import save_parity_snapshot
            parity = compute_ethanol_parity_v2(session)
            if parity.get("ethanol_c_lb"):
                ok = save_parity_snapshot(session, parity)
                src = parity.get("hydrous_source", "?")
                logger.info(
                    "  [%s]  eth=%.2f  NY11=%.2f  gap=%+.2f  ratio=%.3f  %s",
                    src,
                    parity["ethanol_c_lb"], parity.get("ice_c_lb") or 0,
                    parity.get("spread_c_lb") or 0,
                    parity.get("parity_ratio") or 0,
                    parity.get("bias", "?"),
                )
                if not ok:
                    logger.warning("  parity_store: fallo al guardar snapshot")
            else:
                logger.warning("  Sin datos paridad hoy: %s", parity.get("description"))
        except Exception as e:
            logger.warning("parity snapshot: %s", e)

    elapsed = (datetime.now() - t0).total_seconds()
    logger.info("Pipeline completado en %.1fs", elapsed)

    # ── Score Today ───────────────────────────────────────────────────────────
    score_log = os.path.join(LOG_DIR, f"score_{_today}.log")
    logger.info("Lanzando score_today.py → %s", score_log)
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "score_today.py")
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(score_log, "w", encoding="utf-8") as fout:
        r = subprocess.run(
            [sys.executable, script_path],
            stdout=fout, stderr=fout, cwd=project_root
        )
    if r.returncode != 0:
        logger.error("score_today.py salió con código %d — ver %s", r.returncode, score_log)
    else:
        # Extraer las líneas clave del score para el log matutino
        try:
            with open(score_log, encoding="utf-8") as fin:
                lines = fin.readlines()
            for line in lines:
                l = line.rstrip()
                if any(k in l for k in ("RESULTADO", "Score total", "Decision", "Sesgo Capa",
                                        "Trigger Capa", "DECISION COMBINADA", "Direccion auto")):
                    logger.info("  SCORE >> %s", l.strip())
        except Exception:
            pass
        logger.info("score_today.py OK — ver %s", score_log)

    total = (datetime.now() - t0).total_seconds()
    logger.info("Morning pipeline total: %.1fs", total)

    # Log de ejecución en DB
    try:
        from database import SessionLocal as _SL
        from services.parity_store import log_pipeline_run
        with _SL() as _s:
            log_pipeline_run(_s, task_name="morning",
                             script="scripts/run_morning.py",
                             status="ok", duration_s=total,
                             log_file=os.path.join(LOG_DIR, f"morning_{_today}.log"))
    except Exception:
        pass


if __name__ == "__main__":
    run()
