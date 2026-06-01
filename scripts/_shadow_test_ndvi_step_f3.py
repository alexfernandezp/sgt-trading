"""
Shadow Test F.3 — Cross-validación end-to-end CONAB cane vs NDVI satélite.

Objetivo:
  Para cada uno de los 24 meses del bootstrap BR_SP, re-computar anomaly +
  benchmark vs CONAB direction (cane-based PRIMARY). Validar la matriz
  Confirmation/Divergence con datos reales.

Pipeline por mes:
  1. compute_ndvi_anomaly(year, month) — usa caches (rápido)
  2. _load_anomaly_history con self-inclusion exclude — percentile sin bias
  3. benchmark_vs_conab_report — inferencia real cane-based de la DB
  4. Clasificar market_signal

Esperado:
  - CONAB direction = RECOVERY (current 2026/27 lev=1: yoy_cane_pct=+5.30%)
  - Meses con P ≥ 70 (anomaly positivo extremo) → CONFIRMATION
  - Meses con P ≤ 30 (sept-oct 2024 drought) → DIVERGENCE_BEARISH (alpha real)
  - Resto → NEUTRAL

Uso: py scripts/_shadow_test_ndvi_step_f3.py
"""
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ee
from dotenv import load_dotenv

load_dotenv()
project_id = os.getenv("GEE_PROJECT_ID")

print("=" * 88)
print("  Shadow Test F.3 — BR_SP cross-validation CONAB cane vs NDVI satélite")
print("=" * 88)

print(f"\n[Pre-flight] GEE init (project={project_id})...")
t_init = time.perf_counter()
ee.Initialize(project=project_id)
print(f"            init: {time.perf_counter() - t_init:.2f}s")

# Logging level INFO suficiente para audit
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s %(message)s",
)
logging.getLogger("googleapiclient").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
# Silenciar el módulo durante el loop (cada call ya genera mucho); solo
# muestro resumen tabular
logging.getLogger("gee.ndvi_anomaly").setLevel(logging.WARNING)

import gee.ndvi_anomaly as nda
from gee.ndvi_anomaly import (
    benchmark_vs_conab_report, _infer_conab_direction,
)

COUNTRY = "BR"
REGION  = "BR_SP"

# Lista de los 24 meses del bootstrap (oldest first)
HISTORY_MONTHS = [
    (2024, 6),  (2024, 7),  (2024, 8),  (2024, 9),  (2024, 10),
    (2024, 11), (2024, 12), (2025, 1),  (2025, 2),  (2025, 3),
    (2025, 4),  (2025, 5),  (2025, 6),  (2025, 7),  (2025, 8),
    (2025, 9),  (2025, 10), (2025, 11), (2025, 12), (2026, 1),
    (2026, 2),  (2026, 3),  (2026, 4),  (2026, 5),
]

print(f"\n[Phase 0] Pre-flight CONAB direction (queried once, used for all months):")
print("-" * 88)
direction_today, inferred_today = _infer_conab_direction(COUNTRY, REGION)
print(f"  CONAB direction (today): {direction_today}  (inferred={inferred_today})")

print(f"\n[Phase 1] Re-computing 24 months with cane-based benchmark...")
print("-" * 88)

results = []
t_total = time.perf_counter()

for y, m in HISTORY_MONTHS:
    t0 = time.perf_counter()
    try:
        mb = benchmark_vs_conab_report(COUNTRY, REGION, year=y, month=m)
        elapsed = time.perf_counter() - t0
        ar = mb.anomaly_result
        results.append({
            "year": y, "month": m,
            "anomaly":    ar.anomaly_value,
            "percentile": ar.percentile_rank,
            "modified_z": ar.modified_z,
            "conviction": ar.conviction,
            "conab_dir":  mb.conab_direction,
            "signal":     mb.market_signal,
            "elapsed":    elapsed,
        })
    except Exception as e:
        results.append({
            "year": y, "month": m,
            "error": f"{type(e).__name__}: {e}",
            "elapsed": time.perf_counter() - t0,
        })

elapsed_total = time.perf_counter() - t_total

# ── Tabla principal ──────────────────────────────────────────────────────
print()
print(f"  {'Month':10} {'Anomaly':>9} {'Pct':>6} {'mZ':>6}  "
      f"{'Conviction':<18} {'CONAB':<10} {'Market Signal':<22} {'t':>6}")
print("  " + "─" * 86)
for r in results:
    if "error" in r:
        print(f"  {r['year']}-{r['month']:02d}    ERROR: {r['error']}")
        continue
    pct = f"{r['percentile']:5.1f}" if r['percentile'] is not None else "  N/A"
    mz  = f"{r['modified_z']:+5.2f}" if r['modified_z'] is not None else "  N/A"
    # Mark divergencias e high-conviction
    mark = ""
    if "DIVERGENCE" in r['signal']:
        mark = " ★"
    elif r['conviction'] == "HIGH":
        mark = " ◉"
    print(
        f"  {r['year']}-{r['month']:02d}      "
        f"{r['anomaly']:+8.4f} {pct:>6} {mz:>6}  "
        f"{r['conviction']:<18} {r['conab_dir']:<10} {r['signal']:<22}{mark} "
        f"{r['elapsed']:5.2f}s"
    )

# ── Summary ──────────────────────────────────────────────────────────────
print()
print("═" * 88)
print("[Phase 2] Market Signal Summary")
print("═" * 88)

from collections import Counter
signal_counts = Counter(r["signal"] for r in results if "signal" in r)
print(f"\n  Signal distribution over 24 months:")
for sig, count in signal_counts.most_common():
    pct = count / 24 * 100
    print(f"    {sig:<22}: {count:>2}  ({pct:>5.1f}%)")

# ── Alphas ───────────────────────────────────────────────────────────────
print(f"\n  DIVERGENCE signals (alpha-generating):")
divergences = [r for r in results
               if "signal" in r and "DIVERGENCE" in r["signal"]]
if divergences:
    for r in divergences:
        print(
            f"    [{r['signal']}] {r['year']}-{r['month']:02d}: "
            f"anomaly={r['anomaly']:+.4f} P={r['percentile']:.1f} "
            f"mZ={r['modified_z']:+.2f}  CONAB={r['conab_dir']}"
        )
else:
    print("    (none detected)")

print(f"\n  CONFIRMATION signals (validación CONAB):")
confirms = [r for r in results
            if "signal" in r and r["signal"] == "CONFIRMATION"]
if confirms:
    for r in confirms[:5]:
        print(
            f"    [CONFIRMATION] {r['year']}-{r['month']:02d}: "
            f"anomaly={r['anomaly']:+.4f} P={r['percentile']:.1f} "
            f"mZ={r['modified_z']:+.2f}"
        )
    if len(confirms) > 5:
        print(f"    ... ({len(confirms) - 5} more)")
else:
    print("    (none detected)")

# ── Telemetría ───────────────────────────────────────────────────────────
n_ok = sum(1 for r in results if "signal" in r)
n_err = sum(1 for r in results if "error" in r)
print(f"\n  Telemetría:")
print(f"    Total processed     : {n_ok}/24")
print(f"    Errors              : {n_err}")
print(f"    Wall time           : {elapsed_total:.1f}s ({elapsed_total/24:.2f}s/iter)")

print("\n" + "═" * 88)
print("Shadow Test F.3 complete.")
print("═" * 88)
