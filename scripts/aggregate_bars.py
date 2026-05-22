"""
Agrega barras de 1m (Databento) a intervalos 5m, 30m y 4h.
Operacion local sin coste de API. Genera ~7.4 anos de historia intraday.

Uso:
  py scripts/aggregate_bars.py               # agrega los tres intervalos
  py scripts/aggregate_bars.py --interval 5m # solo un intervalo
  py scripts/aggregate_bars.py --dry-run      # muestra estadisticas sin escribir en BD
"""
import sys, os, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd
from sqlalchemy.dialects.postgresql import insert
from database import SessionLocal
from sqlalchemy import text
from models import PriceBars

INSTRUMENT = "SB_CONT"
SOURCE_INTERVAL = "1m"

# Mapeo pandas offset -> nombre de intervalo en BD
TARGETS = {
    "5m":  "5min",   # pandas alias
    "30m": "30min",
    "4h":  "4h",
}


def load_1m_bars(session) -> pd.DataFrame:
    """Carga todas las barras 1m de SB_CONT en un DataFrame indexado por datetime UTC."""
    rows = session.execute(text("""
        SELECT datetime, open, high, low, close, volume
        FROM price_bars
        WHERE instrument = :instr AND interval = :iv
        ORDER BY datetime ASC
    """), {"instr": INSTRUMENT, "iv": SOURCE_INTERVAL}).fetchall()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "volume"])
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    df = df.set_index("datetime")
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    df["volume"] = df["volume"].fillna(0).astype(int)
    return df


def aggregate(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    """
    Agrega un DataFrame de barras OHLCV a una frecuencia mayor.
    Descarta bins sin actividad (volume=0 en todas las barras del bin).
    """
    agg = df.resample(freq, closed="left", label="left").agg(
        open=("open",   "first"),
        high=("high",   "max"),
        low= ("low",    "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    # Descartar periodos sin ninguna barra real (open=NaN o volume=0)
    agg = agg.dropna(subset=["open"])
    agg = agg[agg["volume"] > 0]
    return agg


def upsert_bars(session, df: pd.DataFrame, interval: str) -> int:
    """Inserta/actualiza barras en price_bars en batches de 5.000."""
    rows = []
    for ts, row in df.iterrows():
        rows.append({
            "datetime":   ts.to_pydatetime(),
            "instrument": INSTRUMENT,
            "interval":   interval,
            "open":       float(row["open"]),
            "high":       float(row["high"]),
            "low":        float(row["low"]),
            "close":      float(row["close"]),
            "volume":     int(row["volume"]),
        })

    BATCH = 5_000
    total = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i : i + BATCH]
        stmt = insert(PriceBars).values(batch)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_bars_instr_interval_dt",
            set_={
                "open":   stmt.excluded.open,
                "high":   stmt.excluded.high,
                "low":    stmt.excluded.low,
                "close":  stmt.excluded.close,
                "volume": stmt.excluded.volume,
            },
        )
        session.execute(stmt)
        session.commit()
        total += len(batch)
    return total


def show_sample(df: pd.DataFrame, interval: str, n: int = 5):
    print("  Ultimas %d barras [%s]:" % (n, interval))
    for ts, row in df.tail(n).iterrows():
        print("    %s  O=%.4f H=%.4f L=%.4f C=%.4f  Vol=%d" % (
            str(ts)[:19], row["open"], row["high"], row["low"], row["close"], int(row["volume"])))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", choices=["5m", "30m", "4h"],
                        help="Agregar solo este intervalo")
    parser.add_argument("--dry-run", action="store_true",
                        help="Mostrar estadisticas sin escribir en BD")
    args = parser.parse_args()

    targets = {args.interval: TARGETS[args.interval]} if args.interval else TARGETS

    session = SessionLocal()

    print("Cargando barras 1m de SB_CONT...", end=" ", flush=True)
    df1m = load_1m_bars(session)
    if df1m.empty:
        print("ERROR: no hay barras 1m en BD. Ejecuta primero databento_download.py")
        session.close()
        return

    print("OK  [%d barras  %s -> %s]" % (
        len(df1m),
        str(df1m.index[0])[:19],
        str(df1m.index[-1])[:19],
    ))
    print()

    for interval, freq in targets.items():
        print("Agregando 1m -> %s (freq=%s)..." % (interval, freq), end=" ", flush=True)
        agg = aggregate(df1m, freq)
        print("OK  [%d barras generadas]" % len(agg))

        show_sample(agg, interval)
        print()

        if not args.dry_run:
            print("  Insertando en BD...", end=" ", flush=True)
            n = upsert_bars(session, agg, interval)
            print("OK  [%d filas upsert]" % n)
            print()

    if args.dry_run:
        print("[dry-run] No se escribio nada en BD.")
    else:
        # Resumen final
        print("=== RESUMEN FINAL ===")
        result = session.execute(text("""
            SELECT interval, COUNT(*) n, MIN(datetime) desde, MAX(datetime) hasta
            FROM price_bars
            WHERE instrument = :instr
              AND interval IN ('1m','5m','30m','1h','4h')
            GROUP BY interval
            ORDER BY
              CASE interval
                WHEN '1m'  THEN 1
                WHEN '5m'  THEN 2
                WHEN '30m' THEN 3
                WHEN '1h'  THEN 4
                WHEN '4h'  THEN 5
              END
        """), {"instr": INSTRUMENT}).fetchall()

        print("  %-6s  %8s  %s  ->  %s" % ("INTV", "BARRAS", "DESDE".ljust(19), "HASTA"))
        print("  " + "-" * 65)
        for row in result:
            print("  %-6s  %8d  %s  ->  %s" % (
                row[0], row[1], str(row[2])[:19], str(row[3])[:19]))

    session.close()


if __name__ == "__main__":
    main()
