"""
Descarga e ingesta White Sugar No.5 London (SF.c.0) desde IFEU.IMPACT.
Barras diarias ohlcv-1d -> price_history con instrument='WS_CONT'.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import databento as db
import pandas as pd
from pathlib import Path
from datetime import timezone
from sqlalchemy.dialects.postgresql import insert
from database import SessionLocal
from models import PriceHistory
from config import DATABENTO_API_KEY

DATASET  = "IFEU.IMPACT"
SYMBOL   = "W.c.0"
STYPE    = "continuous"
START    = "2019-01-01"
END      = "2026-05-19"
INSTR    = "WS_CONT"
RAW_DIR  = Path("data/databento_raw")
RAW_PATH = RAW_DIR / "ws_cont_ohlcv_1d_2019_2026.dbn.zst"


def main():
    if not DATABENTO_API_KEY:
        print("ERROR: DATABENTO_API_KEY no definida")
        sys.exit(1)

    client = db.Historical(DATABENTO_API_KEY)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    # Estimar coste
    print("Estimando coste...", end=" ", flush=True)
    try:
        cost = client.metadata.get_cost(
            dataset=DATASET, symbols=[SYMBOL], stype_in=STYPE,
            schema="ohlcv-1d", start=START, end=END,
        )
        print("$%.4f" % cost)
    except Exception as e:
        print("ERROR:", e)
        sys.exit(1)

    # Descargar
    if RAW_PATH.exists():
        print("Fichero ya existe, reutilizando:", RAW_PATH.name)
    else:
        print("Descargando %s -> %s..." % (START, END), end=" ", flush=True)
        client.timeseries.get_range(
            dataset=DATASET, symbols=[SYMBOL], stype_in=STYPE,
            schema="ohlcv-1d", start=START, end=END,
            path=str(RAW_PATH),
        )
        print("OK  [%.1f KB]" % (RAW_PATH.stat().st_size / 1024))

    # Leer y limpiar
    store = db.DBNStore.from_file(str(RAW_PATH))
    df    = store.to_df()

    print("Filas raw:", len(df))
    if "publisher_id" in df.columns:
        print("Publishers:", df["publisher_id"].value_counts().to_dict())

    # Filtrar publisher principal y precios validos
    if "publisher_id" in df.columns:
        pub_counts = df["publisher_id"].value_counts()
        main_pub   = pub_counts.index[0]
        df = df[df["publisher_id"] == main_pub].copy()
    df = df[df["close"] > 0].copy()

    # Escalar si es necesario (Databento a veces entrega en fixed-point)
    median_close = df["close"].median()
    if median_close > 10_000:
        for col in ("open", "high", "low", "close"):
            df[col] = df[col] / 1e9
        print("Precios escalados (factor 1e-9), mediana post-escala: %.2f" % df["close"].median())
    else:
        print("Mediana close: %.2f" % median_close)

    # Ingestar en price_history
    rows = []
    for ts, row in df.iterrows():
        trade_date = pd.Timestamp(ts).date()
        rows.append({
            "date":       trade_date,
            "instrument": INSTR,
            "open":       float(row["open"]),
            "high":       float(row["high"]),
            "low":        float(row["low"]),
            "close":      float(row["close"]),
            "volume":     int(row["volume"]) if pd.notna(row.get("volume")) else None,
            "source":     "databento",
        })

    if not rows:
        print("ERROR: no hay filas para ingestar")
        sys.exit(1)

    session = SessionLocal()
    stmt = insert(PriceHistory).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_price_date_instrument",
        set_={
            "open":   stmt.excluded.open,
            "high":   stmt.excluded.high,
            "low":    stmt.excluded.low,
            "close":  stmt.excluded.close,
            "volume": stmt.excluded.volume,
            "source": stmt.excluded.source,
        },
    )
    session.execute(stmt)
    session.commit()
    session.close()

    print("Ingresadas %d barras diarias para WS_CONT (White Sugar No.5)" % len(rows))
    print("Rango: %s -> %s" % (rows[0]["date"], rows[-1]["date"]))


if __name__ == "__main__":
    main()
