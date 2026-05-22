"""
Ingesta de CSVs descargados de Barchart Premier.

Estructura esperada en BARCHART_DATA_PATH:
  SB11/
    Futures Prices/          <- sugar-11-prices-end-of-day-{date}.csv
    Historical Data/
      SBN26/                 <- sbn26_price-history-{date}.csv
      SBV26/                 <- (futuro)
  SW5/
    Futures Prices/          <- (futuro)
    Historical Data/         <- (futuro)
  Options Chain/
    SBN26/                   <- sbn26-options-...-stacked-intraday-{date}.csv
  Options Greeks/
    SBN26/                   <- sbn26-volatility-greeks-...-{date}.csv

Workflow: cargar el CSV mas reciente de cada carpeta.
"""
import csv
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from models import PriceHistory, OptionsData

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _latest_csv(folder: str) -> Path | None:
    """Devuelve el CSV mas reciente en una carpeta (por fecha en nombre o mtime)."""
    p = Path(folder)
    if not p.exists():
        return None
    csvs = sorted(p.glob("*.csv"), key=lambda f: f.stat().st_mtime, reverse=True)
    return csvs[0] if csvs else None


def _clean_int(val: str) -> int | None:
    if not val or val.strip() in ("", "N/A", "0"):
        return None
    try:
        return int(val.replace(",", "").strip())
    except ValueError:
        return None


def _clean_float(val: str) -> float | None:
    if not val or val.strip() in ("", "N/A"):
        return None
    try:
        return float(val.replace(",", "").strip())
    except ValueError:
        return None


def _clean_pct(val: str) -> float | None:
    """Convierte '27.30%' o '+2.50%' a 0.2730."""
    if not val or val.strip() in ("", "N/A", "0.00%"):
        return None
    try:
        return round(float(val.replace("%", "").replace("+", "").strip()) / 100, 6)
    except ValueError:
        return None


def _parse_contract_name(name: str) -> str | None:
    """
    'SBN26 (Jul \'26)' -> 'SBN26'
    'SBY00 (Cash)'     -> None  (cash, skip)
    """
    name = name.strip().strip('"')
    if "Cash" in name or "Y00" in name:
        return None
    m = re.match(r"^(SB[A-Z]\d{2}|SW[A-Z]\d{2})", name)
    return m.group(1) if m else None


def _parse_expiry_from_filename(filename: str) -> date | None:
    """
    'sbn26-options-monthly-options-exp-06_15_26-...' -> date(2026,6,15)
    """
    m = re.search(r"exp-(\d{2})_(\d{2})_(\d{2})", filename)
    if m:
        month, day, yr = int(m.group(1)), int(m.group(2)), 2000 + int(m.group(3))
        return date(yr, month, day)
    return None


def _parse_date_from_filename(filename: str) -> date | None:
    """
    'sugar-11-prices-end-of-day-05-20-2026.csv' -> date(2026,5,20)
    'sbn26_price-history-05-20-2026.csv'        -> date(2026,5,20)
    """
    m = re.search(r"(\d{2})-(\d{2})-(\d{4})", filename)
    if m:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    return None


def _upsert_price_history(session: Session, rows: list) -> int:
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
    return len(rows)


def _upsert_options_chain(session: Session, rows: list) -> int:
    """Upsert de cadena de opciones: actualiza precio, OI, volumen, premium, bid/ask."""
    if not rows:
        return 0
    stmt = insert(OptionsData).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_options_date_instr_expiry_strike_type",
        set_={
            "last_price":    stmt.excluded.last_price,
            "volume":        stmt.excluded.volume,
            "open_interest": stmt.excluded.open_interest,
            "premium":       stmt.excluded.premium,
            "bid":           stmt.excluded.bid,
            "ask":           stmt.excluded.ask,
        },
    )
    session.execute(stmt)
    session.commit()
    return len(rows)


