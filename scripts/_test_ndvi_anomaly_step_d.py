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
    nda._compute_baseline = lambda c, r, m: {
        "country": c, "region": r, "month": m,
        "baseline_start_year": 2021, "baseline_end_year": 2025,
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
