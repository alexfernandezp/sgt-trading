"""
Step E Smoke Test — pipeline GEE en vivo, BR_SP Mayo 2024.

Protocolo:
  1. Inicializa GEE con project del .env
  2. Limpia caches previos de BR_SP / 2024-05
  3. Corrida 1 (Cold Cache): mide latencia, lee climatología 5yr de GEE
  4. Corrida 2 (Warm Cache): inmediatamente después, debe ser Cache HIT
  5. Telemetría: tiempos exactos, contenido JSON, audit log completo

Uso: py scripts/_smoke_ndvi_anomaly_step_e.py
"""
import logging
import os
import sys
import time
from pathlib import Path

# Forzar UTF-8 en stdout (Windows cp1252 rompe box-drawing chars)
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── Pre-flight: GEE init ──────────────────────────────────────────────────
import ee
from dotenv import load_dotenv

load_dotenv()
project_id = os.getenv("GEE_PROJECT_ID")
if not project_id:
    print("[ERROR] GEE_PROJECT_ID no está en .env — abortando")
    sys.exit(1)

print("=" * 75)
print("  Step E Smoke Test — BR_SP / Mayo 2024 / Cold vs Warm Cache")
print("=" * 75)

print(f"\n[Pre-flight] Inicializando GEE (project={project_id})...")
t0 = time.perf_counter()
try:
    ee.Initialize(project=project_id)
except Exception as e:
    print(f"[ERROR] GEE Initialize failed: {e}")
    print("        Si nunca corriste setup_gee.py, ejecuta:")
    print("        py scripts/setup_gee.py")
    sys.exit(1)
init_elapsed = time.perf_counter() - t0
print(f"          GEE init: {init_elapsed:.2f}s")

# ── Logging detallado para auditar el pipeline ────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
# Silenciar HTTP noise de Google API
logging.getLogger("googleapiclient").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# ── Importar el módulo DESPUÉS de ee.Initialize ───────────────────────────
import gee.ndvi_anomaly as nda
from gee.ndvi_anomaly import benchmark_vs_conab_report

# ── Configuración del test ────────────────────────────────────────────────
COUNTRY = "BR"
REGION  = "BR_SP"
YEAR    = 2024
MONTH   = 5

# Para esta corrida el _compute_baseline anchora a current_year=2026:
# baseline_start = 2021, baseline_end = 2025 (5yr climatology)
BASELINE_START = 2021
BASELINE_END   = 2025


def clear_caches_for_target():
    """Borra caches del target region-month para forzar Cold run."""
    paths = {
        "baseline": nda._baseline_cache_path(
            COUNTRY, REGION, MONTH, BASELINE_START, BASELINE_END
        ),
        "current":  nda._current_cache_path(COUNTRY, REGION, YEAR, MONTH),
        "history":  nda._history_cache_path(COUNTRY, REGION),
    }
    for label, p in paths.items():
        if p.exists():
            p.unlink()
            print(f"          [Cleared] {label}: {p.name}")
        else:
            print(f"          [Not present] {label}: {p.name}")
    return paths


def dump_cache_files(paths_dict):
    """Reporta tamaño de los archivos cache después de cada run."""
    for label, p in paths_dict.items():
        if p.exists():
            size = p.stat().st_size
            print(f"          {label:<10}: {size:>5} bytes  ({p.name})")
        else:
            print(f"          {label:<10}: MISSING ({p.name})")


def dump_result(label: str, result):
    """Imprime el contenido relevante del MarketBenchmark."""
    ar = result.anomaly_result
    print(f"\n          Result [{label}]:")
    print(f"            market_signal     = {result.market_signal}")
    print(f"            conab_direction   = {result.conab_direction}")
    print(f"            conab_inferred    = {result.conab_inferred}")
    print(f"            anomaly_value     = {ar.anomaly_value:+.4f}")
    print(f"            current_ndvi      = {ar.current_ndvi:.4f}")
    print(f"            baseline_ndvi     = {ar.baseline_ndvi:.4f}")
    print(f"            pixel_coverage    = {ar.pixel_coverage_pct:.1%}")
    print(f"            historical_cov    = {ar.historical_coverage_pct:.1%}")
    print(f"            percentile_rank   = {ar.percentile_rank}")
    print(f"            modified_z        = {ar.modified_z}")
    print(f"            conviction        = {ar.conviction}")
    print(f"            n_historical_obs  = {ar.n_historical_obs}")
    print(f"            warnings          = {ar.warnings}")
    print(f"          Audit log lines [{label}]:")
    for line in result.audit_log_lines:
        print(f"            | {line}")


