"""
P2.E — Freshness Gates (BUSINESS_LOGIC §4.1).

Valida que las lecturas públicas degradan limpiamente cuando la última fila
en DB está más antigua que `max_age_days`:

  cepea.get_latest_cepea:
    - dato fresh dentro de [today - 5d, today] → incluido en dict
    - dato stale (today - >5d) → omitido del dict + WARNING
    - boundary exacto today-5d → incluido (<=, no <)
    - mixto fresh+stale → solo frescos en dict, WARNINGs por stale
    - todas las series stale → dict vacío

  santos_port.get_latest_snapshot:
    - snapshot dentro de today - 2d → dict completo
    - snapshot today-3d → None + WARNING
    - boundary today-2d exacto → incluido
    - sin rows → None sin WARNING

  Primitive (validate_freshness):
    - None timestamp → DataQualityError
    - audit log carga source/field/value/expected

Uso: py scripts/_test_freshness_gates.py
"""
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.cepea import get_latest_cepea, CEPEA_MAX_AGE_DAYS
from ingestion.santos_port import get_latest_snapshot, SANTOS_MAX_AGE_DAYS
from services.data_quality import DataQualityError, validate_freshness


# ── WARNING capture ──────────────────────────────────────────────────────
captured: list[tuple[str, str]] = []  # (logger_name, message)


class _Cap(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.WARNING:
            captured.append((record.name, record.getMessage()))


logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(name)s - %(message)s")
for name in ("services.data_quality", "ingestion.cepea", "ingestion.santos_port"):
    logging.getLogger(name).addHandler(_Cap())


results: list[tuple[str, bool]] = []


def report(name: str, ok: bool, detail: str = ""):
    results.append((name, ok))
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name} {detail}")


# ── Session stub: solo necesita .execute(text(...)).fetchall() ───────────
class _ExecResult:
    def __init__(self, rows): self._rows = rows
    def fetchall(self): return self._rows


class _StubSession:
    """Reemplazo mínimo de sqlalchemy.orm.Session para tests sin DB real."""
    def __init__(self, rows): self._rows = rows
    def execute(self, _stmt): return _ExecResult(self._rows)


# ── Reference date fija para reproducibilidad ────────────────────────────
TODAY = date(2026, 6, 2)


def _cepea_row(series_name: str, days_old: int, price: float = 25.0):
    """
    Fabrica una row con la forma esperada por get_latest_cepea:
      (series_name, price_usd, price_date, unit, pct_d, pct_w, pct_m)
    """
    price_date = TODAY - timedelta(days=days_old)
    return (series_name, price, price_date, "US$/unit", 0.5, None, 1.2)


# ─────────────────────────────────────────────────────────────────────────
# validate_freshness primitive
# ─────────────────────────────────────────────────────────────────────────
print("\n=== validate_freshness primitive ===")
captured.clear()

# Fresh → OK
ok_val = validate_freshness(
    TODAY - timedelta(days=3), max_age_days=5,
    source="test", field="ts", reference=TODAY,
)
report("3d old vs 5d limit → returns timestamp",
       ok_val == TODAY - timedelta(days=3))

# Boundary exacto → OK
ok_b = validate_freshness(
    TODAY - timedelta(days=5), max_age_days=5,
    source="test", field="ts", reference=TODAY,
)
report("5d old boundary (==max_age) → returns timestamp",
       ok_b == TODAY - timedelta(days=5))

# Stale → raises
try:
    validate_freshness(
        TODAY - timedelta(days=10), max_age_days=5,
        source="test", field="ts", reference=TODAY,
    )
    report("10d old vs 5d limit → DataQualityError", False, "no raise")
except DataQualityError as e:
    detail = f"source={e.source} field={e.field} value={e.value}"
    report("10d old vs 5d limit → DataQualityError", True, detail)
    report("error carga 'data is 10d stale'",
           "10d stale" in str(e))

# None → raises
try:
    validate_freshness(
        None, max_age_days=5, source="test", field="ts", reference=TODAY,
    )
    report("None timestamp → DataQualityError", False, "no raise")
except DataQualityError as e:
    report("None timestamp → DataQualityError", True, f"msg={e}")

