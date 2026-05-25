"""
Descarga NDVI semanal del cinturón azucarero São Paulo via Google Earth Engine.
Fuente: Sentinel-2 SR Harmonized (COPERNICUS/S2_SR_HARMONIZED)
NDVI = (B8 − B4) / (B8 + B4) — resolución 10m, compuesto semanal mediano.

Configuración inicial (una vez):
  pip install earthengine-api
  earthengine authenticate          ← abre navegador, OAuth con cuenta Google
  → guarda credenciales en ~/.config/earthengine/credentials

  Luego establece GEE_PROJECT_ID en el .env del proyecto.
  O ejecuta: py scripts/setup_gee.py

Región de interés (ROI): cinturón azucarero São Paulo
  Polígono simplificado que cubre los principales municipios cañeros:
  Ribeirão Preto, Piracicaba, Sertãozinho, Barretos, Araçatuba, Jaú.
  Aprox. lat 20–23°S, lon 46–51°W
"""
import logging
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Región de interés: cinturón azucarero SP (polígono simplificado)
SP_SUGARCANE_REGION = [
    [-51.5, -19.5],
    [-45.5, -19.5],
    [-45.5, -23.5],
    [-51.5, -23.5],
    [-51.5, -19.5],
]
REGION_NAME = "sp_sugarcane_belt"

# Escala de muestreo: 500m es suficiente para tendencia regional, mucho más rápido que 10m
SAMPLE_SCALE = 500
MAX_PIXELS   = 1e8


def _init_ee() -> bool:
    """Inicializa Earth Engine. Retorna True si OK."""
    try:
        import ee
        from config import GEE_PROJECT_ID
        if GEE_PROJECT_ID:
            ee.Initialize(project=GEE_PROJECT_ID)
        else:
            ee.Initialize()
        return True
    except Exception as e:
        logger.warning("GEE init failed: %s", e)
        return False


def _compute_ndvi_week(week_start: date, week_end: date) -> Optional[dict]:
    """
    Calcula NDVI medio sobre la región SP para la semana dada.
    Retorna dict con mean_ndvi, std_ndvi, cloud_cover_pct, pixel_count, scene_count.
    """
    try:
        import ee

        region = ee.Geometry.Polygon([SP_SUGARCANE_REGION])

        def mask_clouds(img):
            scl = img.select("SCL")
            # SCL=4 (vegetation), SCL=5 (bare soil) → buenos para NDVI
            # Excluir SCL=3 (cloud shadow), 8,9,10 (clouds), 11 (snow)
            mask = scl.neq(3).And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10)).And(scl.neq(11))
            cloud_pct = img.get("CLOUDY_PIXEL_PERCENTAGE")
            return img.updateMask(mask).set("CLOUDY_PIXEL_PERCENTAGE", cloud_pct)

        def add_ndvi(img):
            ndvi = img.normalizedDifference(["B8", "B4"]).rename("ndvi")
            return img.addBands(ndvi)

        col = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(region)
            .filterDate(str(week_start), str(week_end))
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 70))
            .map(mask_clouds)
            .map(add_ndvi)
        )

        scene_count = col.size().getInfo()
        if scene_count == 0:
            return None

        composite = col.select("ndvi").median()

        # Estadísticas regionales
        stats = composite.reduceRegion(
            reducer   = ee.Reducer.mean().combine(ee.Reducer.stdDev(), sharedInputs=True),
            geometry  = region,
            scale     = SAMPLE_SCALE,
            maxPixels = MAX_PIXELS,
            bestEffort= True,
        ).getInfo()

        mean_ndvi = stats.get("ndvi_mean")
        std_ndvi  = stats.get("ndvi_stdDev")

        if mean_ndvi is None:
            return None

        # Cobertura nubosa media de las escenas
        cloud_list = col.aggregate_array("CLOUDY_PIXEL_PERCENTAGE").getInfo()
        cloud_pct  = sum(cloud_list) / len(cloud_list) if cloud_list else None

        # Contar píxeles válidos (approx: área/scale^2 × fracción válida)
        pixel_count = composite.reduceRegion(
            reducer   = ee.Reducer.count(),
            geometry  = region,
            scale     = SAMPLE_SCALE,
            maxPixels = MAX_PIXELS,
            bestEffort= True,
        ).getInfo().get("ndvi", 0)

        return {
            "mean_ndvi":       round(float(mean_ndvi), 4),
            "std_ndvi":        round(float(std_ndvi), 4) if std_ndvi else None,
            "cloud_cover_pct": round(float(cloud_pct), 1) if cloud_pct else None,
            "pixel_count":     int(pixel_count) if pixel_count else None,
            "scene_count":     int(scene_count),
        }
    except Exception as e:
        logger.warning("GEE NDVI compute (%s–%s): %s", week_start, week_end, e)
        return None


