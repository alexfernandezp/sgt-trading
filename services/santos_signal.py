"""
Señal A5: Cola de exportación de azúcar en el Puerto de Santos.

Tres componentes combinados:

  1. NIVEL (55%) — z-score del tamaño de cola vs media 30d
       Cola grande → congestión → oferta sin llegar al mercado → LONG

  2. VELOCIDAD DE CARGA (30%) — ratio berthed/(berthed+expected) vs media 30d
       Ratio bajo → muchos barcos esperando, pocos cargando → cuello de botella
       que el mercado aún no refleja → señal adelantada LONG
       Ratio alto → flujo libre → oferta llegando → presión SHORT

  3. CRECIMIENTO DE COLA 7d (15%) — Δ expected vs hace 7 días
       Cola expected creciendo → oferta acumulándose → LONG
       Cola expected cayendo → demanda absorbiendo bien → SHORT

Signal combinada: [-1, +1]  →  LONG / NEUTRAL / SHORT
"""
import logging
import statistics
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

LOOKBACK_DAYS    = 30
SIGNAL_THRESHOLD = 0.50   # z combinada mínima para señal no neutral
MIN_HIST_DAYS    = 5      # mínimo de días para activar estadística

# Pesos de los tres componentes
WEIGHT_LEVEL    = 0.55
WEIGHT_VELOCITY = 0.30
WEIGHT_DELTA    = 0.15


