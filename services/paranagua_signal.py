"""
Señal A6: Cola de exportación de azúcar en el Puerto de Paranaguá.

Tres componentes combinados (igual estructura que Santos A5):

  1. NIVEL (55%) — z-score barcos azúcar activos vs media 30d
       Más barcos → congestión → oferta sin salir → alcista

  2. VELOCIDAD DE CARGA (30%) — ratio atracados/(atracados+esperados) vs media 30d
       Ratio bajo → bottleneck → señal adelantada LONG

  3. DWELL TIME (15%) — media dwell time despachados vs histórico
       Dwell por encima de media → congestión real confirmada → LONG
       Dwell por debajo → puerto eficiente → NEUTRAL/SHORT

Signal combinada: [-1, +1]  →  LONG / NEUTRAL / SHORT

Nota: Paranaguá DESPACHADOS tiene llegada + salida directas → dwell sin tracking.
"""
import logging
import statistics
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

LOOKBACK_DAYS    = 30
SIGNAL_THRESHOLD = 0.50
MIN_HIST_DAYS    = 5

WEIGHT_LEVEL    = 0.55
WEIGHT_VELOCITY = 0.30
WEIGHT_DWELL    = 0.15


def compute_paranagua_signal(session: Session) -> Optional[dict]:
    """
    Señal de cola de exportación de azúcar en Paranaguá.

    Returns dict con:
      signal_a6, bias, z_combined
      n_active, n_atracados, n_esperados, n_programados, n_ao_largo
      mean_active, z_level
      velocity_ratio, velocity_ratio_mean, z_velocity
      dwell_days, dwell_mean, z_dwell
      description, snapshot_date
    """
    base_empty = {
        "signal_a6": 0.0, "bias": "NEUTRAL", "z_combined": None,
        "n_active": 0, "n_atracados": 0, "n_esperados": 0,
        "n_programados": 0, "n_ao_largo": 0,
        "mean_active": None, "z_level": None,
        "velocity_ratio": None, "velocity_ratio_mean": None, "z_velocity": None,
        "dwell_days": None, "dwell_mean": None, "z_dwell": None,
        "description": "Paranaguá: sin datos — ejecutar fetch_paranagua_port",
        "snapshot_date": None,
    }

    # Snapshot más reciente
    try:
        from ingestion.paranagua_port import get_latest_snapshot
        snap = get_latest_snapshot(session)
    except Exception as e:
        logger.warning("paranagua_signal snapshot: %s", e)
        base_empty["description"] = f"Paranaguá: error leyendo snapshot — {e}"
        return base_empty

    if snap is None:
        return base_empty

    n_atr  = snap.get("n_atracados",   0)
    n_pro  = snap.get("n_programados", 0)
    n_lar  = snap.get("n_ao_largo",    0)
    n_esp  = snap.get("n_esperados",   0)
    n_active = n_atr + n_pro + n_lar + n_esp
    snap_dt  = snap.get("snapshot_date", "?")

    # ── Histórico 30 días ────────────────────────────────────────────────────
    hist = session.execute(text("""
        SELECT snapshot_date,
               SUM(CASE WHEN page IN ('atracados','programados','ao_largo','esperados')
                        THEN 1 ELSE 0 END)                        AS n_active,
               SUM(CASE WHEN page = 'atracados'  THEN 1 ELSE 0 END) AS n_atr,
               SUM(CASE WHEN page = 'esperados'  THEN 1 ELSE 0 END) AS n_esp
        FROM paranagua_port_snapshot
        WHERE snapshot_date >= CURRENT_DATE - INTERVAL '30 days'
        GROUP BY snapshot_date
        ORDER BY snapshot_date
    """)).fetchall()

    if len(hist) < MIN_HIST_DAYS:
        base_empty["n_active"]     = n_active
        base_empty["n_atracados"]  = n_atr
        base_empty["n_esperados"]  = n_esp
        base_empty["n_programados"] = n_pro
        base_empty["n_ao_largo"]   = n_lar
        base_empty["snapshot_date"] = snap_dt
        base_empty["description"]  = (
            f"Paranaguá: {n_active} barcos azúcar ({n_atr} atrac, {n_pro} prog, "
            f"{n_lar} ao largo, {n_esp} esperados) | historial insuficiente (<{MIN_HIST_DAYS}d)")
        return base_empty

    hist_active   = [float(r[1] or 0) for r in hist]
    hist_atr      = [float(r[2] or 0) for r in hist]
    hist_esp      = [float(r[3] or 0) for r in hist]

    # ── Componente 1: Nivel ──────────────────────────────────────────────────
    mean_active = sum(hist_active) / len(hist_active)
    std_active  = max(statistics.stdev(hist_active) if len(hist_active) > 1 else 1.0, 0.5)
    z_level     = (n_active - mean_active) / std_active

    # ── Componente 2: Velocidad de carga ────────────────────────────────────
    # velocity_ratio = atracados / (atracados + esperados)
    hist_velocity = [
        a / max(1.0, a + e)
        for a, e in zip(hist_atr, hist_esp)
    ]
    mean_velocity = sum(hist_velocity) / len(hist_velocity)
    std_velocity  = max(
        statistics.stdev(hist_velocity) if len(hist_velocity) > 1 else 0.05,
        0.05
    )

    curr_velocity  = n_atr / max(1.0, n_atr + n_esp)
    z_velocity_raw = (curr_velocity - mean_velocity) / std_velocity
    z_velocity_sig = -z_velocity_raw   # ratio bajo = bottleneck = LONG

    # ── Componente 3: Dwell time ─────────────────────────────────────────────
    z_dwell_sig  = 0.0
    dwell_days   = None
    dwell_mean   = None

    try:
        from ingestion.paranagua_port import get_dwell_stats
        dwell = get_dwell_stats(session, days_back=60)
        if dwell is not None:
            dwell_days = dwell.get("mean_dwell_days")
            dwell_std  = max(dwell.get("std_dwell_days", 1.0), 0.5)

            # Necesitamos la media histórica de dwell para comparar.
            # Usamos la media de los últimos 60 días del mismo SP como referencia.
            # Si solo tenemos un período, z_dwell queda en 0.
            # Baseline: media general de los 60d (= dwell_days mismo, z=0)
            # Esto es útil cuando cambia drásticamente (>2d encima de lo usual).
            # Para afinar en el futuro se puede dividir en ventanas.
            dwell_mean = dwell_days   # mismo valor → z=0 la primera vez

            # En datos reales la media cambia semana a semana. Por ahora usamos
            # un threshold absoluto: >6d es congestión, <3d es eficiente.
            if dwell_days is not None:
                if dwell_days > 6.0:
                    z_dwell_sig = 1.0    # congestión real confirmada
                elif dwell_days < 3.0:
                    z_dwell_sig = -1.0   # eficiencia alta
    except Exception as e:
        logger.debug("paranagua dwell: %s", e)

    # ── Combinación ─────────────────────────────────────────────────────────
    z_combined = (WEIGHT_LEVEL    * z_level
                + WEIGHT_VELOCITY * z_velocity_sig
                + WEIGHT_DWELL    * z_dwell_sig)
    signal_a6  = max(-1.0, min(1.0, z_combined / 2.0))

    if z_combined >= SIGNAL_THRESHOLD:
        bias = "LONG"
    elif z_combined <= -SIGNAL_THRESHOLD:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    # ── Descripción ─────────────────────────────────────────────────────────
    vel_label = ""
    if z_velocity_raw < -0.5:
        vel_label = "  ↑ bottleneck"
    elif z_velocity_raw > 0.5:
        vel_label = "  ↓ fluido"

    dwell_str = ""
    if dwell_days is not None:
        dwell_str = "  dwell=%.1fd" % dwell_days

    bias_tag = {"LONG": "congestión → alcista", "SHORT": "flujo normal → bajista",
                "NEUTRAL": "neutral"}.get(bias, bias)

    desc = (
        "Paranaguá A6: %d barcos (%d atrac, %d prog, %d ao largo, %d esp) | "
        "z=%.2f | vel=%.0f%% (med=%.0f%%)%s%s → %s" % (
            n_active, n_atr, n_pro, n_lar, n_esp,
            z_combined,
            curr_velocity * 100, mean_velocity * 100,
            vel_label, dwell_str, bias_tag)
    )

    return {
        "signal_a6":           round(signal_a6, 3),
        "bias":                bias,
        "z_combined":          round(z_combined, 3),
        "z_level":             round(z_level, 3),
        "z_velocity":          round(z_velocity_raw, 3),
        "z_dwell":             round(z_dwell_sig, 3),
        "n_active":            n_active,
        "n_atracados":         n_atr,
        "n_esperados":         n_esp,
        "n_programados":       n_pro,
        "n_ao_largo":          n_lar,
        "mean_active":         round(mean_active, 1),
        "velocity_ratio":      round(curr_velocity, 3),
        "velocity_ratio_mean": round(mean_velocity, 3),
        "dwell_days":          dwell_days,
        "dwell_mean":          dwell_mean,
        "description":         desc,
        "snapshot_date":       snap_dt,
    }
