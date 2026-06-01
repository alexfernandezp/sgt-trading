"""
Smoke + regression tests para Step D del módulo gee/ndvi_anomaly.py.

Mock'ea _compute_baseline, _compute_current y _infer_conab_direction:
los tests NO tocan los servidores de Earth Engine ni la BD.

Objetivo: validar orquestación, accumulator de warnings, formato de audit
log, inmutabilidad del resultado y matriz de clasificación.

Uso:
  py scripts/_test_ndvi_anomaly_step_d.py
"""
import logging
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gee.ndvi_anomaly as nda
from gee.ndvi_anomaly import (
    NdviAnomalyResult, MarketBenchmark,
    benchmark_vs_conab_report, compute_ndvi_anomaly,
)
from services.data_quality import DataQualityError

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
NDA_LOGGER = logging.getLogger("gee.ndvi_anomaly")


# ════════════════════════════════════════════════════════════════════════════
# Test infrastructure: mocks + log capture
# ════════════════════════════════════════════════════════════════════════════

class LogCapture(logging.Handler):
    """In-memory log capture sin interferir con el formato global."""
    def __init__(self):
        super().__init__()
        self.records: list[tuple[str, str]] = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))

    def clear(self):
        self.records.clear()

    def has(self, level: str, fragment: str) -> bool:
        return any(lvl == level and fragment in msg for lvl, msg in self.records)


def setup_mocks(*,
                baseline_ndvi=0.62, baseline_coverage=0.92,
                current_ndvi=0.65, current_coverage=0.88,
                infer_direction: str | None = None) -> dict:
    """Instala mocks. Retorna originals para teardown."""
    originals = {
        "_compute_baseline":       nda._compute_baseline,
        "_compute_current":        nda._compute_current,
        "_infer_conab_direction":  nda._infer_conab_direction,
    }
    nda._compute_baseline = lambda c, r, m, target_year=None: {
        "country": c, "region": r, "month": m,
        "baseline_start_year": (target_year or 2026) - 5,
        "baseline_end_year":   (target_year or 2026) - 1,
        "baseline_ndvi": baseline_ndvi,
        "historical_coverage_pct": baseline_coverage,
        "valid_pixel_count": int(2000 * baseline_coverage),
        "pixel_count": 2000,
    }
    nda._compute_current = lambda c, r, y, m: {
        "country": c, "region": r, "year": y, "month": m,
        "current_ndvi": current_ndvi,
        "pixel_coverage_pct": current_coverage,
        "valid_pixel_count": int(2000 * current_coverage),
        "pixel_count": 2000,
    }
    if infer_direction is not None:
        nda._infer_conab_direction = lambda c, r: (infer_direction, True)
    return originals


def teardown_mocks(originals: dict) -> None:
    for name, fn in originals.items():
        setattr(nda, name, fn)


