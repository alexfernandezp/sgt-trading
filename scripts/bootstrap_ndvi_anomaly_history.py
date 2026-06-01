"""
Bootstrap del historial de NDVI anomalies para regiones CONAB.

Razón (BUSINESS_LOGIC §7.5.6):
  Primera corrida de compute_ndvi_anomaly() devuelve conviction=INSUFFICIENT_DATA
  porque robust_stats necesita ≥10 observaciones en su rolling window. Este script
  computa retroactivamente los últimos N meses (default 24 = 2 zafras completas)
  para cada región, poblando el history JSON.

Iteración oldest-first: la primera anomalía computada no tiene historia previa
(percentile=N/A), pero cada iteración siguiente acumula. Al final, el último mes
debería tener n_historical_obs ≈ N−1.

Caché behavior:
  - Baseline (TTL 180d): 1 entry por (region, calendar_month, target_year).
    Para 24 meses spanning 2-3 años: ~3 unique target_years per calendar_month
    × 12 months × N regions baseline entries.
  - Current (TTL 7d): N regions × 24 months entries.
  - History (append-only): se preserva entre corridas; dedup por (year, month).

Idempotencia: re-ejecutar el script es seguro. Cache hits hacen las re-corridas
casi instantáneas; las anomalías ya en history se sobrescriben dedupedly.

Graceful degradation: DataQualityError en una (region, year, month) se loguea
como WARNING y se salta. Summary final muestra OK/Skipped por región.

Uso:
  py scripts/bootstrap_ndvi_anomaly_history.py [--region BR_SP] [--months 24]

  Sin args: corre las 5 regiones × 24 meses.
"""
import argparse
import logging
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

# Forzar UTF-8 (Windows cp1252 rompe box chars)
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ee
from dotenv import load_dotenv

load_dotenv()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--region", type=str, default=None,
        help="Region ID (ej. BR_SP). Default: todas las regiones de CONAB_REGIONS",
    )
    p.add_argument(
        "--months", type=int, default=24,
        help="Número de meses retrospectivos a computar (default: 24)",
    )
    p.add_argument(
        "--country", type=str, default="BR",
        help="Country code (default: BR)",
    )
    return p.parse_args()


def _months_back(n: int) -> list[tuple[int, int]]:
    """
    Lista de (year, month) para los últimos n meses completos, oldest-first.
    Excluye el mes actual incompleto.

    Ej. si today=2026-06-15 y n=24: returns [(2024, 6), (2024, 7), ..., (2026, 5)].
    """
    today = date.today()
    latest_complete = today.replace(day=1) - timedelta(days=1)
    months = []
    y, m = latest_complete.year, latest_complete.month
    for _ in range(n):
        months.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    months.reverse()
    return months


def _format_anomaly(anomaly: float | None) -> str:
    return f"{anomaly:+.4f}" if anomaly is not None else "  N/A"


def _format_percentile(p: float | None) -> str:
    return f"P={p:5.1f}" if p is not None else "P= N/A"


