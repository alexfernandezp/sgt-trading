import logging
from datetime import date, timedelta
import pandas as pd
import yfinance as yf
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from config import INSTRUMENTS
from models import PriceHistory

logger = logging.getLogger(__name__)


def _download_instrument(name, cfg, start, end):
    for ticker in [cfg["yf_ticker"], cfg.get("fallback")]:
        if not ticker:
            continue
        try:
            df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
            if not df.empty:
                return df
        except Exception as exc:
            logger.warning(f"{name} {ticker}: {exc}")
    return pd.DataFrame()


def fetch_prices(session: Session, instruments=None, days_back: int = 30) -> dict:
    end     = date.today()
    start   = end - timedelta(days=days_back)
    targets = instruments or list(INSTRUMENTS.keys())
    results = {}

    for name in targets:
        cfg = INSTRUMENTS.get(name)
        if not cfg:
            continue
        df = _download_instrument(name, cfg, start, end)
        if df.empty:
            results[name] = 0
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns=str.lower)
        df.index = pd.to_datetime(df.index).date
        rows = []
        for day, row in df.iterrows():
            if pd.isna(row.get("close")):
                continue
            rows.append({
                "date": day, "instrument": name,
                "open":   float(row["open"])   if not pd.isna(row.get("open",   float("nan"))) else None,
                "high":   float(row["high"])   if not pd.isna(row.get("high",   float("nan"))) else None,
                "low":    float(row["low"])    if not pd.isna(row.get("low",    float("nan"))) else None,
                "close":  float(row["close"]),
                "volume": int(row["volume"])   if not pd.isna(row.get("volume", float("nan"))) else None,
                "source": "yfinance",
            })
        if rows:
            stmt = insert(PriceHistory).values(rows)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_price_date_instrument",
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
        results[name] = len(rows)
        logger.info(f"{name}: {len(rows)} filas")
    return results
