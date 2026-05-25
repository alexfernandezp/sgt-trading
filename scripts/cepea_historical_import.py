"""
Importa la serie histórica completa CEPEA desde los XLS descargados.
Ejecutar una sola vez para cargar el histórico; después el scraper diario
mantiene la DB actualizada.

Series:
  ETH id=111  hydrous_paulinia_usd_m3      2010-2026  DIARIO
  ETH id=103  hydrous_fuel_usd_liter       2002-2026  SEMANAL
  ETH id=104  anhydrous_usd_liter          2002-2026  SEMANAL
  ETH id= 85  hydrous_other_usd_liter      2002-2026  SEMANAL
  SUG id= 53  crystal_sugar_usd_bag50kg    2003-2026  DIARIO
"""
import sys, os, logging
from datetime import datetime
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("cepea_import")

import xlrd
from sqlalchemy.dialects.postgresql import insert
from database import SessionLocal, engine
from models import Base
from models.market_data import CepeaPrice

# Asegurar que la tabla existe
Base.metadata.create_all(engine, tables=[CepeaPrice.__table__])

# Mapeo: (path, series_name, source_page, unit)
XLS_FILES = [
    ("data/cepea_eth_111.xls", "hydrous_paulinia_usd_m3",    "ethanol", "US$/m3"),
    ("data/cepea_eth_103.xls", "hydrous_fuel_usd_liter",     "ethanol", "US$/liter"),
    ("data/cepea_eth_104.xls", "anhydrous_usd_liter",        "ethanol", "Anhydrous US$/liter"),
    ("data/cepea_eth_85.xls",  "hydrous_other_usd_liter",    "ethanol", "US$/liter"),
    ("data/cepea_sug_53.xls",  "crystal_sugar_usd_bag50kg",  "sugar",   "US$"),
]


def _parse_date(val):
    """Parsea celdas de fecha: string MM/DD/YYYY o número Excel."""
    if isinstance(val, float):
        try:
            return datetime(*xlrd.xldate_as_tuple(val, 0)[:3]).date()
        except Exception:
            return None
    if isinstance(val, str):
        val = val.strip()
        for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(val, fmt).date()
            except ValueError:
                continue
    return None


def _parse_price(val):
    if isinstance(val, (int, float)):
        return float(val) if val > 0 else None
    if isinstance(val, str):
        try:
            return float(val.strip().replace(",", ""))
        except ValueError:
            return None
    return None


def load_xls(path, series_name, source_page, unit, session) -> int:
    if not os.path.exists(path):
        logger.warning("Archivo no encontrado: %s", path)
        return 0

    wb = xlrd.open_workbook(path, ignore_workbook_corruption=True)
    ws = wb.sheet_by_index(0)

    # Encontrar fila de inicio (primera fila con fecha válida)
    start_row = 4  # por defecto
    for i in range(ws.nrows):
        d = _parse_date(ws.cell_value(i, 0))
        if d is not None:
            start_row = i
            break

    inserted = 0
    skipped  = 0
    for row in range(start_row, ws.nrows):
        price_date = _parse_date(ws.cell_value(row, 0))
        price_usd  = _parse_price(ws.cell_value(row, 1))
        if price_date is None or price_usd is None:
            skipped += 1
            continue

        stmt = (
            insert(CepeaPrice)
            .values(
                price_date  = price_date,
                series_name = series_name,
                price_usd   = price_usd,
                unit        = unit,
                source_page = source_page,
            )
            .on_conflict_do_update(
                constraint = "uq_cepea_date_series",
                set_       = {"price_usd": price_usd, "unit": unit},
            )
        )
        session.execute(stmt)
        inserted += 1

    session.commit()
    logger.info("  %s: %d filas importadas (%d saltadas)", series_name, inserted, skipped)
    return inserted


def run():
    session = SessionLocal()
    total = 0
    try:
        for path, series_name, source_page, unit in XLS_FILES:
            logger.info("Cargando %s ...", series_name)
            total += load_xls(path, series_name, source_page, unit, session)
    finally:
        session.close()
    logger.info("Total filas importadas: %d", total)


if __name__ == "__main__":
    run()
