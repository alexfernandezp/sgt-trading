"""
Rellena close_Nd y ret_Nd / dir_ret_Nd en shadow_trades.
Correr diariamente (ej. via cron o al final de score_today.py).

Lógica:
  - Busca registros con close_5d IS NULL y signal_date <= hoy - 5 dias bursátiles
  - Para cada horizonte N (1,5,10,20): busca el close de SB_CONT en price_history
    para la fecha más cercana >= signal_date + N días calendario
  - dir_ret = ret * signo(direction)  → positivo = modelo acertó
"""
import sys, os, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

from database import SessionLocal
from sqlalchemy import text
from datetime import date, timedelta

HORIZONS = [1, 5, 10, 20]


def _next_close(session, ref_date: date, offset_days: int) -> float | None:
    """Precio de cierre de SB_CONT en o después de ref_date + offset_days."""
    target = ref_date + timedelta(days=offset_days)
    row = session.execute(text("""
        SELECT close FROM price_history
        WHERE instrument = 'SB_CONT' AND date >= :target
        ORDER BY date ASC LIMIT 1
    """), {"target": target}).fetchone()
    return float(row.close) if row else None


def fill_forward_returns(session):
    pending = session.execute(text("""
        SELECT id, signal_date, direction, entry_price
        FROM shadow_trades
        WHERE close_5d IS NULL
          AND signal_date <= CURRENT_DATE - INTERVAL '7 days'
        ORDER BY signal_date
    """)).fetchall()

    if not pending:
        logger.info("fill_forward_returns: nada pendiente")
        return 0

    updated = 0
    for row in pending:
        closes = {}
        for n in HORIZONS:
            closes[n] = _next_close(session, row.signal_date, n)

        entry = float(row.entry_price) if row.entry_price else None
        sign  = 1.0 if row.direction == "LONG" else (-1.0 if row.direction == "SHORT" else 0.0)

        params = {"id": row.id}
        sets   = []
        for n in HORIZONS:
            c = closes.get(n)
            params["c%d" % n] = c
            sets.append("close_%dd = :c%d" % (n, n))
            if c is not None and entry:
                r = (c - entry) / entry
                params["r%d"  % n] = round(r, 6)
                params["dr%d" % n] = round(r * sign, 6)
                sets.append("ret_%dd = :r%d" % (n, n))
                sets.append("dir_ret_%dd = :dr%d" % (n, n))

        if sets:
            session.execute(
                text("UPDATE shadow_trades SET %s WHERE id = :id" % ", ".join(sets)),
                params
            )
            updated += 1

    session.commit()
    logger.info("fill_forward_returns: %d registros actualizados", updated)
    return updated


if __name__ == "__main__":
    with SessionLocal() as s:
        n = fill_forward_returns(s)
    print("Actualizados:", n)
