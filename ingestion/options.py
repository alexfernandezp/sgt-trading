"""
Parser de opciones ICE Sugar No. 11 — formato Barchart side-by-side.
Soporta dos archivos por vencimiento:
  - options chain:  *-options-*-side-by-side-*.csv
  - greeks:         *-volatility-greeks-*.csv

Coloca los CSVs en data/opciones/ antes de ejecutar el scoring.
"""
import logging
import os
import re
from pathlib import Path

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data" / "opciones"


def _clean_num(val) -> float | None:
    """Limpia '3,508', '3.13s', 'N/A' → float o None."""
    if val is None:
        return None
    s = str(val).strip().rstrip("s").replace(",", "").replace("%", "")
    if s in ("N/A", "n/a", "", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_chain(path: Path) -> pd.DataFrame | None:
    """
    Parsea el CSV de options chain side-by-side de Barchart.
    Devuelve DataFrame con columnas:
      strike, call_price, call_volume, call_oi, put_price, put_volume, put_oi
    """
    try:
        df_raw = pd.read_csv(path, header=0)
    except Exception as exc:
        logger.error(f"Error leyendo chain {path.name}: {exc}")
        return None

    # Eliminar footer (última fila si contiene "Downloaded")
    df_raw = df_raw[~df_raw.iloc[:, 0].astype(str).str.contains("Downloaded", na=False)]
    df_raw = df_raw[~df_raw.iloc[:, 0].astype(str).str.contains("Type", na=False)]  # sub-headers

    # Columnas por posición (format: Call cols 0-4, Strike 5, Put cols 6-10)
    # Type(0) Latest(1) Volume(2) OpenInt(3) Premium(4) Strike(5) Type(6) Latest(7) Volume(8) OpenInt(9) Premium(10)
    records = []
    for _, row in df_raw.iterrows():
        vals = list(row)
        if len(vals) < 10:
            continue
        strike = _clean_num(vals[5])
        if strike is None:
            continue
        records.append({
            "strike":      strike,
            "call_price":  _clean_num(vals[1]),
            "call_volume": _clean_num(vals[2]),
            "call_oi":     _clean_num(vals[3]),
            "put_price":   _clean_num(vals[7]),
            "put_volume":  _clean_num(vals[8]),
            "put_oi":      _clean_num(vals[9]),
        })

    if not records:
        return None
    df = pd.DataFrame(records)
    df["call_oi"]     = df["call_oi"].fillna(0)
    df["put_oi"]      = df["put_oi"].fillna(0)
    df["call_volume"] = df["call_volume"].fillna(0)
    df["put_volume"]  = df["put_volume"].fillna(0)
    return df


def _parse_greeks(path: Path) -> pd.DataFrame | None:
    """
    Parsea el CSV de greeks side-by-side de Barchart.
    Devuelve DataFrame con columnas:
      strike, call_iv, call_delta, put_iv, put_delta, call_iv_skew, put_iv_skew
    """
    try:
        df_raw = pd.read_csv(path, header=0)
    except Exception as exc:
        logger.error(f"Error leyendo greeks {path.name}: {exc}")
        return None

    df_raw = df_raw[~df_raw.iloc[:, 0].astype(str).str.contains("Downloaded", na=False)]

    # Latest(0) IV(1) Delta(2) Gamma(3) Theta(4) Vega(5) IVSkew(6) Type(7) LastTrade(8)
    # Strike(9) Latest(10) IV(11) Delta(12) Gamma(13) Theta(14) Vega(15) IVSkew(16) Type(17) LastTrade(18)
    records = []
    for _, row in df_raw.iterrows():
        vals = list(row)
        if len(vals) < 17:
            continue
        strike = _clean_num(vals[9])
        if strike is None:
            continue
        records.append({
            "strike":        strike,
            "call_iv":       _clean_num(str(vals[1]).replace("%", "")),
            "call_delta":    _clean_num(vals[2]),
            "call_gamma":    _clean_num(vals[3]),
            "call_theta":    _clean_num(vals[4]),
            "call_vega":     _clean_num(vals[5]),
            "call_iv_skew":  _clean_num(str(vals[6]).replace("%", "").replace("+", "")),
            "put_iv":        _clean_num(str(vals[11]).replace("%", "")),
            "put_delta":     _clean_num(vals[12]),
            "put_gamma":     _clean_num(vals[13]),
            "put_theta":     _clean_num(vals[14]),
            "put_vega":      _clean_num(vals[15]),
            "put_iv_skew":   _clean_num(str(vals[16]).replace("%", "").replace("+", "")),
        })

    return pd.DataFrame(records) if records else None


def _calc_max_pain(df: pd.DataFrame) -> float | None:
    """
    Calcula el max pain real: strike donde el valor total de opciones
    que expiran worthless es máximo (mínimo para los compradores).
    """
    strikes = sorted(df["strike"].unique())
    min_pain = None
    max_pain_strike = None

    for s in strikes:
        pain = 0.0
        for _, row in df.iterrows():
            k = row["strike"]
            # Calls: dolor si S > K → (S-K) × call_OI
            if s > k:
                pain += (s - k) * row["call_oi"] * 112_000 / 100
            # Puts: dolor si S < K → (K-S) × put_OI
            if s < k:
                pain += (k - s) * row["put_oi"] * 112_000 / 100

        if min_pain is None or pain < min_pain:
            min_pain = pain
            max_pain_strike = s

    return max_pain_strike


def get_latest_files(contract: str = "SBN26") -> dict[str, Path | None]:
    """Devuelve el chain y greeks más reciente para el contrato dado."""
    contract_lower = contract.lower()
    result = {"chain": None, "greeks": None}

    if not DATA_DIR.exists():
        return result

    for csv in sorted(DATA_DIR.glob("*.csv"), key=os.path.getmtime, reverse=True):
        name = csv.name.lower()
        if contract_lower not in name:
            continue
        if "greek" in name or "volatility" in name:
            if result["greeks"] is None:
                result["greeks"] = csv
        elif "option" in name or "chain" in name or "side-by-side" in name:
            if result["chain"] is None:
                result["chain"] = csv
        if result["chain"] and result["greeks"]:
            break

    return result


def parse_options(contract: str = "SBN26", current_price: float | None = None) -> dict | None:
    """
    Parsea chain + greeks del contrato y devuelve métricas clave.
    Si no hay archivos en data/opciones/ devuelve None.
    """
    files = get_latest_files(contract)
    if not files["chain"] and not files["greeks"]:
        logger.info(f"No hay CSVs de opciones para {contract} en data/opciones/")
        return None

    result = {"contract": contract, "chain_file": None, "greeks_file": None}

    # --- Chain ---
    if files["chain"]:
        df_chain = _parse_chain(files["chain"])
        result["chain_file"] = files["chain"].name
        if df_chain is not None:
            total_call_oi  = int(df_chain["call_oi"].sum())
            total_put_oi   = int(df_chain["put_oi"].sum())
            total_call_vol = int(df_chain["call_volume"].sum())
            total_put_vol  = int(df_chain["put_volume"].sum())

            pc_oi  = round(total_put_oi  / total_call_oi,  3) if total_call_oi  > 0 else None
            pc_vol = round(total_put_vol / total_call_vol, 3) if total_call_vol > 0 else None

            result.update({
                "total_call_oi":      total_call_oi,
                "total_put_oi":       total_put_oi,
                "total_call_vol":     total_call_vol,
                "total_put_vol":      total_put_vol,
                "put_call_ratio_oi":  pc_oi,
                "put_call_ratio_vol": pc_vol,
                "max_pain":           _calc_max_pain(df_chain),
            })

    # --- Greeks ---
    if files["greeks"]:
        df_greeks = _parse_greeks(files["greeks"])
        result["greeks_file"] = files["greeks"].name
        if df_greeks is not None and current_price is not None:
            # ATM: strike más cercano al precio actual
            df_greeks["dist"] = (df_greeks["strike"] - current_price).abs()
            atm_row = df_greeks.loc[df_greeks["dist"].idxmin()]

            # IV skew OTM puts — excluir stale (IV=0 o skew=-30.54 que es marcador ICE)
            otm_mask = (
                (df_greeks["strike"] > current_price + 0.50) &
                (df_greeks["put_iv"].notna()) &
                (df_greeks["put_iv"] > 0) &
                (df_greeks["put_iv_skew"].notna()) &
                (df_greeks["put_iv_skew"] != -30.54)
            )
            otm_puts = df_greeks[otm_mask]["put_iv_skew"]
            atm_iv_skew = float(atm_row["call_iv_skew"]) if atm_row["call_iv_skew"] else None
            avg_otm_put_skew = round(float(otm_puts.mean()), 2) if not otm_puts.empty else None

            result.update({
                "atm_strike":        float(atm_row["strike"]),
                "atm_call_iv":       float(atm_row["call_iv"]) if atm_row["call_iv"] else None,
                "atm_put_iv":        float(atm_row["put_iv"])  if atm_row["put_iv"]  else None,
                "atm_call_delta":    float(atm_row["call_delta"]) if atm_row["call_delta"] else None,
                "atm_iv_skew":       atm_iv_skew,
                "avg_otm_put_skew":  avg_otm_put_skew,
            })
        elif df_greeks is not None:
            result["greeks_rows"] = len(df_greeks)

    return result


def score_options(direction: str, contract: str = "SBN26",
                  current_price: float | None = None) -> tuple[int | None, dict]:
    """
    Calcula C3 a partir del CSV más reciente.

    Lógica:
      P/C ratio OI > 1.2 + max_pain < current_price → mercado posicionado bajista
        LONG:  puede haber squeeze → 1
        SHORT: alineado → 1

      OTM put skew elevado (> 5%) → mercado pagando por downside → bajista
        SHORT: 1  |  LONG: 0

      P/C ratio < 0.8 → calls dominan → alcista
        LONG: 1  |  SHORT: 0
    """
    data = parse_options(contract, current_price)
    if data is None:
        return None, {}

    ratio     = data.get("put_call_ratio_oi")
    max_pain  = data.get("max_pain")
    otm_skew  = data.get("avg_otm_put_skew")

    if ratio is None:
        return None, data

    bearish_options = (
        (ratio >= 1.1) or
        (max_pain is not None and current_price is not None and max_pain < current_price - 0.25) or
        (otm_skew is not None and otm_skew > 5.0)
    )
    bullish_options = ratio < 0.85

    if direction == "LONG":
        # Puts muy elevadas pueden indicar squeeze potencial (contrarian)
        score = 1 if (bullish_options or (ratio > 1.5)) else 0
    else:
        score = 1 if bearish_options else 0

    return score, data