def reset_history(country: str = "BR_TEST", region: str = "BR_SP",
                  anomalies: list[float] | None = None) -> None:
    """Limpia y opcionalmente bootstrap-ea history para esta región."""
    p = nda._history_cache_path(country, region)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.unlink(missing_ok=True)
    if anomalies:
        for i, a in enumerate(anomalies):
            y = 2023 + (i // 12)
            m = (i % 12) + 1
            nda._append_anomaly_to_history(country, region, y, m, a, 0.85)


def cleanup_history(country: str = "BR_TEST", region: str = "BR_SP") -> None:
    nda._history_cache_path(country, region).unlink(missing_ok=True)


# ─── DB mock infra para tests del fallback seasonal (T11-T15) ────────────
def install_db_mock(latest_row: tuple | None,
                    prior_lev1_row: tuple | None = None) -> object:
    """
    Monkey-patch database.SessionLocal para retornar rows determinísticos.

    Schema cane-based (post §7.5.8 refactor):
      latest_row    : tupla (season, lev, pub_date, yoy_cane_pct, cane_total_mt)
                      o None para simular DB vacía
      prior_lev1_row: tupla (cane_total_mt,) o None para simular prior no en DB

    Retorna el SessionLocal original para teardown.
    """
    import database

    class _FakeResult:
        def __init__(self, rows): self._rows = rows
        def fetchall(self):  return self._rows
        def fetchone(self):  return self._rows[0] if self._rows else None

    class _FakeSession:
        def execute(self, stmt, params=None):
            # Query 1 (latest): no params, LIMIT 1
            # Query 2 (prior lev=1): {'ps': '...'}
            if params is None:
                return _FakeResult([latest_row] if latest_row else [])
            return _FakeResult([prior_lev1_row] if prior_lev1_row else [])
        def __enter__(self): return self
        def __exit__(self, *_): pass

    original = database.SessionLocal
    database.SessionLocal = lambda: _FakeSession()
    return original


def teardown_db_mock(original) -> None:
    import database
    database.SessionLocal = original


# ════════════════════════════════════════════════════════════════════════════
# Tests
# ════════════════════════════════════════════════════════════════════════════

results: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    tag = "OK  " if ok else "FAIL"
    print(f"  [{tag}] {name} {detail}")


# ───────────────────────────────────────────────────────────────────────────
# T1: compute_ndvi_anomaly happy path
# ───────────────────────────────────────────────────────────────────────────
print("\n=== T1: compute_ndvi_anomaly happy path ===")
capture = LogCapture(); NDA_LOGGER.addHandler(capture)
originals = setup_mocks()
reset_history(anomalies=[-0.025 + i * 0.002 for i in range(24)])
try:
    r = compute_ndvi_anomaly("BR_TEST", "BR_SP", year=2026, month=5)
    report("is NdviAnomalyResult", isinstance(r, NdviAnomalyResult))
    report("anomaly value", abs(r.anomaly_value - 0.03) < 0.0001,
           f"got={r.anomaly_value}")
    report("percentile not None", r.percentile_rank is not None,
           f"P={r.percentile_rank}")
    report("conviction defined", r.conviction in
           ("HIGH", "MEDIUM", "LOW", "INSUFFICIENT_DATA"),
           f"={r.conviction}")
    report("no warnings (clean run)", r.warnings == ())
    try:
        r.anomaly_value = 0.99
        report("frozen", False, "mutation should have raised")
    except FrozenInstanceError:
        report("frozen", True)
    report("audit log INFO emitted",
           capture.has("INFO", "ndvi_anomaly | BR_TEST_BR_SP"))
finally:
    teardown_mocks(originals); cleanup_history()
    NDA_LOGGER.removeHandler(capture)


# ───────────────────────────────────────────────────────────────────────────
# T2: degraded coverage (40%) → warning accumulator captures it
# ───────────────────────────────────────────────────────────────────────────
print("\n=== T2: degraded coverage (40%) accumulates warning ===")
capture = LogCapture(); NDA_LOGGER.addHandler(capture)
originals = setup_mocks(current_coverage=0.40)
reset_history(anomalies=[-0.025 + i * 0.002 for i in range(24)])
try:
    r = compute_ndvi_anomaly("BR_TEST", "BR_SP", year=2026, month=5)
    report("warnings non-empty", len(r.warnings) > 0,
           f"warnings={r.warnings}")
    report("warning mentions degraded",
           any("degraded" in w for w in r.warnings))
    report("WARNING logged", capture.has("WARNING", "coverage degraded"))
finally:
    teardown_mocks(originals); cleanup_history()
    NDA_LOGGER.removeHandler(capture)


# ───────────────────────────────────────────────────────────────────────────
# T3: critical coverage (15%) raises
# ───────────────────────────────────────────────────────────────────────────
print("\n=== T3: critical coverage (15%) raises DataQualityError ===")
originals = setup_mocks(current_coverage=0.15)
reset_history(anomalies=[-0.025 + i * 0.002 for i in range(24)])
try:
    try:
        compute_ndvi_anomaly("BR_TEST", "BR_SP", year=2026, month=5)
        report("raised DataQualityError", False, "no exception")
    except DataQualityError as e:
        report("raised DataQualityError", True,
               f"field={e.field} value={e.value}")
finally:
    teardown_mocks(originals); cleanup_history()


# ───────────────────────────────────────────────────────────────────────────
# T4: historical coverage <80% raises
# ───────────────────────────────────────────────────────────────────────────
print("\n=== T4: historical coverage <80% raises ===")
originals = setup_mocks(baseline_coverage=0.70)
reset_history(anomalies=[-0.025 + i * 0.002 for i in range(24)])
try:
    try:
        compute_ndvi_anomaly("BR_TEST", "BR_SP", year=2026, month=5)
        report("historical gate raised", False, "no exception")
    except DataQualityError as e:
        report("historical gate raised", True,
               f"field={e.field}")
finally:
    teardown_mocks(originals); cleanup_history()


# ───────────────────────────────────────────────────────────────────────────
# T5: benchmark CONFIRMATION (P>=70 + RECOVERY)
# ───────────────────────────────────────────────────────────────────────────
print("\n=== T5: benchmark CONFIRMATION (P>=70 + RECOVERY) ===")
capture = LogCapture(); NDA_LOGGER.addHandler(capture)
# Anomaly muy positiva vs history toda negativa → P=100
originals = setup_mocks(baseline_ndvi=0.60, current_ndvi=0.70)
reset_history(anomalies=[-0.05 + i * 0.001 for i in range(24)])
try:
    mb = benchmark_vs_conab_report(
        "BR_TEST", "BR_SP", conab_direction="RECOVERY",
        year=2026, month=5,
    )
    report("is MarketBenchmark", isinstance(mb, MarketBenchmark))
    report("market_signal=CONFIRMATION",
           mb.market_signal == "CONFIRMATION",
           f"got={mb.market_signal}")
    report("conab_inferred=False (explicit)", mb.conab_inferred is False)
    report("audit_log_lines tuple", isinstance(mb.audit_log_lines, tuple))
    report("INFO Confirmation logged",
           capture.has("INFO", "Market Confirmation"))
    print("    Audit lines:")
    for line in mb.audit_log_lines:
        print(f"      | {line}")
finally:
    teardown_mocks(originals); cleanup_history()
    NDA_LOGGER.removeHandler(capture)


# ───────────────────────────────────────────────────────────────────────────
# T6: benchmark DIVERGENCE_BEARISH (P<=30 + RECOVERY)
# ───────────────────────────────────────────────────────────────────────────
print("\n=== T6: benchmark DIVERGENCE_BEARISH (P<=30 + RECOVERY) ===")
capture = LogCapture(); NDA_LOGGER.addHandler(capture)
originals = setup_mocks(baseline_ndvi=0.65, current_ndvi=0.55)
reset_history(anomalies=[0.05 + i * 0.001 for i in range(24)])
try:
    mb = benchmark_vs_conab_report(
        "BR_TEST", "BR_SP", conab_direction="RECOVERY",
        year=2026, month=5,
    )
    report("market_signal=DIVERGENCE_BEARISH",
           mb.market_signal == "DIVERGENCE_BEARISH",
           f"got={mb.market_signal}")
    report("WARNING DIVERGENCE_BEARISH logged",
           capture.has("WARNING", "DIVERGENCE_BEARISH"))
    print("    Audit lines:")
    for line in mb.audit_log_lines:
        print(f"      | {line}")
finally:
    teardown_mocks(originals); cleanup_history()
    NDA_LOGGER.removeHandler(capture)


# ───────────────────────────────────────────────────────────────────────────
# T7: benchmark DIVERGENCE_BULLISH (P>=70 + DETERIORATION)
# ───────────────────────────────────────────────────────────────────────────
print("\n=== T7: benchmark DIVERGENCE_BULLISH (P>=70 + DETERIORATION) ===")
capture = LogCapture(); NDA_LOGGER.addHandler(capture)
originals = setup_mocks(baseline_ndvi=0.60, current_ndvi=0.70)
reset_history(anomalies=[-0.05 + i * 0.001 for i in range(24)])
try:
    mb = benchmark_vs_conab_report(
        "BR_TEST", "BR_SP", conab_direction="DETERIORATION",
        year=2026, month=5,
    )
    report("market_signal=DIVERGENCE_BULLISH",
           mb.market_signal == "DIVERGENCE_BULLISH",
           f"got={mb.market_signal}")
    report("WARNING DIVERGENCE_BULLISH logged",
           capture.has("WARNING", "DIVERGENCE_BULLISH"))
    print("    Audit lines:")
    for line in mb.audit_log_lines:
        print(f"      | {line}")
finally:
    teardown_mocks(originals); cleanup_history()
    NDA_LOGGER.removeHandler(capture)


# ───────────────────────────────────────────────────────────────────────────
# T8: benchmark NEUTRAL (STABLE direction)
# ───────────────────────────────────────────────────────────────────────────
print("\n=== T8: benchmark NEUTRAL when CONAB direction=STABLE ===")
originals = setup_mocks(baseline_ndvi=0.60, current_ndvi=0.70,
                        infer_direction="STABLE")
reset_history(anomalies=[-0.05 + i * 0.001 for i in range(24)])
try:
    mb = benchmark_vs_conab_report("BR_TEST", "BR_SP", year=2026, month=5)
    report("market_signal=NEUTRAL", mb.market_signal == "NEUTRAL",
           f"got={mb.market_signal}")
    report("conab_inferred=True", mb.conab_inferred is True)
finally:
    teardown_mocks(originals); cleanup_history()


# ───────────────────────────────────────────────────────────────────────────
# T9: ValueError on invalid conab_direction
# ───────────────────────────────────────────────────────────────────────────
print("\n=== T9: ValueError on invalid conab_direction ===")
try:
    benchmark_vs_conab_report("BR", "BR_SP", conab_direction="INVALID")
    report("ValueError raised", False, "no exception")
except ValueError as e:
    report("ValueError raised", True, f"msg='{str(e)[:60]}...'")


# ───────────────────────────────────────────────────────────────────────────
# T10: Warning propagation from anomaly_result to MarketBenchmark
# ───────────────────────────────────────────────────────────────────────────
print("\n=== T10: warning from anomaly_result propaga a benchmark audit ===")
capture = LogCapture(); NDA_LOGGER.addHandler(capture)
originals = setup_mocks(current_coverage=0.40)   # degraded
reset_history(anomalies=[-0.025 + i * 0.002 for i in range(24)])
try:
    mb = benchmark_vs_conab_report(
        "BR_TEST", "BR_SP", conab_direction="RECOVERY",
        year=2026, month=5,
    )
    report("anomaly_result.warnings non-empty",
           len(mb.anomaly_result.warnings) > 0,
           f"{mb.anomaly_result.warnings}")
    report("warning in audit_log_lines",
           any("propagated" in line for line in mb.audit_log_lines))
    report("WARNING propagated logged",
           capture.has("WARNING", "propagated"))
finally:
    teardown_mocks(originals); cleanup_history()
    NDA_LOGGER.removeHandler(capture)


# ════════════════════════════════════════════════════════════════════════════
# T11-T15: _infer_conab_direction con DB mock (sin GEE, sin DB real)
# ════════════════════════════════════════════════════════════════════════════
from datetime import date as _date

# ───────────────────────────────────────────────────────────────────────────
# T11: PRIMARY path — revision_sugar_pct presente
# ───────────────────────────────────────────────────────────────────────────
print("\n=== T11: PRIMARY yoy_cane_pct=-5 -> DETERIORATION ===")
capture = LogCapture(); NDA_LOGGER.addHandler(capture)
db_orig = install_db_mock(
    # (season, lev, pub_date, yoy_cane_pct=-5%, cane_total_mt=720)
    latest_row=("2025/26", 4, _date(2026, 4, 27), -5.0, 720.0),
)
try:
    direction, inferred = nda._infer_conab_direction("BR", "BR_SP")
    report("primary returns DETERIORATION",
           direction == "DETERIORATION", f"got={direction}")
    report("inferred=True", inferred is True)
    report("source=yoy_cane_pct logged",
           capture.has("INFO", "yoy_cane_pct"))
finally:
    teardown_db_mock(db_orig); NDA_LOGGER.removeHandler(capture)


# ───────────────────────────────────────────────────────────────────────────
# T12: APERTURA DE ZAFRA — lev=1 sin revision, prior lev=1 EN DB
# ───────────────────────────────────────────────────────────────────────────
print("\n=== T12: APERTURA YoY CAÑA (lev=1, prior cane found, +4.29%) -> RECOVERY ===")
capture = LogCapture(); NDA_LOGGER.addHandler(capture)
db_orig = install_db_mock(
    # (season, lev, pub_date, yoy_cane_pct=NULL, cane_total_mt=730)
    latest_row=("2026/27", 1, _date(2026, 4, 28), None, 730.0),
    # prior 2025/26 lev=1: cane=700 Mt → Δ = (730-700)/700 = +4.29% → RECOVERY
    prior_lev1_row=(700.0,),
)
try:
    direction, inferred = nda._infer_conab_direction("BR", "BR_SP")
    report("apertura YoY cane returns RECOVERY",
           direction == "RECOVERY", f"got={direction}")
    report("source=yoy_apertura_cane logged",
           capture.has("INFO", "yoy_apertura_cane"))
    report("prior season reference in log",
           capture.has("INFO", "2025/26"))
finally:
    teardown_db_mock(db_orig); NDA_LOGGER.removeHandler(capture)


# ───────────────────────────────────────────────────────────────────────────
# T13: APERTURA pero prior season lev=1 NO en DB -> STABLE
# ───────────────────────────────────────────────────────────────────────────
print("\n=== T13: APERTURA pero prior lev=1 cane not in DB -> STABLE ===")
capture = LogCapture(); NDA_LOGGER.addHandler(capture)
db_orig = install_db_mock(
    latest_row=("2026/27", 1, _date(2026, 4, 28), None, 730.0),
    prior_lev1_row=None,   # prior no encontrado en DB
)
try:
    direction, inferred = nda._infer_conab_direction("BR", "BR_SP")
    report("apertura missing prior -> STABLE",
           direction == "STABLE", f"got={direction}")
    report("WARNING 'prior season ... cane_total_mt not in DB'",
           capture.has("WARNING", "cane_total_mt not in DB"))
finally:
    teardown_db_mock(db_orig); NDA_LOGGER.removeHandler(capture)


# ───────────────────────────────────────────────────────────────────────────
# T14: ANOMALÍA — revision=NULL en lev != 1 -> STABLE
# ───────────────────────────────────────────────────────────────────────────
print("\n=== T14: yoy_cane_pct=NULL en lev=2 (data anomaly) -> STABLE ===")
capture = LogCapture(); NDA_LOGGER.addHandler(capture)
# pub_date reciente (sino el gate de freshness 130d dispara antes)
db_orig = install_db_mock(
    latest_row=("2025/26", 2, _date(2026, 5, 15), None, 720.0),
)
try:
    direction, inferred = nda._infer_conab_direction("BR", "BR_SP")
    report("lev=2 with NULL yoy_cane_pct -> STABLE",
           direction == "STABLE", f"got={direction}")
    report("WARNING 'non-opening levantamento'",
           capture.has("WARNING", "non-opening levantamento"))
finally:
    teardown_db_mock(db_orig); NDA_LOGGER.removeHandler(capture)


# ───────────────────────────────────────────────────────────────────────────
# T15: APERTURA con delta marginal (<3%) -> STABLE
# ───────────────────────────────────────────────────────────────────────────
print("\n=== T15: APERTURA CAÑA con delta marginal (+1.71%) -> STABLE ===")
capture = LogCapture(); NDA_LOGGER.addHandler(capture)
db_orig = install_db_mock(
    # cane: 712 vs prior 700 → Δ = +1.71% (debajo del threshold ±3%)
    latest_row=("2026/27", 1, _date(2026, 4, 28), None, 712.0),
    prior_lev1_row=(700.0,),
)
try:
    direction, inferred = nda._infer_conab_direction("BR", "BR_SP")
    report("apertura cane marginal delta -> STABLE",
           direction == "STABLE", f"got={direction}")
    report("source=yoy_apertura_cane still used",
           capture.has("INFO", "yoy_apertura_cane"))
finally:
    teardown_db_mock(db_orig); NDA_LOGGER.removeHandler(capture)


# ════════════════════════════════════════════════════════════════════════════
# T16: _load_anomaly_history self-inclusion fix
# ════════════════════════════════════════════════════════════════════════════
print("\n=== T16: _load_anomaly_history excludes self (year, month) ===")
country, region = "TEST_BR", "T16_REGION"
p = nda._history_cache_path(country, region)
p.parent.mkdir(parents=True, exist_ok=True)
p.unlink(missing_ok=True)
nda._append_anomaly_to_history(country, region, 2024, 6, -0.05, 0.9)
nda._append_anomaly_to_history(country, region, 2024, 7,  0.02, 0.9)
nda._append_anomaly_to_history(country, region, 2024, 8,  0.10, 0.9)

# Sin exclude: 3 entries
h_all = nda._load_anomaly_history(country, region)
report("default load returns 3 entries", len(h_all) == 3, f"got {len(h_all)}")

# Exclude (2024, 7): 2 entries, +0.02 removido
h_excl = nda._load_anomaly_history(
    country, region, exclude_year=2024, exclude_month=7,
)
report("exclude (2024,7) returns 2 entries", len(h_excl) == 2, f"got {len(h_excl)}")
report("excluded value 0.02 not in history",
       not any(abs(v - 0.02) < 1e-6 for v in h_excl))

# Exclude no-match: 3 entries
h_nomatch = nda._load_anomaly_history(
    country, region, exclude_year=1999, exclude_month=1,
)
report("non-matching exclude returns all 3", len(h_nomatch) == 3)

# Solo un kwarg → no filtra (defensiva)
h_partial = nda._load_anomaly_history(country, region, exclude_year=2024)
report("only exclude_year (no month) -> no filter", len(h_partial) == 3)

p.unlink(missing_ok=True)


# ════════════════════════════════════════════════════════════════════════════
# Summary
# ════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"RESULTS: {passed}/{total} passed")
if passed < total:
    print("\nFailures:")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAIL: {name} {detail}")
    sys.exit(1)
print("All Step D tests passed.")
