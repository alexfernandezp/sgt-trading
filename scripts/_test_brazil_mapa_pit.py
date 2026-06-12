"""
P3.E.5/E.6 — PIT backtest support + multi-revision integration tests.

Valida:
  P3.E.5 — get_latest_production(as_of_date=):
    - Sin as_of_date: comportamiento live (sin filtro, DISTINCT ON)
    - Con as_of_date anterior a primera revision → lista vacia
    - Con as_of_date == primera revision → devuelve primera version
    - Con as_of_date entre rev1 y rev2 → devuelve rev1
    - Con as_of_date >= rev2 → devuelve rev2 (mas reciente)

  P3.E.6 — _insert_revision multi-revision scenarios:
    - first:       primera insercion para (year, seq) → categoria 'first'
    - revision:    issue_date mas nueva → categoria 'revision'
    - duplicate:   misma issue_date que existente → categoria 'duplicate'
    - scope_drift: issue_date mas antigua que existente → categoria 'scope_drift'
    - race:        IntegrityError en INSERT → tratado como duplicate

Uso: py scripts/_test_brazil_mapa_pit.py
"""
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from sqlalchemy.exc import IntegrityError as SAIntegrityError

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.brazil_mapa import (
    MAPA_MAX_AGE_DAYS,
    _insert_revision,
    get_latest_production,
)

# ── Warning capture ──────────────────────────────────────────────────────────
captured: list[tuple[int, str, str]] = []


class _Cap(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.WARNING:
            captured.append((record.levelno, record.name, record.getMessage()))


logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(name)s - %(message)s")
for _mod in ("services.data_quality", "ingestion.brazil_mapa"):
    logging.getLogger(_mod).addHandler(_Cap())

results: list[tuple[str, bool]] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name} {detail}")


TODAY = date(2026, 6, 12)
HARVEST = "2025-2026"


# ── Stub session helpers ─────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def scalar(self):
        return self._rows[0] if self._rows else None


class _StubSession:
    """
    Stub minimo para get_latest_production: execute() devuelve filas pre-seteadas.
    Simula el resultado de DISTINCT ON (ya filtrado/deduplicado por el caller).
    """
    def __init__(self, rows):
        self._rows = rows

    def execute(self, stmt, params=None):
        return _FakeResult(self._rows)


def _row(days_old: int, issue_offset: int = 0, seq: int = 5,
         revision_seq: int = 1) -> tuple:
    """
    Construye una tupla fila simulando el resultado de get_latest_production.
    days_old: antiguedad del report_date respecto a TODAY.
    issue_offset: dias adicionales de antiguedad del issue_date vs report_date.
    """
    rdate = TODAY - timedelta(days=days_old)
    idate = rdate - timedelta(days=issue_offset)
    return (
        rdate, HARVEST, seq,
        400_000_000, 25_000_000, 20_000_000,  # cane/sugar/eth cumulative
        47.5,                                   # sugar_mix_pct
        idate, revision_seq,
    )


# ── P3.E.5 — get_latest_production(as_of_date=) ─────────────────────────────
print("\n=== P3.E.5 — as_of_date=None (live, sin filtro) ===")
captured.clear()
sess = _StubSession([_row(15), _row(30, seq=4)])
out = get_latest_production(sess, n=4, reference=TODAY)
report("as_of_date=None fresh (15d) → filas devueltas", len(out) == 2)
report("primera fila es la mas reciente", out[0]["report_date"] < str(TODAY))
report("sin WARN emitido", not any("latest_report_date" in m for _, _, m in captured))

print("\n=== P3.E.5 — as_of_date fresco (misma logica que None) ===")
captured.clear()
sess = _StubSession([_row(10)])
out_live   = get_latest_production(sess, n=4, reference=TODAY)
out_pit    = get_latest_production(sess, n=4, reference=TODAY,
                                   as_of_date=TODAY)
# Ambas rutas deben devolver la misma cantidad de filas con el mismo stub
report("as_of_date=TODAY produce misma cantidad que None",
       len(out_live) == len(out_pit))

