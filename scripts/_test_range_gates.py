"""
P2.C — Range Gates de price_usd (cepea) y tonelajes (santos).

Valida:
  1. _get_price_range retorna tuplas correctas para cada substring de series_name
  2. _validate_cepea_price acepta valores in-range, rechaza out-of-range + WARNING
  3. _validate_santos_tonnage acepta None / in-range, rechaza extremos + WARNING
  4. Tests de inyección de datos corruptos garantizan que NUNCA llegan al upsert
     (el validator retorna False antes de añadir al batch / antes del session.execute)

Uso: py scripts/_test_range_gates.py
"""
import logging
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.cepea import _get_price_range, _validate_cepea_price
from ingestion.santos_port import _validate_santos_tonnage

# WARN capture
captured: list[str] = []


class _Cap(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.WARNING:
            captured.append(record.getMessage())


logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
logging.getLogger("services.data_quality").addHandler(_Cap())


results: list[tuple[str, bool]] = []


def report(name: str, ok: bool, detail: str = ""):
    results.append((name, ok))
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name} {detail}")


# ── _get_price_range — heurística por substring ──────────────────────────
print("\n=== _get_price_range ===")
report("sugar bag50kg -> [1.0, 100.0]",
       _get_price_range("crystal_sugar_usd_bag50kg") == (1.0, 100.0))
report("sugar2 bag50kg -> [1.0, 100.0]",
       _get_price_range("crystal_sugar2_usd_bag50kg") == (1.0, 100.0))
report("paulinia m3 -> [50.0, 2000.0]",
       _get_price_range("hydrous_paulinia_usd_m3") == (50.0, 2000.0))
report("anhydrous_liter -> [0.05, 2.5]",
       _get_price_range("anhydrous_usd_liter") == (0.05, 2.5))
report("hydrous_fuel_liter -> [0.05, 2.0]",
       _get_price_range("hydrous_fuel_usd_liter") == (0.05, 2.0))


# ── _validate_cepea_price — sugar boundary tests ─────────────────────────
print("\n=== _validate_cepea_price — sugar ===")
captured.clear()
report("sugar 25.0 (valid)",
       _validate_cepea_price(25.0, "crystal_sugar_usd_bag50kg") is True)
report("sugar -5.0 (negative) -> REJECT",
       _validate_cepea_price(-5.0, "crystal_sugar_usd_bag50kg") is False)
report("sugar 500.0 (>100) -> REJECT",
       _validate_cepea_price(500.0, "crystal_sugar_usd_bag50kg") is False)
report("sugar 100.01 (just over 100) -> REJECT",
       _validate_cepea_price(100.01, "crystal_sugar_usd_bag50kg") is False)
report("sugar 1.0 (lower boundary) -> ACCEPT",
       _validate_cepea_price(1.0, "crystal_sugar_usd_bag50kg") is True)
n_warn = sum(1 for w in captured
             if "cepea" in w and ("sugar" in w.lower()))
report("3 WARNINGs disparados para 3 rechazos sugar",
       n_warn == 3, f"got {n_warn}")


# ── _validate_cepea_price — ethanol m3 boundary tests ────────────────────
print("\n=== _validate_cepea_price — ethanol paulinia m3 ===")
captured.clear()
report("paulinia 500.0 (valid)",
       _validate_cepea_price(500.0, "hydrous_paulinia_usd_m3") is True)
report("paulinia 10.0 (<50) -> REJECT",
       _validate_cepea_price(10.0, "hydrous_paulinia_usd_m3") is False)
report("paulinia 3000.0 (>2000) -> REJECT",
       _validate_cepea_price(3000.0, "hydrous_paulinia_usd_m3") is False)


# ── _validate_cepea_price — ethanol liter ────────────────────────────────
print("\n=== _validate_cepea_price — ethanol per-liter ===")
report("anhydrous 2.3 (valid, between 0.05 and 2.5)",
       _validate_cepea_price(2.3, "anhydrous_usd_liter") is True)
report("hydrous_fuel 2.5 (>2.0 reject)",
       _validate_cepea_price(2.5, "hydrous_fuel_usd_liter") is False)
report("hydrous_other 0.5 (valid)",
       _validate_cepea_price(0.5, "hydrous_other_usd_liter") is True)
report("hydrous_other 0.01 (<0.05 reject)",
       _validate_cepea_price(0.01, "hydrous_other_usd_liter") is False)


# ── _validate_santos_tonnage — boundary tests ────────────────────────────
print("\n=== _validate_santos_tonnage ===")
captured.clear()
report("ship 50000/50000 (both valid)",
       _validate_santos_tonnage(50_000, 50_000, "TEST_SHIP") is True)
report("ship None/None (legitimate missing, allow)",
       _validate_santos_tonnage(None, None, "TEST_SHIP") is True)
report("ship 80000/None (valid + None mixed)",
       _validate_santos_tonnage(80_000, None, "TEST_SHIP") is True)
report("ship -100 load -> REJECT",
       _validate_santos_tonnage(-100, 50_000, "TEST_SHIP") is False)
report("ship 5_000_000 load -> REJECT (5M tons absurd)",
       _validate_santos_tonnage(5_000_000, 50_000, "TEST_SHIP") is False)
report("ship 50000 / 5_000_000 weight -> REJECT",
       _validate_santos_tonnage(50_000, 5_000_000, "TEST_SHIP") is False)
report("ship 200_001 (just over cap) -> REJECT",
       _validate_santos_tonnage(200_001, 50_000, "TEST_SHIP") is False)
n_warn_santos = sum(1 for w in captured if "santos_port" in w)
report("WARNINGs disparados para tonelajes corruptos",
       n_warn_santos >= 4, f"got {n_warn_santos}")


# ── Inyección de payload corrupto — protección DB (integración) ──────────
print("\n=== DB protection via validator filter (integration) ===")
# Simulamos un batch mixto: 2 válidos + 3 corruptos. Filtramos con el
# validator. Solo los válidos pasan. Demuestra que el upsert nunca recibe basura.
synthetic_cepea = [
    ("crystal_sugar_usd_bag50kg", 22.5),    # valid
    ("crystal_sugar_usd_bag50kg", -10.0),   # invalid (negative)
    ("crystal_sugar_usd_bag50kg", 999.0),   # invalid (>100)
    ("hydrous_paulinia_usd_m3", 750.0),     # valid
    ("hydrous_paulinia_usd_m3", 5000.0),    # invalid (>2000)
]
filtered_cepea = [
    (s, p) for s, p in synthetic_cepea if _validate_cepea_price(p, s)
]
report("CEPEA filter pasa exactamente 2 de 5",
       len(filtered_cepea) == 2, f"got {len(filtered_cepea)}")
report("CEPEA filter keeps only valid prices",
       filtered_cepea == [
           ("crystal_sugar_usd_bag50kg", 22.5),
           ("hydrous_paulinia_usd_m3", 750.0),
       ])

synthetic_santos = [
    (50_000, 60_000, "VALID_1"),
    (-100, 50_000, "NEG_LOAD"),
    (5_000_000, 50_000, "ABSURD_LOAD"),
    (50_000, -1, "NEG_WEIGHT"),
    (80_000, None, "VALID_2"),    # None permitido
]
filtered_santos = [
    t for t in synthetic_santos
    if _validate_santos_tonnage(t[0], t[1], t[2])
]
report("SANTOS filter pasa exactamente 2 de 5",
       len(filtered_santos) == 2, f"got {len(filtered_santos)}")
report("SANTOS filter keeps only valid ships",
       [t[2] for t in filtered_santos] == ["VALID_1", "VALID_2"])


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
print("All range gate tests passed.")
