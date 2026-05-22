import logging
from datetime import date, datetime, timedelta
import pandas as pd
import yfinance as yf
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from models import PriceBars

logger = logging.getLogger(__name__)

INTRADAY_INSTRUMENTS = {
    "SB_CONT": "SB=F",
    "SBN26":   "SBN26.NYB",
    "SBV26":   "SBV26.NYB",
}

INTERVAL_CONFIG = {
    "5m":  ("5m",  59),
    "30m": ("30m", 59),
    "1h":  ("1h",  729),
    "1wk": ("1wk", 3650),
    "1mo": ("1mo", 3650),
}


def _download_bars(ticker, yf_interval, days_back):
    end   = datetime.utcnow()
    start = end - timedelta(days=days_back)
    try:
        df = yf.download(ticker, start=start, end=end, interval=yf_interval,
                         progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        return df
    except Exception as exc:
        logger.warning(f"{ticker} {yf_interval}: {exc}")
        return pd.DataFrame()


def _resample_4h(df_1h):
    if df_1h.empty:
        return df_1h
    return df_1h.resample("4h").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"),     close=("close", "last"),
        volume=("volume", "sum"),
    ).dropna(subset=["close"])


def _df_to_rows(df, instrument, interval):
    rows = []
    for dt, row in df.iterrows():
        if pd.isna(row.get("close")):
            continue
        ts = pd.Timestamp(dt)
        if ts.tzinfo is not None:
            ts = ts.tz_convert("UTC").tz_localize(None)
        rows.append({
            "datetime": ts.to_pydatetime(), "instrument": instrument, "interval": interval,
            "open":   float(row["open"])   if not pd.isna(row.get("open",   float("nan"))) else None,
            "high":   float(row["high"])   if not pd.isna(row.get("high",   float("nan"))) else None,
            "low":    float(row["low"])    if not pd.isna(row.get("low",    float("nan"))) else None,
            "close":  float(row["close"]),
            "volume": int(row["volume"])   if not pd.isna(row.get("volume", float("nan"))) else None,
        })
    return rows


def _upsert(session, rows):
    if not rows:
        return 0
    stmt = insert(PriceBars).values(rows)
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
    return len(rows)


def fetch_intraday(session: Session, instruments=None, intervals=None) -> dict:
    targets    = instruments or list(INTRADAY_INSTRUMENTS.keys())
    intervals_ = intervals   or list(INTERVAL_CONFIG.keys()) + ["4h"]
    results    = {}

    for name in targets:
        ticker = INTRADAY_INSTRUMENTS.get(name)
        if not ticker:
            continue
        results[name] = {}
        df_1h = pd.DataFrame()
        need_1h = "1h" in intervals_ or "4h" in intervals_

        if need_1h:
            yf_iv, days = INTERVAL_CONFIG["1h"]
            df_1h = _download_bars(ticker, yf_iv, days)
            if not df_1h.empty and "1h" in intervals_:
                n = _upsert(session, _df_to_rows(df_1h, name, "1h"))
                results[name]["1h"] = n
                logger.info(f"{name} 1h: {n} filas")

        if "4h" in intervals_ and not df_1h.empty:
            df_4h = _resample_4h(df_1h)
            n = _upsert(session, _df_to_rows(df_4h, name, "4h"))
            results[name]["4h"] = n
            logger.info(f"{name} 4h: {n} filas (resample)")

        for iv in intervals_:
            if iv in ("1h", "4h") or iv not in INTERVAL_CONFIG:
                continue
            yf_iv, days = INTERVAL_CONFIG[iv]
            df = _download_bars(ticker, yf_iv, days)
            if df.empty:
                results[name][iv] = 0
                continue
            n = _upsert(session, _df_to_rows(df, name, iv))
            results[name][iv] = n
            logger.info(f"{name} {iv}: {n} filas")

    return results


def calc_session_vwap(session: Session, instrument: str, session_date: date = None) -> float | None:
    from sqlalchemy import text
    target_date = session_date or date.today()
    rows = session.execute(text("""
        SELECT high, low, close, volume FROM price_bars
        WHERE instrument = :instr AND interval = '30m'
          AND DATE(datetime) = :dt AND volume > 0
        ORDER BY datetime
    """), {"instr": instrument, "dt": target_date}).fetchall()
    if not rows:
        return None
    cum_tpv = cum_vol = 0.0
    for high, low, close, vol in rows:
        typical  = (float(high) + float(low) + float(close)) / 3
        cum_tpv += typical * float(vol)
        cum_vol += float(vol)
    return round(cum_tpv / cum_vol, 4) if cum_vol > 0 else None
