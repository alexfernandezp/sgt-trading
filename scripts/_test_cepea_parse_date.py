"""
Unit tests for cepea._parse_date — date disambiguation + future-date guard.

Validan que:
  - El comportamiento histórico (MM/DD wins en past dates ambiguas) se preserva
  - El bug del row futuro 2026-10-04 queda cerrado (DD/MM fallback)
  - Casos límite (ambos futuros, garbage, day>12) producen None correctamente

Uso: py scripts/_test_cepea_parse_date.py
"""
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.cepea import _parse_date

# Capturador de WARNINGS para validar audit log
captured_warnings: list[str] = []


class _WarnCapture(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.WARNING:
            captured_warnings.append(record.getMessage())


logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
_handler = _WarnCapture()
logging.getLogger("ingestion.cepea").addHandler(_handler)


results: list[tuple[str, bool]] = []


def report(name: str, actual, expected, *, also_assert: bool = True):
    ok = actual == expected
    results.append((name, ok))
    tag = "OK  " if ok else "FAIL"
    print(f"  [{tag}] {name}: got={actual!r} expected={expected!r}")


print("=" * 80)
print("  cepea._parse_date — disambiguation + future-date guard")
print("=" * 80)
today = date.today()
print(f"\n  Today: {today}  |  Future threshold: {today + timedelta(days=7)}")


# ── T1 — Normal MM/DD past (legacy preserved) ───────────────────────────────
print("\nT1: Normal MM/DD past — '05/22/2026'")
report("MM/DD May 22 2026", _parse_date("05/22/2026"), date(2026, 5, 22))


# ── T2 — BUG FIX: ambiguous future MM/DD → DD/MM past ───────────────────────
print("\nT2: BUG FIX — '10/04/2026' (MM/DD future, DD/MM past)")
captured_warnings.clear()
result = _parse_date("10/04/2026")
report("DD/MM April 10 2026", result, date(2026, 4, 10))
warn_logged = any("is future" in w and "retrying DD/MM" in w
                  for w in captured_warnings)
report("WARNING 'retrying DD/MM' logged", warn_logged, True)


# ── T3 — Day > 12 forces DD/MM ─────────────────────────────────────────────
print("\nT3: Day > 12 — '25/04/2026'")
report("DD/MM April 25 2026", _parse_date("25/04/2026"), date(2026, 4, 25))


# ── T4 — Both ambiguous past → MM/DD wins (legacy preserved) ────────────────
print("\nT4: Both ambiguous past — '03/04/2026' (legacy MM/DD wins)")
report("MM/DD March 4 2026", _parse_date("03/04/2026"), date(2026, 3, 4))


# ── T5 — Garbage input ─────────────────────────────────────────────────────
print("\nT5: Garbage input — 'hola mundo'")
report("None", _parse_date("hola mundo"), None)


# ── T6 — Both interpretations future → reject ──────────────────────────────
print("\nT6: Both future — '10/12/2027'")
captured_warnings.clear()
report("Both future → None", _parse_date("10/12/2027"), None)
warn_logged = any("DD/MM" in w and "also future" in w
                  for w in captured_warnings)
report("WARNING 'also future' logged", warn_logged, True)


# ── T7 — Weekly prefix format ──────────────────────────────────────────────
print("\nT7: Weekly prefix — '18 - 05/22/2026'")
report("Weekly prefix May 22", _parse_date("18 - 05/22/2026"), date(2026, 5, 22))


# ── T8 — Today edge case ───────────────────────────────────────────────────
today_str = today.strftime("%m/%d/%Y")
print(f"\nT8: Today edge case — '{today_str}'")
report("Today valid", _parse_date(today_str), today)


# ── T9 — Invalid mes y dia ───────────────────────────────────────────────
print("\nT9: Invalid — '33/13/2026'")
report("None (mes 13 + day 33 invalid)", _parse_date("33/13/2026"), None)


# ── Summary ────────────────────────────────────────────────────────────────
print("\n" + "=" * 80)
passed = sum(1 for _, ok in results if ok)
total = len(results)
print(f"RESULTS: {passed}/{total} passed")
if passed < total:
    print("\nFailures:")
    for name, ok in results:
        if not ok:
            print(f"  FAIL: {name}")
    sys.exit(1)
print("All cepea._parse_date tests passed.")
