"""
Descarga masiva de datos historicos ICE Sugar No.11 desde Databento.
Maximiza el credito de bienvenida ($125) descargando 7+ anos de historia.

Datos disponibles en IFUS.IMPACT: desde 2018-12-23 hasta hoy.

Plan de descarga (coste estimado):
  ohlcv-1d  SB.c.0  2018-hoy  ~$0.39   -> price_history   (barras diarias ICE oficiales)
  ohlcv-1h  SB.c.0  2018-hoy  ~$3.91   -> price_bars 1h   (agrupable a 4h)
  ohlcv-1m  SB.c.0  2018-hoy  ~$14.46  -> price_bars 1m   (agrupable a 5m/30m/1h/4h)
  ---------------------------------------------------------------
  TOTAL                        ~$18.76  (de $125 disponibles)

Uso:
  py scripts/databento_download.py            # muestra costes y pide confirmacion
  py scripts/databento_download.py --yes      # descarga sin preguntar
  py scripts/databento_download.py --ingest-only  # solo reingestar ficheros ya descargados
"""
import sys, os, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from database import SessionLocal
from config   import DATABENTO_API_KEY
from ingestion.databento import (
    estimate_costs,
    download_ohlcv,
    ingest_daily_from_file,
    ingest_intraday_from_file,
    RAW_DIR,
)

START = "2018-12-23"   # primer dia disponible en IFUS.IMPACT
END   = "2026-05-19"   # ultimo dia completo accesible (plan usage-based)

PLAN = [
    # (schema,    interval_en_BD, descripcion)
    ("ohlcv-1d", "1d",  "Barras diarias    -> price_history"),
    ("ohlcv-1h", "1h",  "Barras horarias   -> price_bars[1h]"),
    ("ohlcv-1m", "1m",  "Barras 1-minuto   -> price_bars[1m]"),
]


def show_cost_table(costs: dict):
    print()
    print("  SCHEMA        COSTE EST.   DESTINO")
    print("  " + "-" * 60)
    total = 0.0
    for schema, interval, desc in PLAN:
        cost = costs.get(schema)
        if cost is not None:
            total += cost
            print("  %-13s  $%7.2f    %s" % (schema, cost, desc))
        else:
            print("  %-13s  ERROR        %s" % (schema, desc))
    print("  " + "-" * 60)
    print("  %-13s  $%7.2f    (de $125 disponibles)" % ("TOTAL", total))
    print()


def confirm(msg: str) -> bool:
    resp = input(msg + " [s/N]: ").strip().lower()
    return resp in ("s", "si", "y", "yes")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes",          action="store_true", help="No pedir confirmacion")
    parser.add_argument("--ingest-only",  action="store_true", help="Solo reingestar ficheros ya descargados")
    parser.add_argument("--skip-ingest",  action="store_true", help="Solo descargar, no ingestar en BD")
    parser.add_argument("--schema",       help="Descargar solo este schema (ohlcv-1d / ohlcv-1h / ohlcv-1m)")
    args = parser.parse_args()

    if not DATABENTO_API_KEY:
        print("ERROR: DATABENTO_API_KEY no definida en .env")
        sys.exit(1)

    print("=" * 65)
    print("  DATABENTO DOWNLOAD — ICE Sugar No.11 (IFUS.IMPACT)")
    print("  Periodo: %s  ->  %s" % (START, END))
    print("=" * 65)

    # Filtrar plan si se pide un schema especifico
    plan = PLAN if not args.schema else [p for p in PLAN if p[0] == args.schema]
    if not plan:
        print("ERROR: schema '%s' no reconocido" % args.schema)
        sys.exit(1)

    # -----------------------------------------------------------------
    # 1. Estimar costes
    # -----------------------------------------------------------------
    if not args.ingest_only:
        print("\nEstimando costes (sin cargo)...")
        schemas_to_check = [p[0] for p in plan]
        costs_raw = {}
        for schema in schemas_to_check:
            try:
                import databento as db
                client = db.Historical(DATABENTO_API_KEY)
                costs_raw[schema] = client.metadata.get_cost(
                    dataset="IFUS.IMPACT",
                    symbols=["SB.c.0"],
                    stype_in="continuous",
                    schema=schema,
                    start=START,
                    end=END,
                )
            except Exception as e:
                costs_raw[schema] = None
                print("  WARN: no se pudo estimar %s: %s" % (schema, e))

        show_cost_table(costs_raw)

        if not args.yes:
            if not confirm("Proceder con la descarga?"):
                print("Cancelado.")
                return

    # -----------------------------------------------------------------
    # 2. Descargar cada schema
    # -----------------------------------------------------------------
    downloaded_files = {}

    if not args.ingest_only:
        print("\n-- DESCARGA --")
        for schema, interval, desc in plan:
            print("  [%s]  %s" % (schema, desc))
            try:
                path = download_ohlcv(
                    api_key=DATABENTO_API_KEY,
                    schema=schema,
                    start=START,
                    end=END,
                )
                size_mb = path.stat().st_size / 1_048_576
                print("  -> %s  (%.1f MB)" % (path.name, size_mb))
                downloaded_files[schema] = path
            except Exception as e:
                print("  ERROR descargando %s: %s" % (schema, e))
    else:
        # Modo ingest-only: buscar ficheros ya descargados
        print("\n-- MODO INGEST-ONLY: buscando ficheros en %s --" % RAW_DIR)
        for schema, interval, desc in plan:
            tag  = schema.replace("-", "_")
            yr0  = START[:4]
            yr1  = END[:4]
            path = RAW_DIR / ("sb_cont_%s_%s_%s.dbn.zst" % (tag, yr0, yr1))
            if path.exists():
                downloaded_files[schema] = path
                print("  Encontrado: %s" % path.name)
            else:
                print("  NO encontrado: %s (ejecuta sin --ingest-only primero)" % path.name)

    if args.skip_ingest or not downloaded_files:
        if not downloaded_files:
            print("\nNo hay ficheros para ingestar.")
        else:
            print("\n-- skip-ingest activo, no se inserta en BD --")
        return

    # -----------------------------------------------------------------
    # 3. Ingestar en BD
    # -----------------------------------------------------------------
    print("\n-- INGESTION EN BASE DE DATOS --")
    session = SessionLocal()

    try:
        for schema, interval, desc in plan:
            path = downloaded_files.get(schema)
            if not path:
                continue

            print("  [%s]  ingiriendo..." % schema, end=" ", flush=True)
            try:
                if schema == "ohlcv-1d":
                    n = ingest_daily_from_file(session, path)
                    print("OK  [%d filas en price_history]" % n)
                else:
                    n = ingest_intraday_from_file(session, path, interval)
                    print("OK  [%d barras en price_bars[%s]]" % (n, interval))
            except Exception as e:
                print("ERROR: %s" % e)
                import traceback; traceback.print_exc()

    finally:
        session.close()

    print()
    print("Listo. Datos Databento disponibles en la BD.")
    print()
    print("Proximos pasos sugeridos:")
    print("  py scripts/databento_download.py --schema statistics  (settlements + OI, ~$58)")
    print("  py scripts/explore_databento.py                        (ver lo que hay en BD)")


if __name__ == "__main__":
    main()
