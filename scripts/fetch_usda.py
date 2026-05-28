"""
Descarga datos USDA FAS PSD para azúcar centrífugo.

Uso:
  py scripts/fetch_usda.py              # años actual + anterior
  py scripts/fetch_usda.py --years 5   # backfill 5 años
  py scripts/fetch_usda.py --bulk      # forzar bulk Excel aunque haya API key
  py scripts/fetch_usda.py --query     # consultar balance mundial desde BD (sin descarga)
"""
import sys, os, argparse, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

from database import SessionLocal
from ingestion.usda_psd import fetch_usda_psd, get_world_balance, get_country_production


def main():
    parser = argparse.ArgumentParser(description="Descarga USDA PSD para azúcar")
    parser.add_argument("--years", type=int, default=2,
                        help="Número de años a descargar (default: 2 = actual + anterior)")
    parser.add_argument("--bulk", action="store_true",
                        help="Forzar descarga bulk Excel aunque haya API key")
    parser.add_argument("--query", action="store_true",
                        help="Solo consultar BD, sin descargar")
    args = parser.parse_args()

    session = SessionLocal()

    if args.query:
        _print_balance(session)
        session.close()
        return

    from datetime import date
    current_year = date.today().year
    years = list(range(current_year - args.years + 1, current_year + 1))

    print("=" * 60)
    print("  USDA FAS PSD -- Azucar Centrifugo (0612000)")
    print("  Años: %s" % years)
    print("  Modo: %s" % ("bulk Excel" if args.bulk else "API -> bulk fallback"))
    print("=" * 60)

    result = fetch_usda_psd(session, years=years, force_bulk=args.bulk)

    print("\n  Fuente     : %s" % result["source"])
    print("  Rows upsert: %d" % result["rows_upserted"])
    print("  Años       : %s" % result["years_fetched"])
    if result["errors"]:
        for e in result["errors"]:
            print("  [!] %s" % e)

    if result["rows_upserted"] > 0:
        print()
        _print_balance(session)

    session.close()


def _print_balance(session):
    """Imprime el balance mundial más reciente desde la BD."""
    wb = get_world_balance(session)
    if not wb:
        print("  Sin datos en BD. Ejecutar sin --query primero.")
        return

    mkt_yr  = wb["marketing_year"]
    prod    = wb.get("production_mt")
    cons    = wb.get("consumption_mt")
    end_s   = wb.get("ending_stocks_mt")
    beg_s   = wb.get("beginning_stocks_mt")
    exports = wb.get("exports_mt")
    stu     = wb.get("stocks_to_use_pct")

    print("  BALANCE MUNDIAL AZÚCAR — Año %s/%s" % (mkt_yr, str(mkt_yr + 1)[2:]))
    print("  " + "-" * 44)
    if beg_s  is not None: print("  Stocks iniciales  : %6.2f Mt" % beg_s)
    if prod   is not None: print("  Producción        : %6.2f Mt" % prod)
    if cons   is not None: print("  Consumo           : %6.2f Mt" % cons)
    if exports is not None: print("  Exportaciones     : %6.2f Mt" % exports)
    if end_s  is not None: print("  Stocks finales    : %6.2f Mt" % end_s)
    if stu    is not None:
        if stu < 32:
            stu_tag = "MERCADO AJUSTADO - alcista"
        elif stu < 38:
            stu_tag = "zona equilibrada"
        else:
            stu_tag = "excedente - presion bajista"
        print("  Stocks/Uso (STU)  : %6.1f%%  [%s]" % (stu, stu_tag))

    # Principales productores (último año disponible)
    print()
    print("  Producción por país (último año):")
    for cc, name in [("BR", "Brasil"), ("IN", "India"), ("TH", "Tailandia"),
                     ("EU", "UE"), ("CN", "China"), ("AU", "Australia")]:
        rows = get_country_production(session, cc, n_years=2)
        if rows:
            last = rows[-1]
            prev = rows[-2] if len(rows) >= 2 else None
            chg_s = ""
            if prev and prev["production_mt"] and last["production_mt"]:
                chg = (last["production_mt"] - prev["production_mt"]) / prev["production_mt"] * 100
                chg_s = "  (%+.1f%% YoY)" % chg
            print("    %-12s : %.2f Mt%s" % (name, last["production_mt"], chg_s))


if __name__ == "__main__":
    main()
