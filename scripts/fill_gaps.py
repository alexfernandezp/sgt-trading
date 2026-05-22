"""
Rellena los huecos del continuo SB.c.0 (julio + oct/nov/dic cada año)
usando contratos trimestrales _Z con instrument_id exacto.

Coste estimado: ~$1.00 total para 2019-2025.

Uso:
  py scripts/fill_gaps.py             # descarga e ingesta todos los huecos
  py scripts/fill_gaps.py --dry-run   # solo muestra costes sin descargar
"""
import sys, os, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import databento as db
import pandas as pd
from pathlib import Path
from database import SessionLocal
from config import DATABENTO_API_KEY
from ingestion.databento import ingest_intraday_from_file, RAW_DIR

DATASET = "IFUS.IMPACT"
DEF_PATH = Path("data/databento_raw/sb_fut_definition.dbn.zst")


def load_instrument_ids() -> dict:
    """Devuelve {raw_symbol_prefix: instrument_id} para todos los _Z."""
    store = db.DBNStore.from_file(str(DEF_PATH))
    df    = store.to_df()
    z     = df[df["raw_symbol"].str.endswith("_Z", na=False)].copy()
    result = {}
    for _, row in z.iterrows():
        # Extraer prefijo limpio p.ej. "FMV0023" de "SB  FMV0023_Z"
        raw = row["raw_symbol"].strip()          # quitar espacios leading/trailing
        key = raw.replace("SB  ", "").replace("_Z", "").strip()
        result[key] = int(row["instrument_id"])
    return result


# Periodos de hueco a rellenar
# Julio → FMV del mismo año (expira sep)
# Oct-Dic → FMH del año siguiente (expira feb)
GAP_PLAN = [
    # (etiqueta, start, end_exclusive, prefijo_contrato)
    ("Jul 2019",      "2019-07-01", "2019-08-01", "FMV0019"),
    ("Oct-Dic 2019",  "2019-10-01", "2020-01-01", "FMH0020"),
    ("Jul 2020",      "2020-07-01", "2020-08-01", "FMV0020"),
    ("Oct-Dic 2020",  "2020-10-01", "2021-01-01", "FMH0021"),
    ("Jul 2021",      "2021-07-01", "2021-08-01", "FMV0021"),
    ("Oct-Dic 2021",  "2021-10-01", "2022-01-01", "FMH0022"),
    ("Jul 2022",      "2022-07-01", "2022-08-01", "FMV0022"),
    ("Oct-Dic 2022",  "2022-10-01", "2023-01-01", "FMH0023"),
    ("Jul 2023",      "2023-07-01", "2023-08-01", "FMV0023"),
    ("Oct-Dic 2023",  "2023-10-01", "2024-01-01", "FMH0024"),
    ("Jul 2024",      "2024-07-01", "2024-08-01", "FMV0024"),
    ("Oct-Dic 2024",  "2024-10-01", "2025-01-01", "FMH0025"),
    ("Jul 2025",      "2025-07-01", "2025-08-01", "FMV0025"),
    ("Oct-Dic 2025",  "2025-10-01", "2026-01-01", "FMH0026"),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Solo mostrar costes, sin descargar")
    args = parser.parse_args()

    if not DATABENTO_API_KEY:
        print("ERROR: DATABENTO_API_KEY no definida en .env")
        sys.exit(1)

    print("Cargando instrument_ids desde definiciones...", end=" ", flush=True)
    ids = load_instrument_ids()
    print("OK  [%d contratos _Z]" % len(ids))
    print()

    client = db.Historical(DATABENTO_API_KEY)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    total_cost = 0.0
    plan_rows  = []

    # Estimar costes
    print("Estimando costes (sin cargo)...")
    print("  %-16s  %-10s  %-10s  %12s" % ("PERIODO", "CONTRATO", "INSTR_ID", "COSTE_1m"))
    print("  " + "-" * 55)
    for label, start, end, prefix in GAP_PLAN:
        iid = ids.get(prefix)
        if iid is None:
            print("  %-16s  %-10s  NO ENCONTRADO" % (label, prefix))
            continue
        try:
            cost = client.metadata.get_cost(
                dataset=DATASET,
                symbols=[str(iid)],
                stype_in="instrument_id",
                schema="ohlcv-1m",
                start=start,
                end=end,
            )
            total_cost += cost
            plan_rows.append((label, start, end, prefix, iid, cost))
            print("  %-16s  %-10s  %-10d  $%10.4f" % (label, prefix, iid, cost))
        except Exception as e:
            print("  %-16s  %-10s  ERROR: %s" % (label, prefix, e))

    print("  " + "-" * 55)
    print("  %-16s  %-10s  %-10s  $%10.4f" % ("TOTAL", "", "", total_cost))
    print()

    if args.dry_run:
        print("[dry-run] Nada descargado.")
        return

    print("Presupuesto total: $%.4f  — procediendo con descarga..." % total_cost)
    print()

    session = SessionLocal()
    total_bars = 0

    try:
        for label, start, end, prefix, iid, cost in plan_rows:
            tag  = prefix.lower()
            path = RAW_DIR / f"gap_{tag}_{start[:7]}_{end[:7]}.dbn.zst"

            # Descargar
            if path.exists():
                print("[%s]  fichero ya existe, reutilizando..." % label)
            else:
                print("[%s]  descargando $%.4f..." % (label, cost), end=" ", flush=True)
                client.timeseries.get_range(
                    dataset=DATASET,
                    symbols=[str(iid)],
                    stype_in="instrument_id",
                    schema="ohlcv-1m",
                    start=start,
                    end=end,
                    path=str(path),
                )
                print("OK  [%.2f KB]" % (path.stat().st_size / 1024))

            # Ingestar
            print("  ingiriendo en BD...", end=" ", flush=True)
            try:
                n = ingest_intraday_from_file(session, path, interval="1m")
                total_bars += n
                print("OK  [%d barras]" % n)
            except Exception as e:
                print("ERROR: %s" % e)
            print()

    finally:
        session.close()

    print("=" * 50)
    print("TOTAL BARRAS INGRESADAS: %d" % total_bars)
    print()
    print("Ejecuta ahora:")
    print("  py scripts/aggregate_bars.py")
    print("para regenerar los intervalos 5m / 30m / 4h con los nuevos datos.")


if __name__ == "__main__":
    main()
