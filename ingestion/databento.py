"""
Ingestion de datos historicos desde Databento (dataset IFUS.IMPACT).
Cubre Sugar No.11 (SB) con barras OHLCV de alta calidad desde ICE directamente.

Schemas soportados:
  ohlcv-1d  -> price_history  (reemplaza datos Yahoo diarios)
  ohlcv-1h  -> price_bars     (interval='1h', agrupable a 4h)
  ohlcv-1m  -> price_bars     (interval='1m', agrupable a 5m/30m/1h/4h)

Uso tipico:
  from ingestion.databento import download_ohlcv, ingest_daily_from_file, ingest_intraday_from_file
"""
import logging
from datetime import timezone
from pathlib import Path

import databento as db
import pandas as pd
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from models import PriceHistory, PriceBars

logger = logging.getLogger(__name__)

DATASET   = "IFUS.IMPACT"
SB_SYMBOL = "SB.c.0"          # contrato continuo Sugar No.11 (calendar roll)
SB_INSTR  = "SB_CONT"         # nombre interno en nuestra BD

RAW_DIR   = Path(__file__).parent.parent / "data" / "databento_raw"

SCHEMAS_INTRADAY = {"ohlcv-1m": "1m", "ohlcv-1h": "1h"}


# ---------------------------------------------------------------------------
# Estimacion de costes (sin cargo)
# ---------------------------------------------------------------------------

def estimate_costs(api_key: str, start: str, end: str) -> dict[str, float | None]:
    """Devuelve coste estimado por schema sin realizar ningun cargo."""
    client = db.Historical(api_key)
    results = {}
    for schema in ["ohlcv-1d", "ohlcv-1h", "ohlcv-1m"]:
        try:
            results[schema] = client.metadata.get_cost(
                dataset=DATASET,
                symbols=[SB_SYMBOL],
                stype_in="continuous",
                schema=schema,
                start=start,
                end=end,
            )
        except Exception as exc:
            results[schema] = None
            logger.warning("Fallo estimacion coste %s: %s", schema, exc)
    return results


# ---------------------------------------------------------------------------
# Descarga a fichero local
# ---------------------------------------------------------------------------

def download_ohlcv(
    api_key: str,
    schema: str,
    start: str,
    end: str,
    overwrite: bool = False,
) -> Path:
    """
    Descarga datos OHLCV al directorio data/databento_raw/.
    Si el fichero ya existe lo reutiliza (re-download dentro de 30 dias es gratis).
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    tag  = schema.replace("-", "_")
    yr0  = start[:4]
    yr1  = end[:4]
    path = RAW_DIR / f"sb_cont_{tag}_{yr0}_{yr1}.dbn.zst"

    if path.exists() and not overwrite:
        logger.info("Fichero ya existe, reutilizando: %s", path.name)
        return path

    client = db.Historical(api_key)
    logger.info("Descargando %s (%s -> %s)...", schema, start, end)

    client.timeseries.get_range(
        dataset=DATASET,
        symbols=[SB_SYMBOL],
        stype_in="continuous",
        schema=schema,
        start=start,
        end=end,
        path=str(path),
    )

    size_mb = path.stat().st_size / 1_048_576
    logger.info("Guardado en %s  (%.1f MB)", path.name, size_mb)
    return path


# ---------------------------------------------------------------------------
# Normalizacion de precios
# ---------------------------------------------------------------------------

def _clean_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Limpia el DataFrame de Databento:
    - Mantiene solo publisher_id=97 (feed principal ICE Futures US)
    - Descarta filas con close=0 (datos incompletos del feed secundario)
    - Sanity check de escala de precios (Sugar: 5-50 c/lb)
    """
    # Publisher 97 = feed principal IFUS; 98 = feed secundario con datos rotos
    if "publisher_id" in df.columns:
        df = df[df["publisher_id"] == 97].copy()

    # Excluir filas con precio cero (datos invalidos)
    df = df[df["close"] > 0].copy()

    # Sanity check escala
    sample = df["close"].dropna().median()
    if sample > 1_000:
        logger.warning(
            "Precios parecen sin escalar (mediana=%.0f), aplicando factor 1e-9", sample
        )
        for col in ("open", "high", "low", "close"):
            df[col] = df[col] / 1e9

    return df


# ---------------------------------------------------------------------------
# Ingestion en BD: price_history (diario)
# ---------------------------------------------------------------------------

def ingest_daily_from_file(session: Session, path: Path) -> int:
    """Lee un fichero ohlcv-1d e inserta/actualiza price_history."""
    store = db.DBNStore.from_file(str(path))
    df    = store.to_df()
    df    = _clean_df(df)

    rows = []
    for ts, row in df.iterrows():
        # ts_event es el indice (pandas Timestamp UTC)
        trade_date = ts.date() if hasattr(ts, "date") else pd.Timestamp(ts).date()

        rows.append({
            "date":       trade_date,
            "instrument": SB_INSTR,
            "open":       float(row["open"]),
            "high":       float(row["high"]),
            "low":        float(row["low"]),
            "close":      float(row["close"]),
            "volume":     int(row["volume"]) if pd.notna(row.get("volume")) else None,
            "source":     "databento",
        })

    if not rows:
        return 0

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
    logger.info("price_history: %d filas insertadas/actualizadas", len(rows))
    return len(rows)


# ---------------------------------------------------------------------------
# Ingestion en BD: price_bars (intraday)
# ---------------------------------------------------------------------------

def ingest_intraday_from_file(session: Session, path: Path, interval: str) -> int:
    """
    Lee un fichero ohlcv-1m u ohlcv-1h e inserta en price_bars.
    Descarta barras con volumen = 0 (horas sin actividad).
    Inserta en batches de 5.000 para controlar memoria.
    """
    store = db.DBNStore.from_file(str(path))
    df    = store.to_df()
    df    = _clean_df(df)   # ya descarta publisher 98 y close=0

    # Filtrar barras sin volumen (out-of-hours gaps), ademas de las ya filtradas
    vol_col = "volume" if "volume" in df.columns else None
    if vol_col:
        n_before = len(df)
        df = df[df[vol_col] > 0].copy()
        logger.info("Barras con volumen: %d / %d (%.0f%%)",
                    len(df), n_before, 100 * len(df) / max(n_before, 1))

    rows = []
    for ts, row in df.iterrows():
        dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else pd.Timestamp(ts).to_pydatetime()
        # Asegurar timezone-aware (UTC) para PostgreSQL
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        rows.append({
            "datetime":   dt,
            "instrument": SB_INSTR,
            "interval":   interval,
            "open":       float(row["open"]),
            "high":       float(row["high"]),
            "low":        float(row["low"]),
            "close":      float(row["close"]),
            "volume":     int(row[vol_col]) if vol_col and pd.notna(row[vol_col]) else None,
        })

    total = 0
    BATCH = 5_000
    for i in range(0, len(rows), BATCH):
        batch = rows[i : i + BATCH]
        stmt  = insert(PriceBars).values(batch)
        stmt  = stmt.on_conflict_do_update(
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
        if total % 50_000 == 0:
            logger.info("  ... %d filas insertadas", total)

    logger.info("price_bars [%s]: %d filas insertadas/actualizadas", interval, total)
    return total
