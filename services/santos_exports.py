"""
Santos export flow tracker.

Metodología:
  Cada día se compara el berthed list actual con el snapshot anterior.
  Los barcos que estaban antes y ya no están = zarparon = se registran en
  santos_departures con su tonelaje.

  Deduplicación: UniqueConstraint (ship_name, terminal, departed_date).
  Si el scraper se corre varias veces en el mismo día, no se duplica.

Uso:
  process_departures(session)       → detecta y registra partidas de hoy
  get_monthly_exports(session)      → toneladas exportadas este mes
  get_weekly_exports(session, n=4)  → últimas N semanas
  format_export_context(session)    → líneas para score_today CONTEXTO MENSUAL
"""
import logging
from datetime import date, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)


# ── Detección de partidas ─────────────────────────────────────────────────────

def process_departures(session: Session, reference_date: date | None = None) -> list:
    """
    Compara el berthed list de reference_date con el snapshot anterior
    más reciente. Registra en santos_departures los barcos que zarparon.

    Idempotente: la UniqueConstraint evita duplicados si se llama varias
    veces el mismo día.

    Retorna lista de dicts de las partidas nuevas insertadas.
    """
    if reference_date is None:
        reference_date = date.today()

    # Barcos actualmente en berthed
    current_rows = session.execute(text("""
        SELECT ship_name, COALESCE(terminal, '') AS terminal
        FROM santos_port_snapshot
        WHERE snapshot_date = :d AND page = 'berthed'
    """), {"d": reference_date}).fetchall()
    current_set = {(r[0], r[1]) for r in current_rows}

    # Snapshot anterior más reciente con barcos berthed
    prev_date_row = session.execute(text("""
        SELECT MAX(snapshot_date)
        FROM santos_port_snapshot
        WHERE page = 'berthed' AND snapshot_date < :d
    """), {"d": reference_date}).fetchone()

    if not prev_date_row or prev_date_row[0] is None:
        logger.info("santos_exports: no hay snapshot previo a %s", reference_date)
        return []

    prev_date = prev_date_row[0]

    # Barcos que estaban en el snapshot anterior
    prev_rows = session.execute(text("""
        SELECT ship_name, COALESCE(terminal, '') AS terminal,
               load_qty_t, cargo, voyage, duv
        FROM santos_port_snapshot
        WHERE snapshot_date = :d AND page = 'berthed'
    """), {"d": prev_date}).fetchall()

    new_departures = []
    for r in prev_rows:
        ship, terminal, qty, cargo, voyage, duv = r
        if (ship, terminal) in current_set:
            continue   # sigue en berthed, no zarpó

        # Primer día visto en berthed (para calcular tiempo de carga)
        first_row = session.execute(text("""
            SELECT MIN(snapshot_date)
            FROM santos_port_snapshot
            WHERE page = 'berthed'
              AND ship_name = :s
              AND COALESCE(terminal, '') = :t
              AND snapshot_date <= :last
        """), {"s": ship, "t": terminal, "last": prev_date}).fetchone()
        first_seen = first_row[0] if (first_row and first_row[0]) else prev_date

        try:
            session.execute(text("""
                INSERT INTO santos_departures
                  (ship_name, terminal, cargo, sugar_tonnes,
                   first_seen, last_seen, departed_date, voyage, duv)
                VALUES
                  (:ship, :terminal, :cargo, :tonnes,
                   :first, :last, :departed, :voyage, :duv)
                ON CONFLICT (ship_name, terminal, departed_date) DO NOTHING
            """), {
                "ship": ship, "terminal": terminal, "cargo": cargo,
                "tonnes": int(qty) if qty else None,
                "first": str(first_seen), "last": str(prev_date),
                "departed": str(reference_date),
                "voyage": voyage, "duv": duv,
            })
            session.commit()
            new_departures.append({
                "ship_name": ship, "terminal": terminal,
                "sugar_tonnes": qty, "departed_date": reference_date,
            })
            logger.info("santos_exports: partida registrada — %s (%s t) el %s",
                        ship, qty or "?", reference_date)
        except Exception as exc:
            session.rollback()
            logger.warning("santos_exports: error insertando partida %s: %s", ship, exc)

    return new_departures


def process_all_historical(session: Session) -> int:
    """
    Procesa todos los snapshots históricos de forma retroactiva.
    Útil para la primera vez que se ejecuta el sistema.
    Retorna número de partidas registradas.
    """
    dates = session.execute(text("""
        SELECT DISTINCT snapshot_date
        FROM santos_port_snapshot
        WHERE page = 'berthed'
        ORDER BY snapshot_date ASC
    """)).fetchall()

    total = 0
    for row in dates:
        deps = process_departures(session, reference_date=row[0])
        total += len(deps)

    logger.info("santos_exports: procesamiento histórico — %d partidas registradas", total)
    return total


# ── Agregaciones ──────────────────────────────────────────────────────────────

def get_monthly_exports(session: Session,
                        year: int | None  = None,
                        month: int | None = None) -> dict:
    """
    Toneladas de azúcar exportadas desde Santos en un mes dado.
    Por defecto: mes actual.
    """
    today = date.today()
    if year  is None: year  = today.year
    if month is None: month = today.month

    from calendar import monthrange
    last_day = monthrange(year, month)[1]
    d_from = date(year, month, 1)
    d_to   = date(year, month, last_day)

    rows = session.execute(text("""
        SELECT COALESCE(SUM(sugar_tonnes), 0),
               COUNT(*),
               COUNT(CASE WHEN sugar_tonnes IS NOT NULL THEN 1 END)
        FROM santos_departures
        WHERE departed_date BETWEEN :d0 AND :d1
    """), {"d0": d_from, "d1": d_to}).fetchone()

    tonnes     = int(rows[0]) if rows[0] else 0
    n_ships    = int(rows[1]) if rows[1] else 0
    n_with_qty = int(rows[2]) if rows[2] else 0

    return {
        "year": year, "month": month,
        "total_tonnes":    tonnes,
        "n_ships":         n_ships,
        "n_with_quantity": n_with_qty,
        "d_from": d_from, "d_to": d_to,
    }