# ════════════════════════════════════════════════════════════════════════════
# Phase 0: clean state
# ════════════════════════════════════════════════════════════════════════════
print("\n" + "─" * 75)
print("[Phase 0] Clearing caches for clean cold start")
print("─" * 75)
target_paths = clear_caches_for_target()


# ════════════════════════════════════════════════════════════════════════════
# Phase 1: COLD CACHE RUN
# ════════════════════════════════════════════════════════════════════════════
print("\n" + "─" * 75)
print("[Phase 1] COLD CACHE RUN — full GEE roundtrip")
print("─" * 75)
print("          (climatología 5yr 2021-2025 + current may-2024 + reduceRegion)\n")

t_cold = time.perf_counter()
try:
    result_cold = benchmark_vs_conab_report(
        COUNTRY, REGION, year=YEAR, month=MONTH,
    )
except Exception as e:
    print(f"\n[ERROR] Cold run failed: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
elapsed_cold = time.perf_counter() - t_cold

print(f"\n>>> COLD RUN ELAPSED: {elapsed_cold:.2f}s <<<")
dump_result("COLD", result_cold)

print("\n          Cache files post-cold:")
dump_cache_files(target_paths)


# ════════════════════════════════════════════════════════════════════════════
# Phase 2: WARM CACHE RUN (inmediatamente después)
# ════════════════════════════════════════════════════════════════════════════
print("\n" + "─" * 75)
print("[Phase 2] WARM CACHE RUN — debe ser cache HIT en baseline + current")
print("─" * 75)

t_warm = time.perf_counter()
result_warm = benchmark_vs_conab_report(
    COUNTRY, REGION, year=YEAR, month=MONTH,
)
elapsed_warm = time.perf_counter() - t_warm

print(f"\n>>> WARM RUN ELAPSED: {elapsed_warm*1000:.0f}ms <<<")
speedup = elapsed_cold / elapsed_warm if elapsed_warm > 0 else float("inf")
print(f">>> SPEEDUP: {speedup:.1f}x <<<")
dump_result("WARM", result_warm)


# ════════════════════════════════════════════════════════════════════════════
# Phase 3: Telemetría + JSON dump
# ════════════════════════════════════════════════════════════════════════════
print("\n" + "═" * 75)
print("[Phase 3] TELEMETRY SUMMARY")
print("═" * 75)
print(f"          GEE Initialize:        {init_elapsed:>8.2f}s")
print(f"          Cold cache run:        {elapsed_cold:>8.2f}s")
print(f"          Warm cache run:        {elapsed_warm*1000:>8.0f}ms"
      f"  ({elapsed_warm:.4f}s)")
print(f"          Speedup factor:        {speedup:>8.1f}x")

# JSON dump del history
print("\n[Phase 3] Contenido del JSON inmutable de history:")
print("─" * 75)
hist_path = target_paths["history"]
if hist_path.exists():
    print(hist_path.read_text(encoding="utf-8"))
else:
    print("[!] History file not present")

# Verificar inmutabilidad
print("\n[Phase 3] Verificación de inmutabilidad del MarketBenchmark:")
print("─" * 75)
from dataclasses import FrozenInstanceError
try:
    result_cold.market_signal = "ALTERED"
    print("[FAIL] frozen=True debería haber bloqueado mutación")
except FrozenInstanceError:
    print("[OK] FrozenInstanceError — MarketBenchmark inmutable")

print("\n" + "═" * 75)
print("Step E smoke test complete.")
print("═" * 75)