# Datetime también funciona
ok_dt = validate_freshness(
    datetime.combine(TODAY - timedelta(days=2), datetime.min.time()),
    max_age_days=5, source="test", field="ts", reference=TODAY,
)
report("datetime (no date) input → OK", ok_dt is not None)


# ─────────────────────────────────────────────────────────────────────────
# get_latest_cepea — freshness gate por serie
# ─────────────────────────────────────────────────────────────────────────
print("\n=== get_latest_cepea — single fresh serie ===")
captured.clear()
sess = _StubSession([_cepea_row("hydrous_paulinia_usd_m3", days_old=3, price=750.0)])
out = get_latest_cepea(sess, reference=TODAY)
report("1 fresh row (3d) → presente en dict",
       "hydrous_paulinia_usd_m3" in out)
report("price_usd preservado",
       out.get("hydrous_paulinia_usd_m3", {}).get("price_usd") == 750.0)
report("ningún WARNING para fresh",
       not any("DataQualityError" in m for _, m in captured))


print("\n=== get_latest_cepea — single stale serie ===")
captured.clear()
sess = _StubSession([_cepea_row("hydrous_paulinia_usd_m3", days_old=10, price=750.0)])
out = get_latest_cepea(sess, reference=TODAY)
report("1 stale row (10d) → ausente del dict",
       "hydrous_paulinia_usd_m3" not in out)
report("dict vacío",
       out == {})
stale_warn = [m for _, m in captured if "latest_price_date" in m]
report("1 WARNING emitido", len(stale_warn) == 1, f"got {len(stale_warn)}")
report("WARNING menciona source=cepea",
       any("source='cepea'" in m or "source=cepea" in m for _, m in captured))


print("\n=== get_latest_cepea — boundary exacto (5d) ===")
captured.clear()
sess = _StubSession([
    _cepea_row("crystal_sugar_usd_bag50kg", days_old=CEPEA_MAX_AGE_DAYS, price=22.0),
])
out = get_latest_cepea(sess, reference=TODAY)
report("boundary exacto (==max_age_days) → ACEPTADO",
       "crystal_sugar_usd_bag50kg" in out)


print("\n=== get_latest_cepea — boundary+1 (6d) ===")
captured.clear()
sess = _StubSession([
    _cepea_row("crystal_sugar_usd_bag50kg", days_old=CEPEA_MAX_AGE_DAYS + 1, price=22.0),
])
out = get_latest_cepea(sess, reference=TODAY)
report("boundary+1 (>max_age_days) → RECHAZADO",
       "crystal_sugar_usd_bag50kg" not in out)


print("\n=== get_latest_cepea — mixto fresh + stale ===")
captured.clear()
sess = _StubSession([
    _cepea_row("hydrous_paulinia_usd_m3",    days_old=2,  price=800.0),  # fresh
    _cepea_row("crystal_sugar_usd_bag50kg",  days_old=8,  price=20.0),   # stale
    _cepea_row("anhydrous_usd_liter",        days_old=4,  price=0.95),   # fresh
    _cepea_row("hydrous_fuel_usd_liter",     days_old=15, price=0.85),   # stale
])
out = get_latest_cepea(sess, reference=TODAY)
report("series frescas (paulinia + anhydrous) presentes",
       "hydrous_paulinia_usd_m3" in out and "anhydrous_usd_liter" in out)
report("series stale (sugar + hydrous_fuel) ausentes",
       "crystal_sugar_usd_bag50kg" not in out
       and "hydrous_fuel_usd_liter" not in out)
report("dict tiene exactamente 2 entradas",
       len(out) == 2, f"got {len(out)}")
n_stale_warn = sum(1 for _, m in captured if "latest_price_date" in m)
report("2 WARNINGs emitidos (uno por stale serie)",
       n_stale_warn == 2, f"got {n_stale_warn}")


print("\n=== get_latest_cepea — todas stale → dict vacío ===")
captured.clear()
sess = _StubSession([
    _cepea_row("hydrous_paulinia_usd_m3",   days_old=20),
    _cepea_row("crystal_sugar_usd_bag50kg", days_old=30),
])
out = get_latest_cepea(sess, reference=TODAY)
report("dict vacío cuando todas stale", out == {})
report("2 WARNINGs (uno por serie)",
       sum(1 for _, m in captured if "latest_price_date" in m) == 2)


