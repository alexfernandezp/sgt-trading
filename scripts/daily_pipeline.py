"""
Pipeline diario SGT Trading. Ejecutar cada manana antes de operar.
Uso: py scripts/daily_pipeline.py
"""
import sys, os, logging
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s - %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("daily_pipeline")

from database import SessionLocal
from ingestion.prices import fetch_prices
from ingestion.cot import fetch_cot
from ingestion.intraday import fetch_intraday
from ingestion.santos_port import fetch_santos_port
from ingestion.cepea import fetch_cepea
from ingestion.oni import fetch_oni
from ingestion.climate_openmeteo import fetch_climate
from ingestion.ndvi_gee import fetch_ndvi
from ingestion.comex_stat import fetch_comex_stat
from ingestion.inpe_fires import fetch_fires
from ingestion.paranagua_port import fetch_paranagua_port
from ingestion.conab_cana import get_latest_conab, fetch_conab


def run():
    start = datetime.now()
    logger.info("=" * 55)
    logger.info("SGT Trading - Pipeline diario")
    logger.info(f"Fecha: {start.strftime('%Y-%m-%d %H:%M')}")
    logger.info("=" * 55)

    session = SessionLocal()
    try:
        logger.info("Precios diarios (30 dias)...")
        for instr, count in fetch_prices(session, days_back=30).items():
            logger.info(f"  {instr:<15} {count} filas" if count else f"  {instr:<15} sin datos")

        logger.info("COT (8 semanas)...")
        logger.info(f"  SUGAR_NO11_ICE  {fetch_cot(session, limit=8)} filas")

        logger.info("Barras intraday (5m, 30m, 1h, 4h, 1wk, 1mo)...")
        for instr, ivs in fetch_intraday(session).items():
            for iv, n in ivs.items():
                logger.info(f"  {instr:<12} {iv:<5} {n} filas")

        logger.info("Puerto de Santos — ship tracker (ACUCAR)...")
        try:
            santos = fetch_santos_port(session)
            logger.info(f"  Expected(Long): {santos['n_expected']} barcos  {santos['tonnage_expected']} t")
            logger.info(f"  Scheduled:      {santos['n_scheduled']} barcos")
            logger.info(f"  Berthed:        {santos['n_berthed']} barcos  {santos['tonnage_berthed']} t cargando")
            if santos["errors"]:
                for err in santos["errors"]:
                    logger.warning("  Santos error: %s", err)
        except Exception as _e:
            logger.warning("Santos port error (no critico): %s", _e)

        logger.info("CEPEA — precios etanol y azúcar físicos Brasil...")
        try:
            cepea = fetch_cepea(session)
            logger.info(f"  Etanol rows: {cepea['ethanol_rows']}  Azúcar rows: {cepea['sugar_rows']}")
            lat = cepea.get("latest", {})
            if lat.get("hydrous_paulinia_usd_m3"):
                logger.info(f"  Etanol hidratado Paulínia: {lat['hydrous_paulinia_usd_m3']:.2f} US$/m³")
            if lat.get("crystal_sugar_usd_bag50kg"):
                logger.info(f"  Azúcar cristal: {lat['crystal_sugar_usd_bag50kg']:.2f} US$/bolsa 50kg")
            if cepea["errors"]:
                for err in cepea["errors"]:
                    logger.warning("  CEPEA error: %s", err)
        except Exception as _e:
            logger.warning("CEPEA error (no critico): %s", _e)

        logger.info("ONI — Índice ENSO NOAA/CPC...")
        try:
            oni = fetch_oni(session)
            logger.info(f"  Filas actualizadas: {oni['rows_upserted']}")
            if oni["latest_oni"] is not None:
                logger.info(f"  Último ONI: {oni['latest_season']} {oni['latest_year']} = "
                            f"{oni['latest_oni']:+.2f} [{oni['classification']}]")
            for err in oni.get("errors", []):
                logger.warning("  ONI error: %s", err)
        except Exception as _e:
            logger.warning("ONI error (no critico): %s", _e)

        logger.info("Clima — Open-Meteo ERA5 (Ribeirão Preto + Piracicaba)...")
        try:
            climate = fetch_climate(session, days_back=10)
            logger.info(f"  Filas actualizadas: {climate['rows_upserted']}  "
                        f"Estaciones: {', '.join(climate['stations'])}")
            for err in climate.get("errors", []):
                logger.warning("  Clima error: %s", err)
        except Exception as _e:
            logger.warning("Open-Meteo error (no critico): %s", _e)

        logger.info("NDVI — Sentinel-2 GEE (cinturón azucarero SP)...")
        try:
            ndvi = fetch_ndvi(session, weeks_back=2)
            if ndvi["latest_ndvi"] is not None:
                logger.info(f"  NDVI más reciente: {ndvi['latest_ndvi']:.4f} ({ndvi['latest_date']})")
                logger.info(f"  Filas actualizadas: {ndvi['rows_upserted']}")
            for err in ndvi.get("errors", []):
                logger.warning("  NDVI GEE: %s", err)
        except Exception as _e:
            logger.warning("NDVI GEE error (no critico): %s", _e)

        logger.info("Comex Stat — exportaciones azúcar Brasil (MAPA/CGDA PDF)...")
        try:
            comex = fetch_comex_stat(session)
            logger.info(f"  Filas actualizadas: {comex['rows_upserted']}  "
                        f"Periodo: {comex['latest_period'] or 'sin datos'}")
            if comex.get("yoy_change_pct") is not None:
                logger.info(f"  YTD actual: {(comex['ytd_curr_t'] or 0)/1e6:.2f} Mt  "
                            f"YoY: {comex['yoy_change_pct']:+.1f}%")
            for err in comex.get("errors", []):
                logger.warning("  Comex Stat error: %s", err)
        except Exception as _e:
            logger.warning("Comex Stat error (no critico): %s", _e)

        logger.info("INPE BDQueimadas — focos incendio SP + PR...")
        try:
            fires = fetch_fires(session, days_back=7)
            logger.info(f"  Filas actualizadas: {fires['rows_upserted']}  "
                        f"Último: {fires['latest_date']} {fires['latest_counts']}")
            for err in fires.get("errors", []):
                logger.warning("  INPE fuego error: %s", err)
        except Exception as _e:
            logger.warning("INPE fuego error (no critico): %s", _e)

        logger.info("Puerto de Paranaguá — ship tracker (ACUCAR)...")
        try:
            paranagua = fetch_paranagua_port(session)
            logger.info(f"  Atracados: {paranagua['n_atracados']}  "
                        f"Programados: {paranagua['n_programados']}  "
                        f"Esperados: {paranagua['n_esperados']}  "
                        f"Despachados: {paranagua['n_despachados']}")
            if paranagua["errors"]:
                for err in paranagua["errors"]:
                    logger.warning("  Paranaguá error: %s", err)
        except Exception as _e:
            logger.warning("Paranaguá port error (no critico): %s", _e)

        logger.info("CONAB — comprobando nuevo levantamento...")
        try:
            from datetime import date as _date
            latest = get_latest_conab(session)
            if latest:
                cur_season_start = _date.today().year if _date.today().month >= 4 else _date.today().year - 1
                cur_season = f"{cur_season_start}/{str(cur_season_start+1)[2:]}"
                if latest["season"] == cur_season:
                    next_lev = latest["levantamento"] + 1
                else:
                    cur_season_start = int(latest["season"].split("/")[0])
                    next_lev = latest["levantamento"] + 1
                result_c = fetch_conab(session, cur_season_start, next_lev, None)
                if result_c.get("errors"):
                    logger.info("  Lev %d: no disponible aún", next_lev)
                else:
                    logger.info("  NUEVO levantamento ingerido: %s %dº lev  azúcar=%.2f Mt  rev=%s%%",
                                result_c["season"], result_c["levantamento"],
                                result_c.get("sugar_total_mt") or 0,
                                f"{result_c['revision_sugar_pct']:+.1f}" if result_c.get("revision_sugar_pct") else "N/A")
            else:
                logger.info("  Sin datos CONAB previos — ejecutar fetch_conab.py manualmente")
        except Exception as _e:
            logger.warning("CONAB auto-check error (no critico): %s", _e)

        logger.info("Signal Log — rellenando retornos forward para IC weighting...")
        try:
            from services.signal_logger import fill_forward_returns
            filled = fill_forward_returns(session, instrument="SBN26")
            total_filled = sum(filled.values())
            if total_filled > 0:
                logger.info("  Retornos rellenados: 5d=%d  10d=%d  20d=%d",
                            filled[5], filled[10], filled[20])
            else:
                logger.info("  Sin retornos pendientes de rellenar")
        except Exception as _e:
            logger.warning("Signal log forward returns error (no critico): %s", _e)

        logger.info("GEE Crops — leyendo métricas almacenadas (NDVI/LST/SPI)...")
        try:
            from ingestion.gee_crops import get_latest_gee_metrics
            gee = get_latest_gee_metrics(session)
            if gee:
                for poi_id, metrics in gee.items():
                    parts = []
                    for metric, d in metrics.items():
                        z = d.get("z_score")
                        v = d.get("value")
                        anom = " ⚠" if d.get("anomaly") else ""
                        if v is not None:
                            parts.append(f"{metric}={v:.3f}(z={z:+.2f}{anom})"
                                         if z is not None else f"{metric}={v:.3f}")
                    logger.info("  %-25s %s", poi_id, "  ".join(parts))
            else:
                logger.info("  Sin datos GEE (ejecutar: py scripts/run_gee_crops.py)")
        except Exception as _e:
            logger.warning("GEE crops read error (no critico): %s", _e)

    except Exception as exc:
        logger.error(f"Error: {exc}", exc_info=True)
        session.rollback()
        raise
    finally:
        session.close()

    logger.info(f"Pipeline completado en {(datetime.now()-start).total_seconds():.1f}s")


if __name__ == "__main__":
    run()
