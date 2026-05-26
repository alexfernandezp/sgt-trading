"""
GEE Crop Intelligence — harvest pace, crop stress, SPI por región.

Fuentes de datos GEE:
  Sentinel-2 SR Harmonized → NDVI (harvest pace) + NDWI (estrés hídrico)
  MODIS MOD11A2             → LST temperatura superficial °C (estrés calor)
  CHIRPS Daily              → SPI-90 precipitación acumulada 90d

Metodología z-score:
  Para cada métrica y POI, calcula el valor actual y lo compara con el
  mismo período (semana del año) en los N años anteriores.
  z = (actual − media_baseline) / std_baseline

  z > +1.5 o z < −1.5 → anomalía significativa (flag anomaly=True)

Configuración de POIs: config/gee_pois.json
Salida: tabla gee_metric en PostgreSQL

Ejecución: scripts/run_gee_crops.py (script independiente, NO en daily_pipeline)
  GEE calls pueden tardar 3-8 minutos por ejecución completa.
  Recomendado: 2x por semana (Sentinel-2 tiene revisita ~5 días).
"""
import json
import logging
import os
import statistics
from datetime import date, timedelta
from typing import Optional

from sqlalchemy.dialects.postgresql import insert as pg_insert

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "gee_pois.json",
)

# Colecciones GEE
S2_COLLECTION    = "COPERNICUS/S2_SR_HARMONIZED"
MODIS_LST        = "MODIS/061/MOD11A2"
CHIRPS_DAILY     = "UCSB-CHG/CHIRPS/DAILY"

MAX_PIXELS       = 1e9
ANOMALY_THRESH   = 1.5   # |z| > 1.5 → anomaly flag


# ── Inicialización GEE ────────────────────────────────────────────────────────

def _init_ee() -> bool:
    try:
        import ee
        from config_env import GEE_PROJECT_ID
        if GEE_PROJECT_ID:
            ee.Initialize(project=GEE_PROJECT_ID)
        else:
            ee.Initialize()
        return True
    except ImportError:
        try:
            import ee
            ee.Initialize()
            return True
        except Exception as e:
            logger.warning("GEE init: %s", e)
            return False
    except Exception as e:
        logger.warning("GEE init: %s", e)
        return False


def _load_pois(config_path: str = _CONFIG_PATH) -> list:
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)["pois"]


def _get_geometry(poi: dict):
    import ee
    coords = poi["coords"]
    closed = coords + [coords[0]] if coords[0] != coords[-1] else coords
    return ee.Geometry.Polygon([closed])


# ── Helpers de baseline ───────────────────────────────────────────────────────

def _zscore_from_series(current: float, baseline: list) -> dict:
    """Calcula z-score dado valor actual y lista de valores históricos."""
    if len(baseline) < 3:
        return {"z_score": None, "baseline_mean": None,
                "baseline_std": None, "n_baseline_yrs": len(baseline)}
    mean = statistics.mean(baseline)
    std  = max(statistics.stdev(baseline) if len(baseline) > 1 else 0.01, 0.01)
    z    = round((current - mean) / std, 3)
    return {
        "z_score":        z,
        "baseline_mean":  round(mean, 4),
        "baseline_std":   round(std, 4),
        "n_baseline_yrs": len(baseline),
    }


def _same_week_dates(ref_date: date, year: int) -> tuple:
    """Devuelve (start, end) para la misma semana ISO en el año dado."""
    woy = ref_date.isocalendar()[1]
    try:
        ws = date.fromisocalendar(year, woy, 1)
    except ValueError:
        ws = date(year, ref_date.month, ref_date.day)
    return ws, ws + timedelta(days=7)


# ── Sentinel-2: NDVI + NDWI ──────────────────────────────────────────────────

def _s2_mask_clouds(img):
    import ee
    scl  = img.select("SCL")
    mask = (scl.neq(3).And(scl.neq(8)).And(scl.neq(9))
               .And(scl.neq(10)).And(scl.neq(11)))
    return img.updateMask(mask)


def _s2_add_indices(img):
    import ee
    ndvi = img.normalizedDifference(["B8", "B4"]).rename("ndvi")
    # NDWI Gao: water content in leaf (NIR-SWIR)/(NIR+SWIR)
    ndwi = img.normalizedDifference(["B8", "B11"]).rename("ndwi")
    return img.addBands([ndvi, ndwi])


