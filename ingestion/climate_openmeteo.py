"""
Descarga datos climáticos diarios del cinturón azucarero São Paulo
via Open-Meteo ERA5 Reanalysis (gratuito, sin API key).

Variables descargadas por estación:
  precipitation_sum           — precipitación diaria (mm)
  et0_fao_evapotranspiration  — ET0 FAO-56 Penman-Monteith (mm)
  temperature_2m_max / min    — temperatura máxima/mínima (°C)
  soil_moisture_0_to_7cm_mean — humedad suelo 0-7 cm (m³/m³)

API ERA5 histórico: archive-api.open-meteo.com (lag ~5 días)
Estaciones: Ribeirão Preto (-21.1767, -47.8208)
            Piracicaba     (-22.7253, -47.6492)

Nota: para la primera carga histórica completa usar:
  scripts/climate_historical_import.py
El pipeline diario actualiza solo los últimos 10 días.
"""
import logging
from datetime import date, timedelta
from typing import Optional

import requests
from sqlalchemy.dialects.postgresql import insert

logger = logging.getLogger(__name__)

ERA5_URL  = "https://archive-api.open-meteo.com/v1/archive"
DAILY_VARS = ",".join([
    "precipitation_sum",
    "et0_fao_evapotranspiration",
    "temperature_2m_max",
    "temperature_2m_min",
    "soil_moisture_0_to_7cm_mean",
])


def _fetch_station(station: dict, start: date, end: date, timeout: int = 30) -> Optional[dict]:
    """
    Llama a la API ERA5 de Open-Meteo para una estación y rango de fechas.
    Retorna dict {date_str: {precip, et0, tmax, tmin, soil}} o None si error.
    """
    params = {
        "latitude":  station["lat"],
        "longitude": station["lon"],
        "start_date": str(start),
        "end_date":   str(end),
        "daily":      DAILY_VARS,
        "timezone":   "America/Sao_Paulo",
    }
    try:
        resp = requests.get(ERA5_URL, params=params, timeout=timeout,
                            headers={"User-Agent": "SGT-Trading/1.0"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("climate_openmeteo fetch %s: %s", station["name"], e)
        return None

    daily = data.get("daily", {})
    dates  = daily.get("time", [])
    precip = daily.get("precipitation_sum", [])
    et0    = daily.get("et0_fao_evapotranspiration", [])
    tmax   = daily.get("temperature_2m_max", [])
    tmin   = daily.get("temperature_2m_min", [])
    soil   = daily.get("soil_moisture_0_to_7cm_mean", [])

    result = {}
    for i, d in enumerate(dates):
        result[d] = {
            "precip":  precip[i] if i < len(precip) else None,
            "et0":     et0[i]    if i < len(et0)    else None,
            "tmax":    tmax[i]   if i < len(tmax)   else None,
            "tmin":    tmin[i]   if i < len(tmin)   else None,
            "soil":    soil[i]   if i < len(soil)   else None,
        }
    return result


def fetch_climate(session, days_back: int = 10) -> dict:
    """
    Actualiza climate_daily con datos Open-Meteo ERA5 para las últimas
    `days_back` días de cada estación configurada.

    Returns dict con claves:
      rows_upserted : int
      stations      : list[str]
      latest_date   : str
      errors        : list[str]
    """
    from models.market_data import ClimateDaily
    from models import Base
    from database import engine
    from config import CLIMATE_STATIONS

    Base.metadata.create_all(engine, tables=[ClimateDaily.__table__])

    today  = date.today()
    # ERA5 tiene lag de ~5 días; terminamos en hace 6 días para estar seguros
    end_d  = today - timedelta(days=6)
    start_d = end_d - timedelta(days=days_back)

    result = {
        "rows_upserted": 0,
        "stations":      [],
        "latest_date":   str(end_d),
        "errors":        [],
    }

    for station in CLIMATE_STATIONS:
        name = station["name"]
        data = _fetch_station(station, start_d, end_d)
        if data is None:
            result["errors"].append(f"{name}: fetch failed")
            continue

        count = 0
        for date_str, vals in data.items():
            if vals["precip"] is None and vals["et0"] is None:
                continue
            try:
                obs = date.fromisoformat(date_str)
            except ValueError:
                continue

            stmt = (
                insert(ClimateDaily)
                .values(
                    obs_date     = obs,
                    station_name = name,
                    latitude     = station["lat"],
                    longitude    = station["lon"],
                    precip_mm    = vals["precip"],
                    et0_mm       = vals["et0"],
                    temp_max_c   = vals["tmax"],
                    temp_min_c   = vals["tmin"],
                    soil_moisture= vals["soil"],
                    source       = "open_meteo_era5",
                )
                .on_conflict_do_update(
                    constraint = "uq_climate_date_station",
                    set_       = {
                        "precip_mm":    vals["precip"],
                        "et0_mm":       vals["et0"],
                        "temp_max_c":   vals["tmax"],
                        "temp_min_c":   vals["tmin"],
                        "soil_moisture": vals["soil"],
                    },
                )
            )
            session.execute(stmt)
            count += 1

        session.commit()
        result["rows_upserted"] += count
        result["stations"].append(name)
        logger.info("climate %s: %d días actualizados", name, count)

    return result


def get_climate_rolling(session, station_name: str, days: int = 95) -> Optional["pd.DataFrame"]:
    """
    Retorna DataFrame con columnas [obs_date, precip_mm, et0_mm, soil_moisture]
    de los últimos `days` días de una estación.
    """
    try:
        import pandas as pd
        from sqlalchemy import text
        from datetime import timedelta

        cutoff = date.today() - timedelta(days=days)
        rows = session.execute(text("""
            SELECT obs_date, precip_mm, et0_mm, soil_moisture
            FROM climate_daily
            WHERE station_name = :st AND obs_date >= :cutoff
            ORDER BY obs_date
        """), {"st": station_name, "cutoff": cutoff}).fetchall()

        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["obs_date", "precip_mm", "et0_mm", "soil_moisture"])
        df["obs_date"] = pd.to_datetime(df["obs_date"])
        for col in ["precip_mm", "et0_mm", "soil_moisture"]:
            df[col] = df[col].astype(float)
        return df.set_index("obs_date").sort_index()
    except Exception as e:
        logger.warning("get_climate_rolling: %s", e)
        return None
