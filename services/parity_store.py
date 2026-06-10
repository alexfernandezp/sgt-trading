"""
Guarda y consulta snapshots diarios de paridad etanol/azúcar SP.
Escribe en ethanol_parity_daily. Llamado desde run_morning.py (o run_afternoon.py).
"""
import logging
from datetime import date, timedelta
from typing import Optional

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from models import EthanolParityDaily, PipelineRunLog

logger = logging.getLogger(__name__)

# Umbrales paridad (calibrados sobre histórico 2010-2026, metodología Green Pool)
# Recalibrar si cambiamos fuente de CEPEA Paulínia a UNICADATA
_BULLISH = 1.028   # P75 ratio → mills prefieren etanol → bearish azúcar
_BEARISH = 0.788   # P25 ratio → mills prefieren azúcar → bullish azúcar


def save_parity_snapshot(session: Session, parity: dict) -> bool:
    """
    Upserta una fila en ethanol_parity_daily con los datos de parity dict
    (producido por compute_ethanol_parity_v2 en ethanol_parity.py).

    Returns True si OK, False si error.
    """
    today = date.today()

    # Weekly changes — comparar con registro de hace 7 días
    prev = _get_snapshot(session, today - timedelta(days=7))
    weekly_eth    = None
    weekly_spread = None
    if prev:
        if parity.get("ethanol_c_lb") and prev.ethanol_c_lb:
            weekly_eth = round(float(parity["ethanol_c_lb"]) - float(prev.ethanol_c_lb), 4)
        if parity.get("spread_c_lb") and prev.spread_c_lb:
            weekly_spread = round(float(parity["spread_c_lb"]) - float(prev.spread_c_lb), 4)

    row = {
        "parity_date":          today,
        "hydrous_source":       parity.get("hydrous_source"),
        "hydrous_brl_liter":    parity.get("hydrous_brl_liter"),
        "hydrous_usd_m3":       parity.get("hydrous_usd_m3"),
        "ptax_used":            parity.get("ptax_used"),
        "ethanol_c_lb":         parity.get("ethanol_c_lb"),
        "ice_c_lb":             parity.get("ice_c_lb"),
        "spread_c_lb":          parity.get("spread_c_lb"),
        "weekly_change_eth":    weekly_eth,
        "weekly_change_spread": weekly_spread,
        "parity_ratio":         parity.get("parity_ratio"),
        "signal":               parity.get("signal", 0),
        "bias":                 parity.get("bias", "NEUTRAL"),
    }

    try:
        stmt = (
            insert(EthanolParityDaily)
            .values(**row)
            .on_conflict_do_update(
                constraint="uq_ethanol_parity_date",
                set_={k: v for k, v in row.items() if k != "parity_date"},
            )
        )
        session.execute(stmt)
        session.commit()
        logger.info(
            "parity_store: %s  eth=%.4f  ice=%.4f  spread=%+.4f  wkly_eth=%s",
            today,
            parity.get("ethanol_c_lb") or 0,
            parity.get("ice_c_lb") or 0,
            parity.get("spread_c_lb") or 0,
            f"{weekly_eth:+.4f}" if weekly_eth is not None else "n/a",
        )
        return True
    except Exception as e:
        logger.error("parity_store save: %s", e)
        session.rollback()
        return False


def _get_snapshot(session: Session, d: date) -> Optional[EthanolParityDaily]:
    """Lee el snapshot más cercano a la fecha d (±3 días)."""
    from sqlalchemy import text
    rows = session.execute(text(
        "SELECT * FROM ethanol_parity_daily "
        "WHERE parity_date BETWEEN :from_d AND :to_d "
        "ORDER BY parity_date DESC LIMIT 1"
    ), {"from_d": d - timedelta(days=3), "to_d": d + timedelta(days=1)}).fetchone()
    if not rows:
        return None
    # Mapear a objeto simple con atributos
    class _Row:
        pass
    obj = _Row()
    for k, v in rows._mapping.items():
        setattr(obj, k, v)
    return obj


def get_latest_parity(session: Session) -> Optional[dict]:
    """Lee el snapshot más reciente de la DB. Útil para el dashboard."""
    from sqlalchemy import text
    row = session.execute(text(
        "SELECT * FROM ethanol_parity_daily ORDER BY parity_date DESC LIMIT 1"
    )).fetchone()
    if not row:
        return None
    return dict(row._mapping)


def get_parity_history(session: Session, days: int = 90) -> list[dict]:
    """Lee los últimos N días para gráficos."""
    from sqlalchemy import text
    from datetime import timedelta
    cutoff = date.today() - timedelta(days=days)
    rows = session.execute(text(
        "SELECT * FROM ethanol_parity_daily "
        "WHERE parity_date >= :cutoff ORDER BY parity_date"
    ), {"cutoff": cutoff}).fetchall()
    return [dict(r._mapping) for r in rows]


# ---------------------------------------------------------------------------
# Pipeline run log
# ---------------------------------------------------------------------------

def log_pipeline_run(
    session: Session,
    task_name: str,
    status: str,
    duration_s: float,
    details: Optional[dict] = None,
    error_msg: Optional[str] = None,
    log_file: Optional[str] = None,
    script: Optional[str] = None,
) -> None:
    """
    Registra la ejecución de un pipeline en pipeline_run_log.
    Silencia excepciones — el logging nunca debe matar el proceso principal.
    """
    try:
        row = PipelineRunLog(
            task_name  = task_name,
            script     = script,
            status     = status,
            duration_s = round(duration_s, 1),
            details    = details or {},
            error_msg  = error_msg,
            log_file   = log_file,
        )
        session.add(row)
        session.commit()
    except Exception as e:
        logger.warning("log_pipeline_run failed (non-fatal): %s", e)
        try:
            session.rollback()
        except Exception:
            pass