print("\n=== get_latest_cepea — sin rows ===")
captured.clear()
sess = _StubSession([])
out = get_latest_cepea(sess, reference=TODAY)
report("empty DB → dict vacío", out == {})
report("sin WARNINGs (no hay nada que validar)",
       not any("latest_price_date" in m for _, m in captured))


# ─────────────────────────────────────────────────────────────────────────
# get_latest_snapshot — freshness gate de Santos (veto total)
# ─────────────────────────────────────────────────────────────────────────
def _santos_row(snapshot_date: date, page: str = "berthed",
                ship: str = "TEST_SHIP"):
    return (
        page, ship, "ACUCAR CRISTAL", "TECON", "Long",
        None, 50_000, 60_000, "", "VOY1", snapshot_date,
    )


print("\n=== get_latest_snapshot — fresh snapshot (1d) ===")
captured.clear()
sess = _StubSession([_santos_row(TODAY - timedelta(days=1))])
out = get_latest_snapshot(sess, reference=TODAY)
report("snapshot 1d old → retorna dict",
       out is not None and out.get("snapshot_date") == str(TODAY - timedelta(days=1)))
report("dict contiene métricas de berthed",
       out is not None and out.get("n_berthed") == 1)
report("sin WARNINGs",
       not any("latest_snapshot_date" in m for _, m in captured))


print("\n=== get_latest_snapshot — boundary exacto (2d) ===")
captured.clear()
sess = _StubSession([_santos_row(TODAY - timedelta(days=SANTOS_MAX_AGE_DAYS))])
out = get_latest_snapshot(sess, reference=TODAY)
report("boundary exacto (==max_age_days) → ACEPTADO",
       out is not None)


print("\n=== get_latest_snapshot — stale snapshot (5d) ===")
captured.clear()
sess = _StubSession([_santos_row(TODAY - timedelta(days=5))])
out = get_latest_snapshot(sess, reference=TODAY)
report("snapshot 5d old → None (veto total)", out is None)
santos_warn = [m for _, m in captured if "latest_snapshot_date" in m]
report("1 WARNING emitido", len(santos_warn) == 1, f"got {len(santos_warn)}")
report("WARNING menciona source=santos_port",
       any("santos_port" in m for _, m in captured))


print("\n=== get_latest_snapshot — boundary+1 (3d) ===")
captured.clear()
sess = _StubSession([_santos_row(TODAY - timedelta(days=SANTOS_MAX_AGE_DAYS + 1))])
out = get_latest_snapshot(sess, reference=TODAY)
report("boundary+1 (3d > 2d) → None", out is None)


print("\n=== get_latest_snapshot — empty DB ===")
captured.clear()
sess = _StubSession([])
out = get_latest_snapshot(sess, reference=TODAY)
report("empty DB → None sin WARN",
       out is None
       and not any("latest_snapshot_date" in m for _, m in captured))


# ─────────────────────────────────────────────────────────────────────────
# Audit log structural verification
# ─────────────────────────────────────────────────────────────────────────
print("\n=== Audit log estructura ===")
captured.clear()
sess = _StubSession([_cepea_row("hydrous_paulinia_usd_m3", days_old=10, price=750.0)])
get_latest_cepea(sess, reference=TODAY)
audit = [m for _, m in captured if "DataQualityError" in m][:1]
report("audit log carga source", any("source=" in m for m in audit))
report("audit log carga field", any("field=" in m for m in audit))
report("audit log carga value (price_date)", any("value=" in m for m in audit))
report("audit log carga expected ('<= 5d old')",
       any("5d old" in m for m in audit))


# ── Summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 72)
passed = sum(1 for _, ok in results if ok)
total = len(results)
print(f"RESULTS: {passed}/{total} passed")
if passed < total:
    for name, ok in results:
        if not ok:
            print(f"  FAIL: {name}")
    sys.exit(1)
print("All freshness gate tests passed.")
