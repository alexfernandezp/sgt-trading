"""
UNICA Quinzenal — CLI de ingestion y consulta.

Uso:
  py scripts/ingest_unica.py            # descarga y parsea reporte mas reciente
  py scripts/ingest_unica.py --show     # muestra ultimo reporte parseado
  py scripts/ingest_unica.py --idm N    # descarga reporte especifico por idM
"""
import sys, os, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def cmd_fetch(idm: int = None):
    from ingestion.unica import scrape_latest_idm, download_pdf, parse_unica_pdf

    if idm is None:
        print("  Buscando reporte mas reciente en unicadata.com.br...")
        idm = scrape_latest_idm()
        if not idm:
            print("  [ERROR] No se pudo obtener el idM del reporte actual.")
            return

    print("  Descargando PDF (idM=%d)..." % idm)
    pdf_bytes = download_pdf(idm)
    if not pdf_bytes:
        print("  [ERROR] Descarga fallida.")
        return

    print("  Parseando PDF...")
    data = parse_unica_pdf(pdf_bytes)
    if not data:
        print("  [ERROR] Parse fallido.")
        return

    data["idm_source"] = idm
    _print_report(data)


def _print_report(data: dict):
    print()
    print("=" * 65)
    print("  UNICA — Acompanhamento Quinzenal Centro-Sul")
    print("=" * 65)
    print("  Safra           : %s" % data.get("safra", "?"))
    print("  Quinzena        : %d  (mes %02d/%d)" % (
        data.get("quinzena_num", 0),
        data.get("ref_month", 0),
        data.get("ref_year", 0),
    ))
    pos = data.get("position_date")
    if pos:
        print("  Posicion hasta  : %s" % pos.strftime("%d/%m/%Y"))
    print()
    print("  ACUMULADO SAFRA — Centro-Sul")
    print("  " + "-" * 45)
    if data.get("cane_cumulative_mt") is not None:
        print("  Cana molida     : {:>8.3f} Mt".format(data["cane_cumulative_mt"]))
    if data.get("sugar_cumulative_mt") is not None:
        print("  Azucar          : {:>8.3f} Mt".format(data["sugar_cumulative_mt"]))
    if data.get("ethanol_total_ml") is not None:
        print("  Etanol total    : {:>8,.0f} M litros".format(data["ethanol_total_ml"]))
    if data.get("atr_kg_t") is not None:
        print("  ATR             : {:>8.2f} kg/t".format(data["atr_kg_t"]))
    if data.get("mix_ethanol_pct") is not None:
        print("  Mix etanol      : {:>8.2f}%%".format(data["mix_ethanol_pct"]))
    if data.get("yoy_sugar_pct") is not None:
        sign = "+" if data["yoy_sugar_pct"] >= 0 else ""
        print("  YoY azucar      : %s%.1f%%" % (sign, data["yoy_sugar_pct"]))
    print()
    print("  QUINZENAL — Centro-Sul")
    print("  " + "-" * 45)
    if data.get("cane_quinzenal_mt") is not None:
        print("  Cana molida     : {:>8.3f} Mt".format(data["cane_quinzenal_mt"]))
    if data.get("sugar_quinzenal_mt") is not None:
        print("  Azucar          : {:>8.3f} Mt".format(data["sugar_quinzenal_mt"]))
    print()
    print("  PROYECCION FULL-YEAR")
    print("  " + "-" * 45)
    prog = data.get("season_progress_pct", 0)
    proj = data.get("projected_full_year_mt")
    print("  Progreso safra  : %.1f%%" % prog)
    if proj:
        print("  Proyeccion CS   : {:>8.3f} Mt azucar".format(proj))
        print("  Fuente          : unica_quinzenal_Q%d_%s" % (
            data.get("quinzena_num", 0), data.get("safra", "?")
        ))
    else:
        print("  Proyeccion      : N/D (temporada demasiado temprana)")
    if data.get("idm_source"):
        print()
        print("  idM fuente      : %d" % data["idm_source"])
    print()


def main():
    parser = argparse.ArgumentParser(description="UNICA Quinzenal ingestion")
    parser.add_argument("--show",  action="store_true", help="Mostrar ultimo reporte")
    parser.add_argument("--idm",   type=int,            help="idM especifico a descargar")
    args = parser.parse_args()

    print("=" * 65)
    print("  SGT — UNICA Quinzenal Brasil Centro-Sul")
    print("=" * 65)

    if args.show and not args.idm:
        from ingestion.unica import scrape_latest_idm
        print("  Obteniendo idM actual...")
        args.idm = scrape_latest_idm()
        if not args.idm:
            print("  [ERROR] No se pudo obtener idM.")
            return

    cmd_fetch(idm=args.idm)


if __name__ == "__main__":
    main()