def compute_santos_signal(session: Session, snapshot: Optional[dict] = None) -> Optional[dict]:
    """
    Calcula la señal de cola de exportación de azúcar en Santos.

    Args:
      session  : DB session
      snapshot : resultado de fetch_santos_port() o get_latest_snapshot().
                 Si None, lee el último snapshot de DB.

    Devuelve dict con:
      signal_a5, bias, z_combined
      n_ships, tonnage, n_expected, n_scheduled, n_berthed
      mean_ships, mean_tonnage
      z_ships, z_tonnage
      velocity_ratio       — berthed / (berthed + expected) actual
      velocity_ratio_mean  — media 30d
      z_velocity           — z-score velocidad (negativo = cuello de botella = alcista)
      queue_delta_7d       — Δ barcos expected vs hace 7 días
      z_queue_delta        — z-score de ese delta
      description
    """
    if snapshot is None:
        from ingestion.santos_port import get_latest_snapshot
        snapshot = get_latest_snapshot(session)

    if snapshot is None:
        logger.warning("santos_signal: sin datos en DB (ejecutar fetch_santos_port)")
        return None

    n_exp   = snapshot["n_expected"]
    n_sch   = snapshot["n_scheduled"]
    n_ber   = snapshot["n_berthed"]
    n_ships = n_exp + n_sch + n_ber
    tonnage = snapshot["tonnage_expected"] + snapshot["tonnage_berthed"]
    snap_dt = snapshot.get("snapshot_date", "?")

    # ── Histórico 30 días ────────────────────────────────────────────────────
    # Trae por día: n_ships total, tonnage, n_berthed, n_expected_long
    hist = session.execute(text("""
        SELECT snapshot_date,
               SUM(CASE WHEN page = 'expected' AND nav_type = 'Long' THEN 1
                        WHEN page IN ('scheduled', 'berthed')         THEN 1
                        ELSE 0 END)                                           AS n_ships,
               SUM(CASE WHEN page = 'expected' AND nav_type = 'Long' THEN COALESCE(weight_t, 0)
                        WHEN page = 'berthed'                          THEN COALESCE(load_qty_t, 0)
                        ELSE 0 END)                                           AS tonnage,
               SUM(CASE WHEN page = 'berthed' THEN 1 ELSE 0 END)             AS n_bert,
               SUM(CASE WHEN page = 'expected' AND nav_type = 'Long' THEN 1
                        ELSE 0 END)                                           AS n_exp_long
        FROM santos_port_snapshot
        WHERE snapshot_date >= CURRENT_DATE - INTERVAL '30 days'
        GROUP BY snapshot_date
        ORDER BY snapshot_date
    """)).fetchall()

    # Insuficiente histórico
    if len(hist) < MIN_HIST_DAYS:
        desc = (
            "Santos: %d barcos ACUCAR (%d exp Long, %d sched, %d berthed) | "
            "%.0f t pipeline | Historial insuficiente (<%dd)" % (
                n_ships, n_exp, n_sch, n_ber, tonnage, MIN_HIST_DAYS)
        )
        return {
            "signal_a5": 0.0, "bias": "NEUTRAL",
            "n_ships": n_ships, "tonnage": tonnage,
            "n_expected": n_exp, "n_scheduled": n_sch, "n_berthed": n_ber,
            "mean_ships": None, "mean_tonnage": None,
            "z_ships": None, "z_tonnage": None, "z_combined": None,
            "velocity_ratio": None, "velocity_ratio_mean": None, "z_velocity": None,
            "queue_delta_7d": None, "z_queue_delta": None,
            "description": desc, "snapshot_date": snap_dt,
        }

    hist_ships    = [float(r[1] or 0) for r in hist]
    hist_tonnage  = [float(r[2] or 0) for r in hist]
    hist_berthed  = [float(r[3] or 0) for r in hist]
    hist_expected = [float(r[4] or 0) for r in hist]
    hist_dates    = [r[0] for r in hist]

    # ── Componente 1: Nivel (existente) ─────────────────────────────────────
    mean_ships   = sum(hist_ships)   / len(hist_ships)
    mean_tonnage = sum(hist_tonnage) / len(hist_tonnage)

    std_ships   = max(statistics.stdev(hist_ships)   if len(hist_ships)   > 1 else 1.0, 0.5)
    std_tonnage = max(statistics.stdev(hist_tonnage) if len(hist_tonnage) > 1 else 1.0, 100.0)

    z_ships   = (n_ships  - mean_ships)   / std_ships
    z_tonnage = (tonnage  - mean_tonnage) / std_tonnage
    z_level   = 0.40 * z_ships + 0.60 * z_tonnage

    # ── Componente 2: Velocidad de carga ────────────────────────────────────
    # velocity_ratio = berthed / (berthed + expected_long)
    # Ratio BAJO → bottleneck → alcista (señal invertida en la combinación)
    hist_velocity = [
        b / max(1.0, b + e)
        for b, e in zip(hist_berthed, hist_expected)
    ]
    mean_velocity = sum(hist_velocity) / len(hist_velocity)
    std_velocity  = max(
        statistics.stdev(hist_velocity) if len(hist_velocity) > 1 else 0.05,
        0.05
    )

    curr_velocity  = n_ber / max(1.0, n_ber + n_exp)
    z_velocity_raw = (curr_velocity - mean_velocity) / std_velocity
    # Invertir: ratio bajo (bottleneck) = LONG → contribución positiva
    z_velocity_sig = -z_velocity_raw

    # ── Componente 3: Crecimiento de cola 7d ────────────────────────────────
    std_expected = max(
        statistics.stdev(hist_expected) if len(hist_expected) > 1 else 1.0,
        0.5
    )

    cutoff_7d   = date.today() - timedelta(days=7)
    past_exp    = [(d, e) for d, e in zip(hist_dates, hist_expected) if d <= cutoff_7d]
    queue_delta_7d = None
    z_queue_delta  = 0.0

    if past_exp:
        n_exp_7d_ago   = past_exp[-1][1]   # más reciente de los que tienen ≥7 días
        queue_delta_7d = n_exp - n_exp_7d_ago
        z_queue_delta  = queue_delta_7d / std_expected

    # ── Combinación ponderada ────────────────────────────────────────────────
    # Si no hay dato de delta (menos de 7 días de historia), redistribuir pesos
    if queue_delta_7d is None:
        w_lev = WEIGHT_LEVEL + WEIGHT_DELTA / 2
        w_vel = WEIGHT_VELOCITY + WEIGHT_DELTA / 2
        w_del = 0.0
    else:
        w_lev = WEIGHT_LEVEL
        w_vel = WEIGHT_VELOCITY
        w_del = WEIGHT_DELTA

    z_combined = w_lev * z_level + w_vel * z_velocity_sig + w_del * z_queue_delta
    signal_a5  = max(-1.0, min(1.0, z_combined / 2.0))

    if z_combined >= SIGNAL_THRESHOLD:
        bias = "LONG"
    elif z_combined <= -SIGNAL_THRESHOLD:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    # ── Descripción ─────────────────────────────────────────────────────────
    vel_pct  = curr_velocity * 100
    vel_m_pct = mean_velocity * 100
    vel_label = ""
    if z_velocity_raw < -0.5:
        vel_label = "↑ bottleneck"
    elif z_velocity_raw > 0.5:
        vel_label = "↓ fluido"

    delta_str = ""
    if queue_delta_7d is not None:
        delta_str = "  Δ7d_exp=%+d" % int(queue_delta_7d)

    desc = (
        "Santos A5: %d barcos (%d exp, %d sched, %d berthed) | "
        "%.0f t | z=%.2f | velocidad=%.0f%% (media=%.0f%%)%s%s%s" % (
            n_ships, n_exp, n_sch, n_ber,
            tonnage, z_combined,
            vel_pct, vel_m_pct, delta_str,
            "  " + vel_label if vel_label else "",
            {
                "LONG":    " → congestión → alcista",
                "SHORT":   " → flujo normal → bajista",
                "NEUTRAL": "",
            }[bias])
    )

    return {
        "signal_a5":          round(signal_a5, 3),
        "bias":               bias,
        "z_combined":         round(z_combined, 3),
        "z_ships":            round(z_ships, 3),
        "z_tonnage":          round(z_tonnage, 3),
        "z_level":            round(z_level, 3),
        "n_ships":            n_ships,
        "tonnage":            tonnage,
        "n_expected":         n_exp,
        "n_scheduled":        n_sch,
        "n_berthed":          n_ber,
        "mean_ships":         round(mean_ships, 1),
        "mean_tonnage":       round(mean_tonnage, 0),
        "velocity_ratio":     round(curr_velocity, 3),
        "velocity_ratio_mean": round(mean_velocity, 3),
        "z_velocity":         round(z_velocity_raw, 3),
        "queue_delta_7d":     int(queue_delta_7d) if queue_delta_7d is not None else None,
        "z_queue_delta":      round(z_queue_delta, 3),
        "description":        desc,
        "snapshot_date":      snap_dt,
    }