def _s2_regional_stats(collection, region, scale: int):
    """Calcula media de NDVI y NDWI sobre la región."""
    import ee
    composite = (collection
                 .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 70))
                 .map(_s2_mask_clouds)
                 .map(_s2_add_indices)
                 .select(["ndvi", "ndwi"])
                 .median())
    stats = composite.reduceRegion(
        reducer   = ee.Reducer.mean(),
        geometry  = region,
        scale     = scale,
        maxPixels = MAX_PIXELS,
        bestEffort= True,
    ).getInfo()
    return stats  # {"ndvi": float|None, "ndwi": float|None}


def _get_s2_metrics(poi: dict, week_start: date) -> Optional[dict]:
    """
    Calcula NDVI + NDWI para la semana dada y computa z-score vs baseline.
    Devuelve {"ndvi": {...}, "ndwi": {...}} o None si sin datos.
    """
    try:
        import ee
        region         = _get_geometry(poi)
        scale          = poi.get("scale_m", 500)
        baseline_years = poi.get("baseline_years", 5)
        week_end       = week_start + timedelta(days=7)
        curr_yr        = week_start.year

        def col_for(ws, we):
            return (ee.ImageCollection(S2_COLLECTION)
                    .filterBounds(region)
                    .filterDate(str(ws), str(we)))

        # Año actual
        curr_stats = _s2_regional_stats(col_for(week_start, week_end), region, scale)
        curr_ndvi  = curr_stats.get("ndvi")
        curr_ndwi  = curr_stats.get("ndwi")

        if curr_ndvi is None:
            logger.debug("GEE S2 %s week %s: sin datos", poi["id"], week_start)
            return None

        # Baseline
        ndvi_hist, ndwi_hist = [], []
        for yr in range(curr_yr - baseline_years, curr_yr):
            ws, we = _same_week_dates(week_start, yr)
            h = _s2_regional_stats(col_for(ws, we), region, scale)
            if h.get("ndvi") is not None:
                ndvi_hist.append(float(h["ndvi"]))
            if h.get("ndwi") is not None:
                ndwi_hist.append(float(h["ndwi"]))

        def _build(curr_val, hist):
            if curr_val is None:
                return None
            r = _zscore_from_series(float(curr_val), hist)
            r["value"]   = round(float(curr_val), 4)
            r["anomaly"] = r["z_score"] is not None and abs(r["z_score"]) > ANOMALY_THRESH
            return r

        return {
            "ndvi": _build(curr_ndvi, ndvi_hist),
            "ndwi": _build(curr_ndwi, ndwi_hist),
        }

    except Exception as e:
        logger.warning("GEE S2 %s: %s", poi["id"], e)
        return None


# ── MODIS LST ────────────────────────────────────────────────────────────────

def _get_lst_metrics(poi: dict, ref_date: date) -> Optional[dict]:
    """
    Calcula LST (°C) MODIS MOD11A2 para la semana y z-score vs baseline.
    """
    try:
        import ee
        region         = _get_geometry(poi)
        scale          = max(poi.get("scale_m", 1000), 1000)
        baseline_years = poi.get("baseline_years", 5)
        curr_yr        = ref_date.year
        # MOD11A2 es composición 8 días; buscamos ±8 días alrededor de ref_date
        ws = ref_date - timedelta(days=4)
        we = ref_date + timedelta(days=12)

        def lst_mean(start, end):
            col = (ee.ImageCollection(MODIS_LST)
                   .filterBounds(region)
                   .filterDate(str(start), str(end))
                   .select("LST_Day_1km"))
            if col.size().getInfo() == 0:
                return None
            img   = col.mean()
            stats = img.multiply(0.02).subtract(273.15).reduceRegion(
                reducer   = ee.Reducer.mean(),
                geometry  = region,
                scale     = scale,
                maxPixels = MAX_PIXELS,
                bestEffort= True,
            ).getInfo()
            return stats.get("LST_Day_1km")

        curr_lst = lst_mean(ws, we)
        if curr_lst is None:
            return None

        hist = []
        for yr in range(curr_yr - baseline_years, curr_yr):
            ws_h = ws.replace(year=yr)
            we_h = we.replace(year=yr)
            v = lst_mean(ws_h, we_h)
            if v is not None:
                hist.append(float(v))

        r = _zscore_from_series(float(curr_lst), hist)
        r["value"]   = round(float(curr_lst), 2)
        r["anomaly"] = r["z_score"] is not None and abs(r["z_score"]) > ANOMALY_THRESH
        return {"lst": r}

    except Exception as e:
        logger.warning("GEE LST %s: %s", poi["id"], e)
        return None


