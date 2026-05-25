"""Crea la tabla cepea_prices en DB y ejecuta el primer fetch."""
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal, engine
from models import Base
from models.market_data import CepeaPrice
from ingestion.cepea import fetch_cepea, get_latest_cepea

# Crear tabla si no existe
Base.metadata.create_all(engine, tables=[CepeaPrice.__table__])
print("Tabla cepea_prices: OK")

session = SessionLocal()
try:
    print("\nFetcheando CEPEA...")
    result = fetch_cepea(session)
    print("  Etanol rows :", result["ethanol_rows"])
    print("  Azucar rows :", result["sugar_rows"])
    if result["errors"]:
        for e in result["errors"]:
            print("  ERROR:", e)

    print("\nUltimos valores por serie:")
    latest = get_latest_cepea(session)
    for name, d in sorted(latest.items()):
        pr = d.get("price_usd")
        dt = d.get("price_date")
        unit = d.get("unit", "")
        pct_d = d.get("pct_daily")
        pct_w = d.get("pct_weekly")
        pct_m = d.get("pct_monthly")
        extra = ""
        if pct_d is not None:
            extra += "  1d=%+.2f%%" % pct_d
        if pct_w is not None:
            extra += "  1w=%+.2f%%" % pct_w
        if pct_m is not None:
            extra += "  1m=%+.2f%%" % pct_m
        print("  %-35s  %8.4f %-20s %s%s" % (name, pr or 0, unit, dt or "?", extra))

    # Calcula paridad si hay datos
    print("\nParidad etanol-azucar:")
    hy = latest.get("hydrous_paulinia_usd_m3", {}).get("price_usd")
    cry = latest.get("crystal_sugar_usd_bag50kg", {}).get("price_usd")
    if hy and cry:
        ethanol_ton = hy / 1.20
        crystal_ton = cry * 20
        ratio_phys  = ethanol_ton / crystal_ton
        print("  Etanol Paulinia: %.2f US$/m3 -> %.2f US$/ton equiv azucar" % (hy, ethanol_ton))
        print("  Azucar cristal : %.2f US$/bolsa -> %.2f US$/ton" % (cry, crystal_ton))
        print("  Ratio etanol/azucar fisico: %.4f" % ratio_phys)
        if ratio_phys >= 1.05:
            print("  -> Mills prefieren ETANOL (+%.1f%%) -> menos azucar -> LONG SB" % ((ratio_phys-1)*100))
        elif ratio_phys <= 0.95:
            print("  -> Mills prefieren AZUCAR (+%.1f%% ventaja) -> mas oferta -> SHORT SB" % ((1-ratio_phys)*100))
        else:
            print("  -> Zona neutral [0.95-1.05] — sin sesgo de mezcla definido")
    else:
        print("  Sin datos suficientes para calcular paridad")
finally:
    session.close()
