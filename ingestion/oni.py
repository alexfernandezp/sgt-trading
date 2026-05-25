"""
Descarga e importa el Índice ONI (Oceanic Niño Index) de NOAA/CPC.

Fuente: https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt
Formato: texto de ancho fijo — columnas SEAS, YR, ANOM

El ONI es la media móvil de 3 meses de la anomalía de temperatura
superficial del mar en la región Niño 3.4 (5°N–5°S, 120°–170°W).
ENSO classification thresholds (CPC standard):
  ≥ +2.0 → Very Strong El Niño
  +1.5 to +2.0 → Strong El Niño
  +1.0 to +1.5 → Moderate El Niño
  +0.5 to +1.0 → Weak El Niño
  −0.5 to +0.5 → Neutral
  −0.5 to −1.0 → Weak La Niña
  −1.0 to −1.5 → Moderate La Niña
  ≤ −1.5       → Strong La Niña

Impacto en azúcar Brasil (CS):
  El Niño fuerte → sequía SP/PR → restricción producción → alcista SB
  La Niña fuerte → lluvias excesivas/buenas → mayor producción → bajista SB
"""
import logging
from datetime import date
from typing import Optional

import pandas as pd
import requests
from sqlalchemy.dialects.postgresql import insert

logger = logging.getLogger(__name__)

ONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"

# Temporada → mes central (1-12)
SEASON_TO_MONTH = {
    "DJF": 1,  "JFM": 2,  "FMA": 3,  "MAM": 4,
    "AMJ": 5,  "MJJ": 6,  "JJA": 7,  "JAS": 8,
    "ASO": 9,  "SON": 10, "OND": 11, "NDJ": 12,
}


def _classify(oni: float) -> str:
    if oni >= 2.0:   return "VERY_STRONG_NINO"
    if oni >= 1.5:   return "STRONG_NINO"
    if oni >= 1.0:   return "MODERATE_NINO"
    if oni >= 0.5:   return "WEAK_NINO"
    if oni <= -1.5:  return "STRONG_NINA"
    if oni <= -1.0:  return "MODERATE_NINA"
    if oni <= -0.5:  return "WEAK_NINA"
    return "NEUTRAL"


def fetch_oni(session, timeout: int = 30) -> dict:
    """
    Descarga el archivo ONI de NOAA y actualiza la tabla oni_index.

    Returns dict con claves:
      rows_upserted : int
      latest_oni    : float (valor más reciente)
      latest_season : str  (temporada más reciente, ej. 'DJF')
      latest_year   : int
      classification: str
      errors        : list[str]
    """
    from models.market_data import OniIndex
    from models import Base
    from database import engine

    Base.metadata.create_all(engine, tables=[OniIndex.__table__])

    result = {
        "rows_upserted": 0,
        "latest_oni":     None,
        "latest_season":  None,
        "latest_year":    None,
        "classification": None,
        "errors":         [],
    }

    try:
        resp = requests.get(ONI_URL, timeout=timeout,
                            headers={"User-Agent": "SGT-Trading/1.0"})
        resp.raise_for_status()
        content = resp.text
    except Exception as e:
        result["errors"].append(f"download: {e}")
        logger.error("ONI download failed: %s", e)
        return result

    try:
        from io import StringIO
        # Archivo NOAA tiene 4 columnas: SEAS  YR  TOTAL  ANOM
        # TOTAL = temperatura absoluta (~25°C); ANOM = anomalía (lo que usamos)
        df = pd.read_csv(
            StringIO(content),
            sep=r"\s+",
            header=0,
            names=["season", "year", "total", "oni"],
        )
        df["year"] = pd.to_numeric(df["year"], errors="coerce")
        df["oni"]  = pd.to_numeric(df["oni"],  errors="coerce")
        df = df.dropna(subset=["season", "year", "oni"])
        df = df[df["season"].isin(SEASON_TO_MONTH)]
        # Excluir valores de relleno NOAA (−99.9, 9999, etc.)
        df = df[df["oni"].abs() < 10.0]
        df["year"] = df["year"].astype(int)
        df["oni"]  = df["oni"].astype(float)
    except Exception as e:
        result["errors"].append(f"parse: {e}")
        logger.error("ONI parse failed: %s", e)
        return result

    rows_done = 0
    for _, row in df.iterrows():
        season = str(row["season"]).strip()
        year   = int(row["year"])
        oni    = float(row["oni"])
        month  = SEASON_TO_MONTH.get(season)
        if month is None:
            continue

        obs_date = date(year, month, 1)
        cls      = _classify(oni)

        stmt = (
            insert(OniIndex)
            .values(
                obs_date       = obs_date,
                season         = season,
                year           = year,
                month          = month,
                oni_value      = oni,
                classification = cls,
                source         = "noaa_cpc",
            )
            .on_conflict_do_update(
                constraint = "uq_oni_year_month",
                set_       = {"oni_value": oni, "classification": cls},
            )
        )
        session.execute(stmt)
        rows_done += 1

    session.commit()

    # Última fila disponible
    last = df.iloc[-1]
    result["rows_upserted"] = rows_done
    result["latest_oni"]    = float(last["oni"])
    result["latest_season"] = str(last["season"])
    result["latest_year"]   = int(last["year"])
    result["classification"] = _classify(float(last["oni"]))

    logger.info("ONI: %d filas actualizadas — último: %s %d = %.2f (%s)",
                rows_done, result["latest_season"], result["latest_year"],
                result["latest_oni"], result["classification"])
    return result


def get_latest_oni(session) -> Optional[dict]:
    """Retorna el registro ONI más reciente de la DB."""
    try:
        from sqlalchemy import text
        row = session.execute(text("""
            SELECT obs_date, season, year, month, oni_value, classification
            FROM oni_index ORDER BY obs_date DESC LIMIT 1
        """)).fetchone()
        if row:
            return {
                "obs_date":      str(row[0]),
                "season":        row[1],
                "year":          row[2],
                "month":         row[3],
                "oni_value":     float(row[4]),
                "classification": row[5],
            }
    except Exception as e:
        logger.warning("get_latest_oni: %s", e)
    return None
