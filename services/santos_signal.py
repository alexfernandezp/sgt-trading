"""
Señal A5: Cola de exportación de azúcar en el Puerto de Santos.

Lógica:
  Cola grande (ships + tonelaje > media histórica) → exportación bajo presión física
  → oferta no llega al mercado eficientemente → sesgo LONG (soporte precio).

  Cola pequeña / flujo normal → oferta fluyendo bien → sesgo SHORT.

Métricas usadas:
  n_ships_total   : expected(Long) + scheduled + berthed
  tonnage_total   : tonnage_expected + tonnage_berthed
  z_ships         : (n_ships - mean30d) / std30d
  z_tonnage       : (tonnage - mean30d) / std30d

Signal: promedio ponderado de z_ships(40%) + z_tonnage(60%), normalizado a [-1, +1].
"""
import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SHIP_WEIGHT    = 0.40
TONNAGE_WEIGHT = 0.60
LOOKBACK_DAYS  = 30
SIGNAL_THRESHOLD = 0.5   # z-score mínimo para señal no neutral


def compute_santos_signal(session: Session, snapshot: Optional[dict] = None) -> Optional[dict]:
    """
    Calcula la señal de cola de exportación de azúcar en Santos.

    Args:
      session  : DB session
      snapshot : resultado de fetch_santos_port() o get_latest_snapshot().
                 Si None, lee el último snapshot de DB.

    Devuelve:
      signal_a5   : float [-1, +1]  (+1 = cola grande = alcista)
      bias        : "LONG" / "SHORT" / "NEUTRAL"
      n_ships     : total barcos azúcar en pipeline (expected+sched+berthed)
      tonnage     : tonelaje total en pipeline
      mean_ships  : media 30d
      mean_tonnage: media 30d
      z_ships     : z-score barcos
      z_tonnage   : z-score tonelaje
      description : texto para score card
    """
    if snapshot is None:
        from ingestion.santos_port import get_latest_snapshot
        snapshot = get_latest_snapshot(session)

    if snapshot is None:
        logger.warning("santos_signal: sin datos en DB (ejecutar fetch_santos_port)")
        return None

    n_ships  = snapshot["n_expected"] + snapshot["n_scheduled"] + snapshot["n_berthed"]
    tonnage  = snapshot["tonnage_expected"] + snapshot["tonnage_berthed"]
    snap_dt  = snapshot.get("snapshot_date", "?")

    # Histórico 30 días
    hist = session.execute(text("""
        SELECT snapshot_date,
               SUM(CASE WHEN page = 'expected' AND nav_type = 'Long' THEN 1
                        WHEN page IN ('scheduled', 'berthed')         THEN 1
                        ELSE 0 END) AS n_ships,
               SUM(CASE WHEN page = 'expected' AND nav_type = 'Long' THEN COALESCE(weight_t, 0)
                        WHEN page = 'berthed'                          THEN COALESCE(load_qty_t, 0)
                        ELSE 0 END) AS tonnage
        FROM santos_port_snapshot
        WHERE snapshot_date >= CURRENT_DATE - INTERVAL ':days days'
        GROUP BY snapshot_date
        ORDER BY snapshot_date
    """.replace(":days", str(LOOKBACK_DAYS)))).fetchall()

    # Necesitamos ≥5 días para estadística útil
    if len(hist) < 5:
        bias = "NEUTRAL"
        desc = (
            "Santos: %d barcos ACUCAR (%d exp Long, %d sched, %d berthed) | "
            "%.0f t pipeline | Historial insuficiente (<5d)" % (
                n_ships,
                snapshot["n_expected"], snapshot["n_scheduled"], snapshot["n_berthed"],
                tonnage)
        )
        return {
            "signal_a5": 0.0, "bias": bias,
            "n_ships": n_ships, "tonnage": tonnage,
            "mean_ships": None, "mean_tonnage": None,
            "z_ships": None, "z_tonnage": None,
            "description": desc, "snapshot_date": snap_dt,
            "n_expected": snapshot["n_expected"],
            "n_scheduled": snapshot["n_scheduled"],
            "n_berthed": snapshot["n_berthed"],
        }

    hist_ships   = [float(r[1] or 0) for r in hist]
    hist_tonnage = [float(r[2] or 0) for r in hist]

    mean_ships   = sum(hist_ships)   / len(hist_ships)
    mean_tonnage = sum(hist_tonnage) / len(hist_tonnage)

    import statistics
    std_ships   = statistics.stdev(hist_ships)   if len(hist_ships)   > 1 else 1.0
    std_tonnage = statistics.stdev(hist_tonnage) if len(hist_tonnage) > 1 else 1.0

    std_ships   = max(std_ships,   0.5)   # evitar división por casi-cero
    std_tonnage = max(std_tonnage, 100.0)

    z_ships   = (n_ships  - mean_ships)   / std_ships
    z_tonnage = (tonnage  - mean_tonnage) / std_tonnage

    # Señal ponderada, clampeada a [-1, +1]
    z_combined = SHIP_WEIGHT * z_ships + TONNAGE_WEIGHT * z_tonnage
    signal_a5  = max(-1.0, min(1.0, z_combined / 2.0))   # /2 para normalizar z típicos

    if z_combined >= SIGNAL_THRESHOLD:
        bias = "LONG"
    elif z_combined <= -SIGNAL_THRESHOLD:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    direction_txt = {
        "LONG":    "↑ cola GRANDE → congestión → presión alcista",
        "SHORT":   "↓ cola PEQUEÑA → flujo libre → presión bajista",
        "NEUTRAL": "→ cola en rango normal",
    }[bias]

    desc = (
        "Santos A5: %d barcos (%d exp, %d sched, %d berthed) | "
        "%.0f t | z=%.2f | %s" % (
            n_ships,
            snapshot["n_expected"], snapshot["n_scheduled"], snapshot["n_berthed"],
            tonnage, z_combined, direction_txt)
    )

    return {
        "signal_a5":    round(signal_a5, 3),
        "bias":         bias,
        "z_combined":   round(z_combined, 3),
        "z_ships":      round(z_ships, 3),
        "z_tonnage":    round(z_tonnage, 3),
        "n_ships":      n_ships,
        "tonnage":      tonnage,
        "mean_ships":   round(mean_ships, 1),
        "mean_tonnage": round(mean_tonnage, 0),
        "n_expected":   snapshot["n_expected"],
        "n_scheduled":  snapshot["n_scheduled"],
        "n_berthed":    snapshot["n_berthed"],
        "description":  desc,
        "snapshot_date": snap_dt,
    }