def _upsert_options_greeks(session: Session, rows: list) -> int:
    """Upsert de greeks: solo actualiza IV/delta/gamma/theta/vega/iv_skew. No toca OI."""
    if not rows:
        return 0
    stmt = insert(OptionsData).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_options_date_instr_expiry_strike_type",
        set_={
            "last_price": stmt.excluded.last_price,
            "iv":         stmt.excluded.iv,
            "delta":      stmt.excluded.delta,
            "gamma":      stmt.excluded.gamma,
            "theta":      stmt.excluded.theta,
            "vega":       stmt.excluded.vega,
            "iv_skew":    stmt.excluded.iv_skew,
        },
    )
    session.execute(stmt)
    session.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Parser 1: SB11/Futures Prices  →  price_history (todos los contratos)
# ---------------------------------------------------------------------------

def load_futures_prices(session: Session, data_path: str) -> dict:
    """
    Lee el CSV mas reciente de SB11/Futures Prices y actualiza price_history
    con OHLCV oficial ICE para todos los contratos disponibles.
    Devuelve {instrument: 1} para cada contrato cargado.
    """
    folder = os.path.join(data_path, "SB11", "Futures Prices")
    csv_file = _latest_csv(folder)
    if not csv_file:
        logger.warning("load_futures_prices: no CSV en %s", folder)
        return {}

    rows = []
    with open(csv_file, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            contract = row.get("Contract", "")
            if "Downloaded" in contract:
                continue
            instr = _parse_contract_name(contract)
            if not instr:
                continue
            trade_date_str = row.get("Time", "").strip()
            try:
                trade_date = date.fromisoformat(trade_date_str)
            except ValueError:
                continue

            rows.append({
                "date":       trade_date,
                "instrument": instr,
                "open":       _clean_float(row.get("Open", "")),
                "high":       _clean_float(row.get("High", "")),
                "low":        _clean_float(row.get("Low", "")),
                "close":      _clean_float(row.get("Last", "")),
                "volume":     _clean_int(row.get("Volume", "")),
                "source":     "barchart",
            })

    n = _upsert_price_history(session, rows)
    logger.info("load_futures_prices: %d contratos desde %s", n, csv_file.name)
    return {r["instrument"]: 1 for r in rows}


# ---------------------------------------------------------------------------
# Parser 2: SB11/Historical Data/{instr}  →  price_history (historico diario)
# ---------------------------------------------------------------------------

def load_historical_data(session: Session, data_path: str,
                         instruments: list = None) -> dict:
    """
    Lee el CSV mas reciente de cada subcarpeta de SB11/Historical Data
    y carga el historico diario OHLCV en price_history.
    """
    base = os.path.join(data_path, "SB11", "Historical Data")
    targets = instruments or [d for d in os.listdir(base)
                              if os.path.isdir(os.path.join(base, d))] if os.path.exists(base) else []
    results = {}

    for instr in targets:
        folder = os.path.join(base, instr)
        csv_file = _latest_csv(folder)
        if not csv_file:
            continue

        rows = []
        with open(csv_file, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                date_str = row.get("Time", "").strip()
                if "Downloaded" in date_str or not date_str:
                    continue
                try:
                    trade_date = date.fromisoformat(date_str)
                except ValueError:
                    continue

                rows.append({
                    "date":       trade_date,
                    "instrument": instr,
                    "open":       _clean_float(row.get("Open", "")),
                    "high":       _clean_float(row.get("High", "")),
                    "low":        _clean_float(row.get("Low", "")),
                    "close":      _clean_float(row.get("Latest", "")),
                    "volume":     _clean_int(row.get("Volume", "")),
                    "source":     "barchart",
                })

        n = _upsert_price_history(session, rows)
        results[instr] = n
        logger.info("load_historical_data %s: %d dias desde %s", instr, n, csv_file.name)

    return results


# ---------------------------------------------------------------------------
# Parser 3: Options Chain  →  options_data (OI, vol, precio por strike)
# ---------------------------------------------------------------------------

def load_options_chain(session: Session, data_path: str,
                       instruments: list = None) -> dict:
    """
    Lee el CSV mas reciente de Options Chain/{instr} (formato stacked).
    Columnas: Strike(con C/P), Open, High, Low, Latest, Change, Bid, Ask,
              Volume, Open Int, Premium, Last Trade, Type
    """
    base = os.path.join(data_path, "Options Chain")
    targets = instruments or ([d for d in os.listdir(base)
                                if os.path.isdir(os.path.join(base, d))]
                               if os.path.exists(base) else [])
    results = {}

    for instr in targets:
        folder = os.path.join(base, instr)
        csv_file = _latest_csv(folder)
        if not csv_file:
            continue

        trade_date = _parse_date_from_filename(csv_file.name) or date.today()
        expiry     = _parse_expiry_from_filename(csv_file.name)

        rows = []
        with open(csv_file, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                strike_raw = row.get("Strike", "").strip()
                if "Downloaded" in strike_raw or not strike_raw:
                    continue
                opt_type = row.get("Type", "").strip().lower()
                if opt_type not in ("call", "put"):
                    continue
                # Strike puede venir como "15.00C" o "15.00" con Type separado
                strike_str = re.sub(r"[CP]$", "", strike_raw)
                strike = _clean_float(strike_str)
                if strike is None:
                    continue

                rows.append({
                    "trade_date":    trade_date,
                    "instrument":    instr,
                    "expiry":        expiry,
                    "strike":        strike,
                    "option_type":   opt_type,
                    "last_price":    _clean_float(row.get("Latest", "")),
                    "volume":        _clean_int(row.get("Volume", "")),
                    "open_interest": _clean_int(row.get("Open Int", "")),
                    "premium":       _clean_float(row.get("Premium", "")),
                    "bid":           _clean_float(row.get("Bid", "")),
                    "ask":           _clean_float(row.get("Ask", "")),
                    "source":        "barchart",
                })

        n = _upsert_options_chain(session, rows)
        results[instr] = n
        logger.info("load_options_chain %s: %d strikes desde %s", instr, n, csv_file.name)

    return results


# ---------------------------------------------------------------------------
# Parser 4: Options Greeks  →  options_data (actualiza greeks en filas existentes)
# ---------------------------------------------------------------------------

def load_options_greeks(session: Session, data_path: str,
                        instruments: list = None) -> dict:
    """
    Lee el CSV mas reciente de Options Greeks/{instr}.
    Columnas: Strike, Type, Latest, IV, Delta, Gamma, Theta, Vega, IV Skew, Last Trade
    Hace upsert actualizando los campos de greeks en options_data.
    """
    base = os.path.join(data_path, "Options Greeks")
    targets = instruments or ([d for d in os.listdir(base)
                                if os.path.isdir(os.path.join(base, d))]
                               if os.path.exists(base) else [])
    results = {}

    for instr in targets:
        folder = os.path.join(base, instr)
        csv_file = _latest_csv(folder)
        if not csv_file:
            continue

        trade_date = _parse_date_from_filename(csv_file.name) or date.today()
        expiry     = _parse_expiry_from_filename(csv_file.name)

        rows = []
        with open(csv_file, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                strike_raw = row.get("Strike", "").strip()
                if "Downloaded" in strike_raw or not strike_raw:
                    continue
                opt_type = row.get("Type", "").strip().lower()
                if opt_type not in ("call", "put"):
                    continue
                strike = _clean_float(strike_raw)
                if strike is None:
                    continue

                rows.append({
                    "trade_date":    trade_date,
                    "instrument":    instr,
                    "expiry":        expiry,
                    "strike":        strike,
                    "option_type":   opt_type,
                    "last_price":    _clean_float(row.get("Latest", "")),
                    "volume":        None,
                    "open_interest": None,
                    "premium":       None,
                    "bid":           None,
                    "ask":           None,
                    "iv":            _clean_pct(row.get("IV", "")),
                    "delta":         _clean_float(row.get("Delta", "")),
                    "gamma":         _clean_float(row.get("Gamma", "")),
                    "theta":         _clean_float(row.get("Theta", "")),
                    "vega":          _clean_float(row.get("Vega", "")),
                    "iv_skew":       _clean_pct(row.get("IV Skew", "")),
                    "source":        "barchart",
                })

        n = _upsert_options_greeks(session, rows)
        results[instr] = n
        logger.info("load_options_greeks %s: %d strikes desde %s", instr, n, csv_file.name)

    return results