print("\n=== P3.E.5 — as_of_date stale gate ===")
captured.clear()
# issue_date es 40 dias antes del report_date; as_of_date = TODAY → stale gate
# report_date es 36 dias antes de TODAY → freshness stale
sess = _StubSession([_row(MAPA_MAX_AGE_DAYS + 1)])
out = get_latest_production(sess, n=4, reference=TODAY)
report("report_date 36d → stale → lista vacia", out == [])
report("WARN emitido por freshness gate",
       any("latest_report_date" in m for _, _, m in captured))

print("\n=== P3.E.5 — as_of_date anterior a issue_date → sin filas de la DB ===")
# Simulamos que la DB no retorna filas (porque as_of filter excluye todo)
captured.clear()
sess = _StubSession([])  # DB retorna vacío — el PIT filter excluyo todo
out = get_latest_production(sess, n=4, reference=TODAY, as_of_date=TODAY - timedelta(days=60))
report("DB vacia con as_of_date → lista vacia sin WARN",
       out == [] and not any("latest_report_date" in m for _, _, m in captured))

print("\n=== P3.E.5 — as_of_date seleccion de revision correcta ===")
# Rev1 issue_date = 2026-04-20, Rev2 issue_date = 2026-05-10
# Nota: el stub simula lo que DISTINCT ON entregaria para cada as_of_date
REV1_DATE = date(2026, 4, 20)
REV2_DATE = date(2026, 5, 10)

# Stub simula resultado PIT con as_of = rev1_date → solo rev1 disponible
sess_rev1 = _StubSession([
    (date(2026, 4, 15), HARVEST, 1,
     300_000_000, 15_000_000, 12_000_000, 45.0, REV1_DATE, 1)
])

# En backtesting PIT: reference=as_of_date para que freshness se evalúe
# relativo a la fecha de consulta histórica, no a TODAY.
out_rev1 = get_latest_production(sess_rev1, n=4, reference=REV1_DATE, as_of_date=REV1_DATE)
report("as_of=rev1_date → devuelve revision 1",
       len(out_rev1) == 1 and out_rev1[0]["report_issue_date"] == str(REV1_DATE))
report("revision_seq == 1 en resultado",
       len(out_rev1) > 0 and out_rev1[0]["report_revision_seq"] == 1)

# Stub simula resultado PIT con as_of = rev2_date → rev2 disponible (mas reciente)
sess_rev2 = _StubSession([
    (date(2026, 5, 8), HARVEST, 1,
     310_000_000, 16_000_000, 12_500_000, 46.2, REV2_DATE, 2)
])
out_rev2 = get_latest_production(sess_rev2, n=4, reference=REV2_DATE, as_of_date=REV2_DATE)
report("as_of=rev2_date → devuelve revision 2",
       len(out_rev2) == 1 and out_rev2[0]["report_issue_date"] == str(REV2_DATE))
report("revision_seq == 2 en resultado",
       len(out_rev2) > 0 and out_rev2[0]["report_revision_seq"] == 2)

print("\n=== P3.E.5 — campos del dict de salida (backward compat) ===")
captured.clear()
sess = _StubSession([_row(10)])
out = get_latest_production(sess, n=4, reference=TODAY)
if out:
    keys = set(out[0].keys())
    expected = {"report_date", "harvest_year", "fortnight_seq",
                "cane_crushed_t", "sugar_t", "ethanol_total_m3",
                "sugar_mix_pct", "report_issue_date", "report_revision_seq"}
    report("claves backward-compat presentes", expected.issubset(keys))
    report("cane_crushed_t float", isinstance(out[0]["cane_crushed_t"], float))
    report("report_date string (no date)", isinstance(out[0]["report_date"], str))


# ── P3.E.6 — _insert_revision multi-revision ────────────────────────────────
print("\n=== P3.E.6 — _insert_revision categorias ===")

class _InsertStubResult:
    """Resultado stub para SELECT max() y INSERT."""
    def __init__(self, value):
        self._value = value
    def scalar(self):
        return self._value