def fetch_ndvi(session, weeks_back: int = 4) -> dict:
    """
    Calcula y guarda NDVI semanal de las últimas `weeks_back` semanas.

    Returns dict:
      rows_upserted : int
      latest_ndvi   : float (NDVI más reciente)
      latest_date   : str
      errors        : list[str]
    """
    from models.market_data import NdviSentinel
    from models import Base
    from database import engine
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    Base.metadata.create_all(engine, tables=[NdviSentinel.__table__])

    result = {
        "rows_upserted": 0,
        "latest_ndvi":   None,
        "latest_date":   None,
        "errors":        [],
    }

    if not _init_ee():
        result["errors"].append("GEE no inicializado — ejecutar setup_gee.py")
        return result

    today = date.today()
    rows_done = 0
    latest_ndvi = None
    latest_date = None

    for w in range(weeks_back, 0, -1):
        week_end   = today - timedelta(weeks=w - 1) - timedelta(days=2)
        week_start = week_end - timedelta(days=6)

        ndvi_data = _compute_ndvi_week(week_start, week_end)
        if ndvi_data is None:
            logger.debug("GEE: sin datos S2 para semana %s–%s", week_start, week_end)
            continue

        stmt = (
            pg_insert(NdviSentinel)
            .values(
                obs_date        = week_start,
                region_name     = REGION_NAME,
                mean_ndvi       = ndvi_data["mean_ndvi"],
                std_ndvi        = ndvi_data["std_ndvi"],
                cloud_cover_pct = ndvi_data["cloud_cover_pct"],
                pixel_count     = ndvi_data["pixel_count"],
                scene_count     = ndvi_data["scene_count"],
                source          = "sentinel2_gee",
            )
            .on_conflict_do_update(
                constraint = "uq_ndvi_date_region",
                set_       = {
                    "mean_ndvi":       ndvi_data["mean_ndvi"],
                    "std_ndvi":        ndvi_data["std_ndvi"],
                    "cloud_cover_pct": ndvi_data["cloud_cover_pct"],
                    "pixel_count":     ndvi_data["pixel_count"],
                    "scene_count":     ndvi_data["scene_count"],
                },
            )
        )
        session.execute(stmt)
        rows_done += 1
        latest_ndvi = ndvi_data["mean_ndvi"]
        latest_date = str(week_start)
        logger.info("NDVI %s: %.4f (N_scenes=%d)", week_start, ndvi_data["mean_ndvi"], ndvi_data["scene_count"])

    session.commit()
    result["rows_upserted"] = rows_done
    result["latest_ndvi"]   = latest_ndvi
    result["latest_date"]   = latest_date
    return result


def get_latest_ndvi(session) -> Optional[dict]:
    """Retorna el registro NDVI más reciente de la DB."""
    try:
        from sqlalchemy import text
        row = session.execute(text("""
            SELECT obs_date, mean_ndvi, std_ndvi, cloud_cover_pct, scene_count
            FROM ndvi_sentinel
            WHERE region_name = 'sp_sugarcane_belt'
            ORDER BY obs_date DESC LIMIT 1
        """)).fetchone()
        if row:
            return {
                "obs_date":        str(row[0]),
                "mean_ndvi":       float(row[1]) if row[1] else None,
                "std_ndvi":        float(row[2]) if row[2] else None,
                "cloud_cover_pct": float(row[3]) if row[3] else None,
                "scene_count":     row[4],
            }
    except Exception as e:
        logger.warning("get_latest_ndvi: %s", e)
    return None
