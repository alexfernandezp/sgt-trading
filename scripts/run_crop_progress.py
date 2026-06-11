"""
Brazil Crop Progress runner — ejecutar cuando UNICA publica (~cada 15 días).

Workflow:
  1. Descarga PDF quinzenal más reciente de UNICA.
  2. Parsea Tabelas 3-7, de-acumula a neto por quincena, upsert en unica_biweekly.
  3. Calcula señales ALL-IN (cumsum, robust_stats, descomposición, proyección, bias).
  4. Imprime reporte completo en consola y logs/crop_progress_YYYYMMDD.log.

Uso:
    py scripts/run_crop_progress.py
    py scripts/run_crop_progress.py --no-fetch   # solo recalcula sin bajar PDF nuevo
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

        # 1. Fetch y persist quincenas del PDF UNICA
        if not no_fetch:
            logger.info("[1] Descargando último reporte quinzenal UNICA...")
            try:
                from ingestion.unica import get_latest_unica, save_unica_to_db
                data = get_latest_unica()
                if data:
                    saved = save_unica_to_db(session, data)
                    logger.info(
                        "  UNICA idM=%s  safra=%s  Q%d/%d  "
                        "acucar_acum=%.3f Mt  proyeccion=%s Mt  saved=%s",
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
                logger.error("  UNICA fetch/save: %s", e, exc_info=True)
        else:
            logger.info("[1] Skipping UNICA fetch (--no-fetch)")

        # 2. Calcular señales crop progress
        logger.info("[2] Calculando señales Brazil Crop Progress...")
        try:
            from services.brazil_crop_progress import format_unica_forecast_table
            signals = compute_crop_progress(session, region="CS")
            report = format_crop_progress_report(signals)
            logger.info("\n%s", report)
            forecast_table = format_unica_forecast_table(signals)
            logger.info("%s", forecast_table)

            if not signals.get("error"):
                sig_a = signals.get("A_cane_pace") or {}
                sig_b = signals.get("B_sugar_pace") or {}
                proj  = signals.get("F_proj") or {}
                proj_sugar = proj.get("sugar") or {}
                logger.info(
                    "SUMMARY: safra=%s seq=%d | "
                    "cum_cane=%.2fMt cum_sugar=%.2fMt | "
                    "yoy_cane=%s%% yoy_sugar=%s%% | "
                    "cane_mZ=%s [%s] sugar_mZ=%s [%s] | "
                    "atr_delta=%s kg/t | "
                    "proj_sugar=%s Mt | bias=%s",
                    signals.get("latest_safra"),
                    signals.get("latest_seq", 0),
                    signals.get("cum_cane_mt") or 0,
                    signals.get("cum_sugar_mt") or 0,
                    signals.get("yoy_cane_pct"),
                    signals.get("yoy_sugar_pct"),
                    sig_a.get("modified_z"), sig_a.get("conviction"),
                    sig_b.get("modified_z"), sig_b.get("conviction"),
                    signals.get("atr_delta"),
                    proj_sugar.get("point_mt"),
                    signals.get("H_bias_ice11"),
                )

            # Escribir fundamental_backdrop.json para consumo de score_today.py
            try:
                import json as _j, os as _oj
                bias_v  = signals.get("H_bias_ice11") or 0.0
                yoy_c   = signals.get("yoy_cane_pct")
                yoy_s   = signals.get("yoy_sugar_pct")
                safra   = signals.get("latest_safra", "?")
                seq_n   = signals.get("latest_seq", 0)
                proj_s  = (signals.get("F_proj") or {}).get("sugar") or {}
                proj_pt = proj_s.get("point_mt")
                direction = "BEARISH" if bias_v < -0.15 else ("BULLISH" if bias_v > 0.15 else "NEUTRAL")
                summary_parts = ["safra %s seq=%d" % (safra, seq_n)]
                if yoy_c is not None:
                    summary_parts.append("YoY cana %+.1f%%" % yoy_c)
                if yoy_s is not None:
                    summary_parts.append("azucar %+.1f%%" % yoy_s)
                if proj_pt is not None:
                    summary_parts.append("proj azu=%.1f Mt" % proj_pt)
                backdrop = {
                    "updated":   t0.strftime("%Y-%m-%d %H:%M"),
                    "direction": direction,
                    "bias":      round(bias_v, 3),
                    "summary":   "  |  ".join(summary_parts),
                    "safra":     safra,
                    "seq":       seq_n,
                    "yoy_cane_pct":  yoy_c,
                    "yoy_sugar_pct": yoy_s,
                    "proj_sugar_mt": proj_pt,
                    "cum_cane_mt":   signals.get("cum_cane_mt"),
                    "cum_sugar_mt":  signals.get("cum_sugar_mt"),
                }
                bd_path = _oj.path.join(_oj.path.dirname(_oj.path.abspath(__file__)),
                                        "..", "logs", "fundamental_backdrop.json")
                with open(bd_path, "w", encoding="utf-8") as _fbd:
                    _j.dump(backdrop, _fbd, ensure_ascii=False, indent=2)
                logger.info("fundamental_backdrop.json actualizado → %s", _oj.path.abspath(bd_path))
            except Exception as _be:
                logger.warning("No se pudo escribir fundamental_backdrop.json: %s", _be)

        except Exception as e:
            logger.error("  compute_crop_progress: %s", e, exc_info=True)

    elapsed = (datetime.now() - t0).total_seconds()
    logger.info("Crop Progress completado en %.1fs  →  %s", elapsed, LOG_FILE)


if __name__ == "__main__":
    run()
