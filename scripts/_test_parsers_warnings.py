"""
P2.B — Tests para _parse_pct, _parse_price (cepea) y _parse_int (santos).

Valida:
  1. Inputs válidos parsean correctamente (sin regresión)
  2. Inputs malformados retornan None Y disparan WARNING vía parse_log_warning
  3. Edge cases (whitespace, empty, None) manejados sin excepciones

Uso: py scripts/_test_parsers_warnings.py
"""
import logging
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.cepea import _parse_pct, _parse_price
from ingestion.santos_port import _parse_int

# WARN capture — sigue al logger compartido (services.data_quality)
captured: list[tuple[str, str]] = []


class _Cap(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.WARNING:
            captured.append((record.name, record.getMessage()))


logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
# parse_log_warning loguea en services.data_quality logger
logging.getLogger("services.data_quality").addHandler(_Cap())


results: list[tuple[str, bool]] = []


def report(name: str, ok: bool, detail: str = ""):
    results.append((name, ok))
    tag = "OK  " if ok else "FAIL"
    print(f"  [{tag}] {name} {detail}")


# ── _parse_pct ──────────────────────────────────────────────────────────
print("\n=== cepea._parse_pct ===")
captured.clear()
report("'5.5%' -> 5.5", _parse_pct("5.5%") == 5.5)
report("'-3,10' -> -3.10 (Brazilian decimal)", _parse_pct("-3,10") == -3.10)
report("'  2.5  ' (whitespace)", _parse_pct("  2.5  ") == 2.5)
report("'abc' -> None", _parse_pct("abc") is None)
report("'' -> None (empty)", _parse_pct("") is None)

# Verify WARNING fired for invalid inputs
warns_pct = [m for _, m in captured if "cepea._parse_pct" in m]
report("WARNING fired for invalid pct", len(warns_pct) >= 2,
       f"got {len(warns_pct)} warnings: {warns_pct[:1]}")


# ── _parse_price ────────────────────────────────────────────────────────
print("\n=== cepea._parse_price ===")
captured.clear()
report("'18.6800' -> 18.68", _parse_price("18.6800") == 18.68)
report("'1,234.56' -> 1234.56 (commas stripped)",
       _parse_price("1,234.56") == 1234.56)
report("'  25.5  ' (whitespace)", _parse_price("  25.5  ") == 25.5)
report("'abc' -> None", _parse_price("abc") is None)
report("'' -> None (empty)", _parse_price("") is None)

warns_price = [m for _, m in captured if "cepea._parse_price" in m]
report("WARNING fired for invalid price", len(warns_price) >= 2,
       f"got {len(warns_price)} warnings")


# ── santos_port._parse_int ──────────────────────────────────────────────
print("\n=== santos_port._parse_int ===")
captured.clear()
report("'12345' -> 12345", _parse_int("12345") == 12345)
report("'12,345 t' -> 12345 (comma+unit stripped)",
       _parse_int("12,345 t") == 12345)
report("'55000 ton' -> 55000", _parse_int("55000 ton") == 55000)
report("'abc' -> None", _parse_int("abc") is None)
# Empty string: split()[0] raises IndexError → caught
report("'' -> None (empty, IndexError caught)", _parse_int("") is None)
report("'   ' -> None (whitespace only)", _parse_int("   ") is None)

warns_int = [m for _, m in captured if "santos_port._parse_int" in m]
report("WARNING fired for invalid int", len(warns_int) >= 2,
       f"got {len(warns_int)} warnings")


# ── Summary ─────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
passed = sum(1 for _, ok in results if ok)
total = len(results)
print(f"RESULTS: {passed}/{total} passed")
if passed < total:
    for name, ok in results:
        if not ok:
            print(f"  FAIL: {name}")
    sys.exit(1)
print("All parser warning tests passed.")
