"""
CONAB Boletim da Safra de Cana-de-Açúcar — script de descarga y parseo.

Ejecutar cuando CONAB publica un nuevo levantamento (~4-6x por temporada).
Calendario típico safra 2026/27:
  1º lev: ~abr 2026   2º lev: ~ago 2026   3º lev: ~oct/nov 2026
  4º lev: ~ene/feb 2027  (5º lev opcional: ~mar 2027)

Uso:
  py scripts/fetch_conab.py --season 2025 --lev 4
  py scripts/fetch_conab.py --season 2025 --lev 4 --date 2026-04-27
  py scripts/fetch_conab.py --season 2025 --lev 1 --lev 2 --lev 3 --lev 4   (backfill)
"""
import sys, os, logging, argparse
from datetime import date
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fetch_conab")


def run(season_start: int, levantamentos: list[int], pub_date: date | None):
    from datetime import datetime
    start = datetime.now()

    logger.info("=" * 60)
    logger.info("SGT Trading — CONAB Boletim Safra Cana-de-Açúcar")
    logger.info("Temporada: %d/%s", season_start, str(season_start + 1)[2:])
    logger.info("=" * 60)

    from database import SessionLocal, create_all_tables
    create_all_tables()
    session = SessionLocal()

    from ingestion.conab_cana import fetch_conab

    try:
        for lev in levantamentos:
            logger.info("Descargando %dº levantamento...", lev)
            result = fetch_conab(session, season_start, lev, pub_date)

            if result.get("errors"):
                for err in result["errors"]:
                    logger.warning("  Error: %s", err)
                continue

            logger.info("  %s %dº lev — %s",
                        result["season"], result["levantamento"],
                        "NUEVO" if result.get("is_new") else "actualizado")

            if result.get("cane_total_mt"):
                logger.info("  Caña total   : %.1f Mt  YoY: %s%%",
                            result["cane_total_mt"],
                            f"{result['yoy_cane_pct']:+.1f}"
                            if result.get("yoy_cane_pct") is not None else "N/D")
            if result.get("sugar_total_mt"):
                logger.info("  Azúcar total : %.2f Mt  YoY: %s%%",
                            result["sugar_total_mt"],
                            f"{result['yoy_sugar_pct']:+.1f}"
                            if result.get("yoy_sugar_pct") is not None else "N/D")
                if result.get("revision_sugar_pct") is not None:
                    rev = result["revision_sugar_pct"]
                    tag = ("↑ BEARISH" if rev >= 2 else
                           "↓ BULLISH" if rev <= -2 else "neutral")
                    logger.info("  Revisión vs lev anterior: %+.1f%%  [%s]", rev, tag)
            if result.get("ethanol_cana_blt"):
                logger.info("  Etanol caña  : %.2f bil L  YoY: %s%%",
                            result["ethanol_cana_blt"],
                            f"{result['yoy_ethanol_cana_pct']:+.1f}"
                            if result.get("yoy_ethanol_cana_pct") is not None else "N/D")

    except Exception as e:
        logger.error("Error fatal: %s", e, exc_info=True)
        session.rollback()
        raise
    finally:
        session.close()

    logger.info("Completado en %.1fs", (datetime.now() - start).total_seconds())
    logger.info("Señal disponible en score_today.py vía macro → conab")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", type=int, required=True,
                        help="Año inicio temporada (ej. 2025 para 2025/26)")
    parser.add_argument("--lev", type=int, action="append", dest="levs",
                        metavar="N", required=True,
                        help="Número(s) de levantamento (puede repetirse)")
    parser.add_argument("--date", type=str, default=None,
                        help="Fecha publicación YYYY-MM-DD (default: hoy)")
    args = parser.parse_args()

    pub = None
    if args.date:
        from datetime import date as _date
        pub = _date.fromisoformat(args.date)

    run(args.season, sorted(set(args.levs)), pub)
