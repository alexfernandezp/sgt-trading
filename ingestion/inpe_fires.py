"""
Focos de incendio diarios — INPE/Terrabrasilis AMS.

Fuente: Terrabrasilis WFS GeoServer (OGC WFS 2.0).
  Base: https://terrabrasilis.dpi.inpe.br/geoserver/ows
  Capa: ams1h:active-fire-today (focos activos detectados hoy, multi-satélite)

Filtro geográfico BBOX para São Paulo + Paraná (cinturón azucarero):
  lon_min=-54.6  lat_min=-26.7  lon_max=-44.1  lat_max=-19.7

Nota: La capa solo publica datos del día en curso (sin histórico API).
  El módulo acumula observaciones diarias en DB para construir un
  baseline rolling de 30 días.

Señal: focos del día vs baseline rolling (últimos 30 días).
  z > +1.0  → focos anómalos altos → sequía/estrés hídrico → alcista azúcar
  z < -1.5  → focos muy bajos → condiciones húmedas → bajista azúcar
"""
import logging
from datetime import date
from typing import Optional

import requests
from sqlalchemy.dialects.postgresql import insert

logger = logging.getLogger(__name__)

WFS_URL   = "https://terrabrasilis.dpi.inpe.br/geoserver/ows"
WFS_LAYER = "ams1h:active-fire-today"

# BBOX SP + PR: lon_min,lat_min,lon_max,lat_max,EPSG:4326
# Nota: WFS 2.0 con EPSG:4326 explícito usa orden lon/lat (comportamiento
# de este servidor Terrabrasilis; sin SRS usa lat/lon estándar)
SPPR_BBOX = "-54.6,-26.7,-44.1,-19.7,EPSG:4326"
REGION    = "SP+PR"
SATELLITE = "MULTI"


def _fetch_today_sppr(timeout: int = 60) -> Optional[int]:
    """Descarga focos de hoy en región SP+PR via BBOX. Devuelve conteo o None."""
    params = {
        "service":      "WFS",
        "version":      "2.0.0",
        "request":      "GetFeature",
        "typeName":     WFS_LAYER,
        "BBOX":         SPPR_BBOX,
        "outputFormat": "application/json",
        "count":        100000,
    }
    try:
        r = requests.get(WFS_URL, params=params, timeout=timeout,
                         headers={"User-Agent": "SGT-Trading/1.0"})
        r.raise_for_status()
        data = r.json()
        n = len(data.get("features", []))
        logger.info("INPE fires SP+PR: %d focos hoy", n)
        return n
    except Exception as e:
        logger.warning("inpe_fires WFS BBOX: %s", e)
        return None


def fetch_fires(session, days_back: int = 7) -> dict:
    """
    Obtiene focos de incendio del día actual para SP+PR y almacena en DB.

    Nota: ams1h:active-fire-today solo publica datos del día en curso.
    days_back se mantiene por compatibilidad de firma pero no se usa para
    consultas históricas (el WFS no soporta filtrado por fecha).

    Returns dict con:
      rows_upserted : int
      latest_date   : str
      latest_counts : dict {"SP+PR": n}
      errors        : list[str]
    """
    from models.market_data import InpeFire
    from models import Base
    from database import engine
    from sqlalchemy import text

    # Ampliar columna state si la tabla ya existe con VARCHAR(2)
    try:
        session.execute(text(
            "ALTER TABLE inpe_fire ALTER COLUMN state TYPE VARCHAR(10)"
        ))
        session.commit()
    except Exception:
        session.rollback()

    Base.metadata.create_all(engine, tables=[InpeFire.__table__])

    result = {"rows_upserted": 0, "latest_date": None,
              "latest_counts": {}, "errors": []}

    count = _fetch_today_sppr()
    if count is None:
        result["errors"].append(
            "WFS ams1h:active-fire-today no disponible — sin datos de focos"
        )
        return result

    today  = date.today()
    record = {
        "obs_date":   today,
        "state":      REGION,
        "fire_count": count,
        "satellite":  SATELLITE,
        "source":     "ams1h_terrabrasilis",
    }
    stmt = (
        insert(InpeFire)
        .values(**record)
        .on_conflict_do_update(
            constraint="uq_inpe_date_state_sat",
            set_={"fire_count": count},
        )
    )
    try:
        session.execute(stmt)
        session.commit()
        result["rows_upserted"] = 1
        result["latest_date"]   = str(today)
        result["latest_counts"] = {REGION: count}
    except Exception as e:
        logger.warning("inpe_fires upsert: %s", e)
        session.rollback()
        result["errors"].append(str(e))

    return result


def get_fire_baseline(session, state: str = REGION,
                      lookback_days: int = 30) -> Optional[dict]:
    """
    Baseline rolling de focos diarios (últimos lookback_days días).
    Requiere mínimo 7 días acumulados en DB.

    Returns dict con:
      state               : región (ej. "SP+PR")
      current_count       : focos hoy
      baseline_mean       : media de los días anteriores en la ventana
      baseline_std        : desviación estándar
      z_score             : (current - mean) / std
      days_available      : días con datos
      current_month_total : alias de current_count (compatibilidad fire_signal)
      month               : mes actual (1-12)
    """
    from sqlalchemy import text
    today = date.today()

    rows = session.execute(text("""
        SELECT obs_date, fire_count
        FROM inpe_fire
        WHERE state = :st
          AND obs_date >= CURRENT_DATE - :days * INTERVAL '1 day'
        ORDER BY obs_date DESC
    """), {"st": state, "days": lookback_days}).fetchall()

    if len(rows) < 7:
        return None

    current_count = int(rows[0][1])
    baseline_vals = [float(r[1]) for r in rows[1:]]
    if len(baseline_vals) < 6:
        return None

    import statistics
    mean = statistics.mean(baseline_vals)
    std  = max(
        statistics.stdev(baseline_vals) if len(baseline_vals) > 1 else 0.0,
        1.0,
    )
    z = (float(current_count) - mean) / std

    return {
        "state":               state,
        "current_count":       current_count,
        "baseline_mean":       round(mean, 1),
        "baseline_std":        round(std, 1),
        "z_score":             round(z, 2),
        "days_available":      len(rows),
        "current_month_total": current_count,
        "month":               today.month,
    }