# ── CHIRPS SPI-90 ────────────────────────────────────────────────────────────

def _get_spi90(poi: dict, ref_date: date) -> Optional[dict]:
    """
    Calcula precipitación acumulada 90 días (CHIRPS) y z-score vs baseline.
    El z-score resultante es equivalente al SPI-90.
    """
    try:
        import ee
        region         = _get_geometry(poi)
        baseline_years = poi.get("baseline_years", 5)
        curr_yr        = ref_date.year
        end_date       = ref_date
        start_date     = ref_date - timedelta(days=90)

        def precip_90d(start, end):
            col = (ee.ImageCollection(CHIRPS_DAILY)
                   .filterBounds(region)
                   .filterDate(str(start), str(end)))
            if col.size().getInfo() == 0:
                return None
            total = col.sum()
            stats = total.reduceRegion(
                reducer   = ee.Reducer.mean(),
                geometry  = region,
                scale     = 5500,
                maxPixels = MAX_PIXELS,
                bestEffort= True,
            ).getInfo()
            return stats.get("precipitation")

        curr_precip = precip_90d(start_date, end_date)
        if curr_precip is None:
            return None

        hist = []
        for yr in range(curr_yr - baseline_years, curr_yr):
            try:
                bs = start_date.replace(year=yr)
                be = end_date.replace(year=yr)
            except ValueError:
                continue
            v = precip_90d(bs, be)
            if v is not None:
                hist.append(float(v))

        r = _zscore_from_series(float(curr_precip), hist)
        r["value"]   = round(float(curr_precip), 1)   # mm/90d
        r["anomaly"] = r["z_score"] is not None and (r["z_score"] < -1.0 or r["z_score"] > 2.0)
        return {"spi90": r}

    except Exception as e:
        logger.warning("GEE SPI90 %s: %s", poi["id"], e)
        return None


# ── Upsert ───────────────────────────────────────────────────────────────────

def _upsert_metric(session, poi_id: str, obs_date: date,
                   metric: str, data: dict):
    from models.market_data import GeeMetric
    if data is None or data.get("value") is None:
        return
    record = {
        "obs_date":       obs_date,
        "poi_id":         poi_id,
        "metric":         metric,
        "value":          data.get("value"),
        "z_score":        data.get("z_score"),
        "anomaly":        bool(data.get("anomaly", False)),
        "baseline_mean":  data.get("baseline_mean"),
        "baseline_std":   data.get("baseline_std"),
        "n_baseline_yrs": data.get("n_baseline_yrs"),
        "source":         "gee",
    }
    stmt = (
        pg_insert(GeeMetric)
        .values(**record)
        .on_conflict_do_update(
            constraint="uq_gee_date_poi_metric",
            set_={k: v for k, v in record.items()
                  if k not in ("obs_date", "poi_id", "metric")},
        )
    )
    session.execute(stmt)


# ── API pública ───────────────────────────────────────────────────────────────

