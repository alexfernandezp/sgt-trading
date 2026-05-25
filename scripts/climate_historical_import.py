"""
Carga histórica de datos climáticos ERA5 desde Open-Meteo.
Ejecutar UNA sola vez para poblar los últimos 5 años.

Uso: py scripts/climate_historical_import.py

El pipeline diario (daily_pipeline.py) actualiza solo los últimos 10 días.
Este script carga desde 2020-01-01 hasta ayer para tener histórico completo.
"""
import sys, os, logging
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-8s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("climate_import")

from datetime import date, timedelta
import requests
from sqlalchemy.dialects.postgresql import insert

from database import SessionLocal, engine
from models import Base
from models.market_data import ClimateDaily
from config import CLIMATE_STATIONS

# ERA5 disponible desde 1940; usamos 2018-01-01 (7 años de histórico)
START_DATE = date(2018, 1, 1)
END_DATE   = date.today() - timedelta(days=6)  # lag ERA5 ~5 días

ERA5_URL  = "https://archive-api.open-meteo.com/v1/archive"
DAILY_VARS = ",".join([
    "precipitation_sum",
    "et0_fao_evapotranspiration",
    "temperature_2m_max",
    "temperature_2m_min",
    "soil_moisture_0_to_7cm_mean",
])

Base.metadata.create_all(engine, tables=[ClimateDaily.__table__])


def load_station(session, station: dict) -> int:
    """Descarga y guarda datos completos para una estación. Retorna filas insertadas."""
    name = station["name"]
    logger.info("Estación: %s (%s, %s)", name, station["lat"], station["lon"])

    params = {
        "latitude":   station["lat"],
        "longitude":  station["lon"],
        "start_date": str(START_DATE),
        "end_date":   str(END_DATE),
        "daily":      DAILY_VARS,
        "timezone":   "America/Sao_Paulo",
    }

    try:
        resp = requests.get(ERA5_URL, params=params, timeout=120,
                            headers={"User-Agent": "SGT-Trading/1.0"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error("  Error descargando %s: %s", name, e)
        return 0

    daily = data.get("daily", {})
    dates  = daily.get("time", [])
    precip = daily.get("precipitation_sum", [])
    et0    = daily.get("et0_fao_evapotranspiration", [])
    tmax   = daily.get("temperature_2m_max", [])
    tmin   = daily.get("temperature_2m_min", [])
    soil   = daily.get("soil_moisture_0_to_7cm_mean", [])

    count = 0
    batch = []
    for i, d in enumerate(dates):
        pval = precip[i] if i < len(precip) else None
        eval_ = et0[i]   if i < len(et0)    else None
        if pval is None and eval_ is None:
            continue

        batch.append({
            "obs_date":     date.fromisoformat(d),
            "station_name": name,
            "latitude":     station["lat"],
            "longitude":    station["lon"],
            "precip_mm":    pval,
            "et0_mm":       eval_,
            "temp_max_c":   tmax[i] if i < len(tmax) else None,
            "temp_min_c":   tmin[i] if i < len(tmin) else None,
            "soil_moisture": soil[i] if i < len(soil) else None,
            "source":       "open_meteo_era5",
        })
        count += 1

        # Batch inserts cada 500 filas
        if len(batch) >= 500:
            stmt = insert(ClimateDaily).values(batch)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_climate_date_station",
                set_={"precip_mm": stmt.excluded.precip_mm,
                      "et0_mm":    stmt.excluded.et0_mm,
                      "temp_max_c": stmt.excluded.temp_max_c,
                      "temp_min_c": stmt.excluded.temp_min_c,
                      "soil_moisture": stmt.excluded.soil_moisture},
            )
            session.execute(stmt)
            session.commit()
            batch = []

    if batch:
        stmt = insert(ClimateDaily).values(batch)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_climate_date_station",
            set_={"precip_mm": stmt.excluded.precip_mm,
                  "et0_mm":    stmt.excluded.et0_mm,
                  "temp_max_c": stmt.excluded.temp_max_c,
                  "temp_min_c": stmt.excluded.temp_min_c,
                  "soil_moisture": stmt.excluded.soil_moisture},
        )
        session.execute(stmt)
        session.commit()

    logger.info("  %s: %d días importados (%s → %s)", name, count, START_DATE, END_DATE)
    return count


def run():
    session = SessionLocal()
    total = 0
    try:
        for station in CLIMATE_STATIONS:
            total += load_station(session, station)
    finally:
        session.close()

    logger.info("Total filas importadas: %d", total)
    logger.info("Para ver señal actual: py scripts/score_today.py")


if __name__ == "__main__":
    run()
