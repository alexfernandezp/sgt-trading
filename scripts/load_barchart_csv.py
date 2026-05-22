"""
Carga los CSVs de Barchart a la base de datos.
Ejecutar cada manana despues de subir los CSVs a OneDrive.

Uso:
  py scripts/load_barchart_csv.py

Estructura de carpetas esperada en OneDrive/.../data/:
  SB11/Futures Prices/          <- curva completa de ayer (OHLCV todos contratos)
  SB11/Historical Data/SBN26/   <- historico 6 meses SBN26
  Options Chain/SBN26/          <- cadena de opciones (OI por strike)
  Options Greeks/SBN26/         <- greeks por strike (IV, delta, gamma...)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal
from config import BARCHART_DATA_PATH
import ingestion.barchart_csv as bc

session = SessionLocal()
print("Cargando datos de Barchart desde:")
print("  %s\n" % BARCHART_DATA_PATH)

# 1. Curva de futuros (todos los contratos, OHLCV de ayer)
print("1. Futures Prices (curva completa)...", end=" ", flush=True)
r = bc.load_futures_prices(session, BARCHART_DATA_PATH)
if r:
    print("OK  [%s]" % ", ".join(sorted(r.keys())))
else:
    print("sin datos")

# 2. Historico diario SBN26 (y otros si existen)
print("2. Historical Data (OHLCV diario)...", end=" ", flush=True)
r = bc.load_historical_data(session, BARCHART_DATA_PATH)
if r:
    parts = ["%s:%d dias" % (k, v) for k, v in sorted(r.items())]
    print("OK  [%s]" % ", ".join(parts))
else:
    print("sin datos")

# 3. Cadena de opciones (OI, vol, precio por strike)
print("3. Options Chain (OI por strike)...", end=" ", flush=True)
r = bc.load_options_chain(session, BARCHART_DATA_PATH)
if r:
    parts = ["%s:%d strikes" % (k, v) for k, v in sorted(r.items())]
    print("OK  [%s]" % ", ".join(parts))
else:
    print("sin datos")

# 4. Greeks (IV, delta, gamma por strike)
print("4. Options Greeks (IV/delta/gamma)...", end=" ", flush=True)
r = bc.load_options_greeks(session, BARCHART_DATA_PATH)
if r:
    parts = ["%s:%d strikes" % (k, v) for k, v in sorted(r.items())]
    print("OK  [%s]" % ", ".join(parts))
else:
    print("sin datos")

session.close()
print("\nListo. BD actualizada con datos oficiales ICE.")