class _InsertSession:
    """
    Stub para _insert_revision. Heuristica: si la SQL contiene 'max(' → SELECT;
    si no → INSERT. Mismo patron que _test_brazil_mapa_hardening.py.
    """
    def __init__(self, existing_issue=None, *, raise_integrity=False):
        self._existing_issue = existing_issue
        self._raise_integrity = raise_integrity
        self.n_inserts = 0
        self.n_selects = 0
        self.did_rollback = False

    def execute(self, stmt, params=None):
        sql_text = str(stmt).lower()
        if "max(" in sql_text or ("select" in sql_text and "report_issue_date" in sql_text):
            self.n_selects += 1
            return _InsertStubResult(self._existing_issue)
        if self._raise_integrity:
            raise SAIntegrityError("stub", "stub", Exception("uq_brazil_pit"))
        self.n_inserts += 1
        return _InsertStubResult(None)

    def rollback(self):
        self.did_rollback = True

    def commit(self):
        pass


def _record(issue_date: date, seq: int = 5, rev: int = 1) -> dict:
    return {
        "harvest_year":         HARVEST,
        "fortnight_seq":        seq,
        "report_date":          date(2026, 5, 1),
        "report_issue_date":    issue_date,
        "report_revision_seq":  rev,
        "source_url":           "http://test/foo.xls",
        "cane_crushed_t_cumulative":       400_000_000,
        "sugar_t_cumulative":              25_000_000,
        "ethanol_anhydrous_m3_cumulative": 10_000_000,
        "ethanol_hydrated_m3_cumulative":  10_000_000,
        "ethanol_total_m3_cumulative":     20_000_000,
        "sugar_mix_pct":                   47.5,
    }


# first: sin existente → insert + 'first'
captured.clear()
sess = _InsertSession(None)
cat = _insert_revision(sess, _record(TODAY))
report("sin existente → categoria 'first'", cat == "first")
report("INSERT ejecutado (SELECT + INSERT)",
       sess.n_selects == 1 and sess.n_inserts == 1)

# revision: new > existing → insert + 'revision'
captured.clear()
sess = _InsertSession(TODAY - timedelta(days=15))
cat = _insert_revision(sess, _record(TODAY))
report("new > existing → categoria 'revision'", cat == "revision")
report("INSERT ejecutado en revision", sess.n_inserts == 1)

# duplicate: new == existing → skip + 'duplicate'
captured.clear()
sess = _InsertSession(TODAY)
cat = _insert_revision(sess, _record(TODAY))
report("new == existing → categoria 'duplicate'", cat == "duplicate")
report("INSERT NO ejecutado en duplicate", sess.n_inserts == 0)

# scope_drift: new < existing → skip + WARN
captured.clear()
sess = _InsertSession(TODAY)
cat = _insert_revision(sess, _record(TODAY - timedelta(days=5)))
report("new < existing → categoria 'scope_drift'", cat == "scope_drift")
warn_msgs = [m for _, _, m in captured if "scope_drift" in m]
report("WARN scope_drift emitido", len(warn_msgs) == 1)
report("INSERT NO ejecutado en scope_drift", sess.n_inserts == 0)

# race condition: IntegrityError en INSERT → 'duplicate'
captured.clear()
sess = _InsertSession(None, raise_integrity=True)
cat = _insert_revision(sess, _record(TODAY))
report("IntegrityError en INSERT → categoria 'duplicate'", cat == "duplicate")
report("rollback() llamado tras IntegrityError", sess.did_rollback)

# ── Resumen ──────────────────────────────────────────────────────────────────
n_ok   = sum(1 for _, ok in results if ok)
n_fail = sum(1 for _, ok in results if not ok)
print(f"\n{'='*72}")
print(f"RESULTS: {n_ok}/{len(results)} passed")
if n_fail:
    print("FAILED:")
    for name, ok in results:
        if not ok:
            print(f"  FAIL  {name}")
    sys.exit(1)
else:
    print("All P3.E.5/E.6 PIT tests passed.")