def fetch_gee_crops(session, config_path: str = _CONFIG_PATH,
                    weeks_back: int = 2) -> dict:
    """
    Calcula y almacena métricas GEE para todos los POIs configurados.

    Para cada POI calcula: NDVI, NDWI (si Sentinel-2), LST (MODIS),
    SPI90 (CHIRPS), con z-score vs baseline 5 años.

    NOTA: Este módulo es lento (3-8 min por ejecución completa).
    Ejecutar desde scripts/run_gee_crops.py, NO desde daily_pipeline.

    Returns dict con:
      rows_upserted : int
      pois_processed: list[str]
      errors        : list[str]
    """
    from models.market_data import GeeMetric
    from models import Base
    from database import engine
    Base.metadata.create_all(engine, tables=[GeeMetric.__table__])

    result = {"rows_upserted": 0, "pois_processed": [], "errors": []}

    if not _init_ee():
        result["errors"].append("GEE no inicializado — ejecutar setup_gee.py")
        return result

    try:
        pois = _load_pois(config_path)
    except Exception as e:
        result["errors"].append(f"Error cargando config POIs: {e}")
        return result

    today    = date.today()
    total    = 0

    for poi in pois:
        poi_id  = poi["id"]
        metrics = poi.get("metrics", [])
        logger.info("GEE crops: procesando %s (%s)", poi_id, poi["name"])

        for w in range(weeks_back, 0, -1):
            # Semana de referencia (2 días de lag para S2)
            week_end   = today - timedelta(weeks=w - 1) - timedelta(days=2)
            week_start = week_end - timedelta(days=6)

            # Sentinel-2: NDVI + NDWI juntos
            needs_s2 = any(m in metrics for m in ("ndvi", "ndwi"))
            if needs_s2:
                s2_data = _get_s2_metrics(poi, week_start)
                if s2_data:
                    for metric in ("ndvi", "ndwi"):
                        if metric in metrics and s2_data.get(metric):
                            _upsert_metric(session, poi_id, week_start,
                                           metric, s2_data[metric])
                            total += 1
                            logger.info("  %s %s %s: %.4f (z=%s)",
                                        poi_id, metric, week_start,
                                        s2_data[metric].get("value", 0),
                                        s2_data[metric].get("z_score"))

            # MODIS LST
            if "lst" in metrics:
                lst_data = _get_lst_metrics(poi, week_start)
                if lst_data and lst_data.get("lst"):
                    _upsert_metric(session, poi_id, week_start,
                                   "lst", lst_data["lst"])
                    total += 1
                    logger.info("  %s lst %s: %.1f°C (z=%s)",
                                poi_id, week_start,
                                lst_data["lst"].get("value", 0),
                                lst_data["lst"].get("z_score"))

            # CHIRPS SPI90 (solo para la semana más reciente)
            if "spi90" in metrics and w == 1:
                spi_data = _get_spi90(poi, week_end)
                if spi_data and spi_data.get("spi90"):
                    _upsert_metric(session, poi_id, week_start,
                                   "spi90", spi_data["spi90"])
                    total += 1
                    logger.info("  %s spi90 %s: %.1fmm (z=%s)",
                                poi_id, week_start,
                                spi_data["spi90"].get("value", 0),
                                spi_data["spi90"].get("z_score"))

        result["pois_processed"].append(poi_id)

    session.commit()
    result["rows_upserted"] = total
    return result


def get_latest_gee_metrics(session, poi_ids: list = None) -> dict:
    """
    Lee las métricas GEE más recientes de la DB por POI.

    Returns dict: {poi_id: {metric: {value, z_score, anomaly, obs_date}}}
    """
    from sqlalchemy import text
    filter_clause = ""
    params: dict = {}
    if poi_ids:
        placeholders = ", ".join(f":p{i}" for i in range(len(poi_ids)))
        filter_clause = f"AND poi_id IN ({placeholders})"
        params = {f"p{i}": v for i, v in enumerate(poi_ids)}

    rows = session.execute(text(f"""
        SELECT DISTINCT ON (poi_id, metric)
               poi_id, metric, obs_date, value, z_score, anomaly,
               baseline_mean, baseline_std, n_baseline_yrs
        FROM gee_metric
        WHERE 1=1 {filter_clause}
        ORDER BY poi_id, metric, obs_date DESC
    """), params).fetchall()

    result: dict = {}
    for r in rows:
        poi_id, metric = r[0], r[1]
        if poi_id not in result:
            result[poi_id] = {}
        result[poi_id][metric] = {
            "obs_date":       str(r[2]),
            "value":          float(r[3]) if r[3] is not None else None,
            "z_score":        float(r[4]) if r[4] is not None else None,
            "anomaly":        bool(r[5]),
            "baseline_mean":  float(r[6]) if r[6] is not None else None,
            "baseline_std":   float(r[7]) if r[7] is not None else None,
            "n_baseline_yrs": r[8],
        }
    return result