def main() -> int:
    args = _parse_args()

    # ── Pre-flight GEE init ───────────────────────────────────────────────
    project_id = os.getenv("GEE_PROJECT_ID")
    if not project_id:
        print("[ERROR] GEE_PROJECT_ID no está en .env — abortando")
        return 1

    print("=" * 78)
    print("  Bootstrap NDVI Anomaly History")
    print("=" * 78)
    print(f"\n[Pre-flight] Inicializando GEE (project={project_id})...")
    t0 = time.perf_counter()
    ee.Initialize(project=project_id)
    init_elapsed = time.perf_counter() - t0
    print(f"            GEE init: {init_elapsed:.2f}s")

    # Logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    # Import después de ee.Initialize
    import gee.ndvi_anomaly as nda
    from gee.ndvi_anomaly import (
        CONAB_REGIONS, compute_ndvi_anomaly,
    )
    from services.data_quality import DataQualityError

    # ── Decidir regiones a procesar ───────────────────────────────────────
    if args.region:
        if args.region not in CONAB_REGIONS:
            print(f"[ERROR] region {args.region!r} no existe en CONAB_REGIONS")
            print(f"        Válidas: {sorted(CONAB_REGIONS.keys())}")
            return 1
        regions = [args.region]
    else:
        regions = sorted(CONAB_REGIONS.keys())

    months = _months_back(args.months)
    total_ops = len(regions) * len(months)

    print(f"\n[Config] regions={regions}")
    print(f"         months window: {months[0][0]}-{months[0][1]:02d} → "
          f"{months[-1][0]}-{months[-1][1]:02d}  ({len(months)} meses)")
    print(f"         total ops to attempt: {total_ops}")

    # ── Bootstrap loop ────────────────────────────────────────────────────
    global_t0 = time.perf_counter()
    results_per_region: dict[str, dict] = {}

    for region in regions:
        print("\n" + "─" * 78)
        print(f"[Region {region}] {CONAB_REGIONS[region]['display_name']}")
        print("─" * 78)
        ok, skipped = 0, 0
        skip_reasons: list[str] = []
        region_t0 = time.perf_counter()

        for i, (y, m) in enumerate(months, 1):
            iter_t0 = time.perf_counter()
            try:
                result = compute_ndvi_anomaly(
                    args.country, region, year=y, month=m,
                )
                iter_elapsed = time.perf_counter() - iter_t0
                ok += 1
                print(
                    f"  [{i:2d}/{len(months)}] {y}-{m:02d}  OK   "
                    f"anomaly={_format_anomaly(result.anomaly_value)}  "
                    f"{_format_percentile(result.percentile_rank)}  "
                    f"conviction={result.conviction:<17}  "
                    f"n_hist={result.n_historical_obs:2d}  ({iter_elapsed:5.1f}s)"
                )
                if result.warnings:
                    for w in result.warnings:
                        print(f"            ⚠ {w}")
            except DataQualityError as e:
                iter_elapsed = time.perf_counter() - iter_t0
                skipped += 1
                reason = f"{e.field}={e.value}"
                skip_reasons.append(f"{y}-{m:02d}: {reason}")
                print(
                    f"  [{i:2d}/{len(months)}] {y}-{m:02d}  SKIP  "
                    f"DataQualityError: {reason}  ({iter_elapsed:.1f}s)"
                )
            except Exception as e:
                iter_elapsed = time.perf_counter() - iter_t0
                skipped += 1
                reason = f"{type(e).__name__}: {e}"
                skip_reasons.append(f"{y}-{m:02d}: {reason}")
                print(
                    f"  [{i:2d}/{len(months)}] {y}-{m:02d}  ERROR "
                    f"{reason}  ({iter_elapsed:.1f}s)"
                )

        region_elapsed = time.perf_counter() - region_t0
        results_per_region[region] = {
            "ok": ok, "skipped": skipped, "elapsed": region_elapsed,
            "skip_reasons": skip_reasons,
        }
        print(
            f"\n  [{region}] DONE: {ok}/{len(months)} OK, {skipped} skipped, "
            f"{region_elapsed:.1f}s total"
        )

    # ── Telemetría global + history dumps ─────────────────────────────────
    global_elapsed = time.perf_counter() - global_t0
    print("\n" + "═" * 78)
    print("BOOTSTRAP SUMMARY")
    print("═" * 78)
    print(f"  Total elapsed (incl. GEE init):  {init_elapsed + global_elapsed:7.1f}s")
    print(f"  Per-region breakdown:")
    for r, info in results_per_region.items():
        rate = (info["ok"] / args.months) * 100
        print(
            f"    {r:8s}: {info['ok']:2d}/{args.months} OK ({rate:5.1f}%)  "
            f"skipped={info['skipped']:2d}  elapsed={info['elapsed']:6.1f}s"
        )
        if info["skip_reasons"]:
            for r_msg in info["skip_reasons"][:5]:   # cap a 5 por brevedad
                print(f"              · {r_msg}")
            if len(info["skip_reasons"]) > 5:
                print(f"              · ... ({len(info['skip_reasons']) - 5} more)")

    # ── Dump del último history file por región ───────────────────────────
    print("\n  Final history JSON sizes:")
    for r in results_per_region:
        hp = nda._history_cache_path(args.country, r)
        if hp.exists():
            data = hp.read_text(encoding="utf-8")
            import json as _json
            parsed = _json.loads(data)
            n = len(parsed.get("anomalies", []))
            print(f"    {r:8s}: {hp.stat().st_size:5d} bytes, {n:2d} anomalies")
        else:
            print(f"    {r:8s}: history NOT created")

    print("\n" + "═" * 78)
    print("Bootstrap complete.")
    print("═" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