def get_weekly_exports(session: Session, n_weeks: int = 4) -> list:
    """
    Exportaciones de las últimas N semanas (lunes→domingo).
    Retorna lista de dicts ordenada de más reciente a más antigua.
    """
    today = date.today()
    # Inicio de la semana actual (lunes)
    week_start = today - timedelta(days=today.weekday())
    results = []
    for i in range(n_weeks):
        w0 = week_start - timedelta(weeks=i)
        w1 = w0 + timedelta(days=6)
        row = session.execute(text("""
            SELECT COALESCE(SUM(sugar_tonnes), 0), COUNT(*)
            FROM santos_departures
            WHERE departed_date BETWEEN :d0 AND :d1
        """), {"d0": w0, "d1": w1}).fetchone()
        results.append({
            "week_start":   w0,
            "week_end":     w1,
            "total_tonnes": int(row[0]) if row[0] else 0,
            "n_ships":      int(row[1]) if row[1] else 0,
        })
    return results


def get_export_yoy(session: Session) -> dict | None:
    """
    Mes-a-fecha actual vs mismo período del año anterior.
    Retorna None si no hay datos de ningún año.
    """
    today = date.today()
    d_from_curr  = date(today.year, today.month, 1)
    d_from_prev  = date(today.year - 1, today.month, 1)
    d_to_prev    = d_from_prev + timedelta(days=(today - d_from_curr).days)

    def _mtd(d0, d1):
        r = session.execute(text("""
            SELECT COALESCE(SUM(sugar_tonnes), 0), COUNT(*)
            FROM santos_departures
            WHERE departed_date BETWEEN :d0 AND :d1
        """), {"d0": d0, "d1": d1}).fetchone()
        return int(r[0]) if r[0] else 0, int(r[1]) if r[1] else 0

    curr_t, curr_n = _mtd(d_from_curr, today)
    prev_t, prev_n = _mtd(d_from_prev, d_to_prev)

    if curr_t == 0 and prev_t == 0:
        return None

    yoy = round((curr_t / prev_t - 1) * 100, 1) if prev_t > 0 else None
    return {
        "current_mtd_t": curr_t, "current_n": curr_n,
        "prior_mtd_t":   prev_t, "prior_n":   prev_n,
        "yoy_pct":       yoy,
        "period_days":   (today - d_from_curr).days + 1,
    }


# ── Display para score_today ──────────────────────────────────────────────────

def format_export_context(session: Session) -> list:
    """
    Genera líneas de texto para el bloque CONTEXTO MENSUAL de score_today.py.
    """
    lines = []
    lines.append("")
    lines.append("  -- SANTOS: FLUJO EXPORTADOR AZUCAR --")

    # Cuántas partidas tenemos en total para dar contexto de madurez del tracker
    total_row = session.execute(text(
        "SELECT COUNT(*), MIN(departed_date), MAX(departed_date) FROM santos_departures"
    )).fetchone()
    total_deps = int(total_row[0]) if total_row[0] else 0

    if total_deps == 0:
        lines.append("  Sin datos de partidas aun — ejecutar scripts/process_santos_departures.py")
        return lines

    min_date = total_row[1]
    max_date = total_row[2]
    lines.append("  Tracking desde %s  (%d partidas registradas)" % (
        str(min_date), total_deps))

    # Mes actual MTD
    today = date.today()
    month_data = get_monthly_exports(session)
    month_name = today.strftime("%b-%Y")
    if month_data["n_ships"] > 0:
        tonnes_s = ("%d t" % month_data["total_tonnes"]) if month_data["n_with_quantity"] > 0 else "tonelaje no disponible"
        lines.append("  %s MTD: %s  (%d barcos)" % (month_name, tonnes_s, month_data["n_ships"]))
    else:
        lines.append("  %s MTD: sin partidas registradas aun" % month_name)

    # Últimas 3 semanas
    weeks = get_weekly_exports(session, n_weeks=3)
    for i, w in enumerate(weeks):
        if w["n_ships"] == 0 and i > 0:
            continue
        label = "Sem actual" if i == 0 else ("Sem -%d" % i)
        if w["n_ships"] > 0:
            tonnes_s = ("%d t" % w["total_tonnes"]) if w["total_tonnes"] > 0 else "s/tonelaje"
            lines.append("  %-12s (%s - %s): %s  %d barcos" % (
                label,
                w["week_start"].strftime("%d/%m"),
                w["week_end"].strftime("%d/%m"),
                tonnes_s,
                w["n_ships"],
            ))

    # YoY si hay datos del año anterior
    yoy = get_export_yoy(session)
    if yoy and yoy["prior_mtd_t"] > 0 and yoy["yoy_pct"] is not None:
        dir_s = "^" if yoy["yoy_pct"] > 0 else "v"
        lines.append("  YoY %s: %+.1f%%  (curr=%d t  prev=%d t)" % (
            month_name, yoy["yoy_pct"], yoy["current_mtd_t"], yoy["prior_mtd_t"]))

    return lines
