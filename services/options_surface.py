"""
Superficie de volatilidad implícita y señales de opciones avanzadas.

Lee los CSVs de Barchart descargados en OneDrive:
  data/Options Greeks/SBN26/*.csv  → IV por strike, delta, skew
  data/Options Chain/SBN26/*.csv   → OI, volumen, put/call ratio

Métricas calculadas:
  atm_iv        : IV implícita ATM (call+put avg)
  iv_25d_rr     : Risk Reversal 25Δ = IV(call25Δ) - IV(put25Δ)  [skew]
  iv_25d_bf     : Butterfly 25Δ = (IV(c25Δ)+IV(p25Δ))/2 - ATM_IV  [kurtosis/wings]
  term_structure: [SBN26, SBV26, SBH27] ATM IVs y fechas exp
  put_call_oi   : put OI / call OI (>1 = bearish bias en opciones)
  put_call_vol  : put volume / call volume
  max_pain      : strike con mayor dolor a expiración
  vol_skew_bias : "CALL_SKEW" / "PUT_SKEW" / "FLAT"  (señal gamma/vega)
"""
import os
import logging
import re
from datetime import date, datetime
from typing import Optional

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# Ruta base OneDrive
_ONEDRIVE = os.path.join(
    os.path.expanduser("~"),
    "OneDrive - Sugar Global Trading",
    "sgt_trading", "data",
)

CHAIN_DIR  = os.path.join(_ONEDRIVE, "Options Chain")
GREEKS_DIR = os.path.join(_ONEDRIVE, "Options Greeks")


def _latest_csv(folder: str, contract: str) -> Optional[str]:
    """Devuelve el CSV más reciente para un contrato (SBN26, SBV26, SBH27)."""
    if not os.path.isdir(folder):
        return None
    sub = os.path.join(folder, contract.upper())
    if not os.path.isdir(sub):
        # Intentar sin subcarpeta
        sub = folder
    files = [
        f for f in os.listdir(sub)
        if f.endswith(".csv") and contract.lower()[:3] in f.lower()
    ]
    if not files:
        return None
    files.sort()
    return os.path.join(sub, files[-1])


def _read_csv_safe(path: str) -> Optional[pd.DataFrame]:
    try:
        return pd.read_csv(path)
    except Exception as e:
        logger.warning("Error leyendo %s: %s", path, e)
        return None


# ---------------------------------------------------------------------------
# Greeks parser → IV surface
# ---------------------------------------------------------------------------

def _parse_greeks(contract: str) -> Optional[pd.DataFrame]:
    """
    Lee el CSV de Greeks de Barchart y devuelve DataFrame limpio con:
    strike, type, iv, delta, gamma, theta, vega, iv_skew
    """
    path = _latest_csv(GREEKS_DIR, contract)
    if not path:
        return None

    df = _read_csv_safe(path)
    if df is None:
        return None

    # Normalizar nombres de columnas
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    rename = {
        "strike": "strike", "type": "type", "latest": "price",
        "iv": "iv_raw", "delta": "delta", "gamma": "gamma",
        "theta": "theta", "vega": "vega", "iv_skew": "iv_skew_raw",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    if "strike" not in df.columns:
        return None

    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df["delta"]  = pd.to_numeric(df["delta"],  errors="coerce")

    # Parsear IV (puede venir como "33.42%" o 0.3342)
    def _parse_iv(x):
        try:
            s = str(x).strip().replace("%", "")
            v = float(s)
            return v / 100 if v > 1.5 else v   # normalizar a fracción
        except Exception:
            return np.nan

    if "iv_raw" in df.columns:
        df["iv"] = df["iv_raw"].apply(_parse_iv)
    else:
        df["iv"] = np.nan

    df["type"] = df["type"].str.strip().str.capitalize() if "type" in df.columns else "Call"
    df = df.dropna(subset=["strike"])
    return df


# ---------------------------------------------------------------------------
# Chain parser → OI / volume
# ---------------------------------------------------------------------------

def _parse_chain(contract: str) -> Optional[pd.DataFrame]:
    path = _latest_csv(CHAIN_DIR, contract)
    if not path:
        return None

    df = _read_csv_safe(path)
    if df is None:
        return None

    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    # Extraer strike y tipo del campo "Strike" (e.g. "15.00C", "15.00P")
    if "strike" in df.columns:
        df["strike_raw"] = df["strike"].astype(str)
        df["type"] = df["strike_raw"].str.extract(r"([CP])$", expand=False).str.upper()
        df["type"] = df["type"].map({"C": "Call", "P": "Put"})
        df["strike"] = pd.to_numeric(df["strike_raw"].str.replace(r"[CP]$", "", regex=True), errors="coerce")

    for col in ["open_int", "volume", "latest", "bid", "ask"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    return df


# ---------------------------------------------------------------------------
# Core metrics
# ---------------------------------------------------------------------------

def _atm_iv(greeks_df: pd.DataFrame, spot: float) -> Optional[float]:
    """ATM IV: promedio de call y put al strike más cercano al spot."""
    if greeks_df is None or len(greeks_df) == 0:
        return None
    atm_strike = greeks_df["strike"].sub(spot).abs().idxmin()
    atm_price  = greeks_df.loc[atm_strike, "strike"]

    near = greeks_df[greeks_df["strike"] == atm_price]
    ivs  = near["iv"].dropna()
    valid = ivs[ivs > 0]
    return float(valid.mean()) if len(valid) > 0 else None


def _rr25_bf25(greeks_df: pd.DataFrame) -> tuple[Optional[float], Optional[float]]:
    """
    Risk Reversal 25Δ y Butterfly 25Δ.
    Busca strikes con |delta| ≈ 0.25 (calls y puts).
    """
    if greeks_df is None or "delta" not in greeks_df.columns:
        return None, None

    calls = greeks_df[greeks_df["type"] == "Call"].dropna(subset=["delta", "iv"])
    puts  = greeks_df[greeks_df["type"] == "Put"].dropna(subset=["delta", "iv"])

    if len(calls) == 0 or len(puts) == 0:
        return None, None

    # 25Δ call: delta closest to +0.25
    calls_valid = calls[calls["iv"] > 0]
    puts_valid  = puts[puts["iv"]  > 0]

    if len(calls_valid) == 0 or len(puts_valid) == 0:
        return None, None

    c25_idx = calls_valid["delta"].sub(0.25).abs().idxmin()
    p25_idx = puts_valid["delta"].sub(-0.25).abs().idxmin()

    iv_c25 = float(calls_valid.loc[c25_idx, "iv"])
    iv_p25 = float(puts_valid.loc[p25_idx, "iv"])

    # ATM IV: calls with delta ≈ 0.50
    atm_idx    = calls_valid["delta"].sub(0.50).abs().idxmin()
    iv_atm_ref = float(calls_valid.loc[atm_idx, "iv"]) if len(calls_valid) > 0 else (iv_c25 + iv_p25) / 2

    rr25 = iv_c25 - iv_p25          # positivo = call skew (bullish)
    bf25 = (iv_c25 + iv_p25) / 2 - iv_atm_ref   # positivo = fat tails

    return round(rr25, 4), round(bf25, 4)


def _put_call_ratios(chain_df: pd.DataFrame) -> dict:
    """Put/Call OI ratio y Put/Call volume ratio."""
    if chain_df is None:
        return {"put_call_oi": None, "put_call_vol": None, "max_pain": None}

    calls = chain_df[chain_df["type"] == "Call"]
    puts  = chain_df[chain_df["type"] == "Put"]

    total_call_oi  = calls["open_int"].sum() if "open_int" in calls.columns else 0
    total_put_oi   = puts["open_int"].sum()  if "open_int" in puts.columns  else 0
    total_call_vol = calls["volume"].sum()   if "volume"   in calls.columns else 0
    total_put_vol  = puts["volume"].sum()    if "volume"   in puts.columns  else 0

    pc_oi  = round(total_put_oi / total_call_oi, 3)   if total_call_oi > 0 else None
    pc_vol = round(total_put_vol / total_call_vol, 3) if total_call_vol > 0 else None

    # Max Pain: strike donde el total de OI valor expira sin valor es mínimo
    max_pain = None
    if "strike" in chain_df.columns and "open_int" in chain_df.columns:
        strikes = sorted(chain_df["strike"].dropna().unique())
        if len(strikes) >= 3:
            pain = {}
            for s in strikes:
                call_pain = sum(max(0, s - k) * oi for k, oi in zip(
                    calls["strike"], calls["open_int"]) if not pd.isna(k))
                put_pain  = sum(max(0, k - s) * oi for k, oi in zip(
                    puts["strike"], puts["open_int"]) if not pd.isna(k))
                pain[s] = call_pain + put_pain
            if pain:
                max_pain = min(pain, key=pain.get)

    return {
        "put_call_oi":    pc_oi,
        "put_call_vol":   pc_vol,
        "total_call_oi":  int(total_call_oi),
        "total_put_oi":   int(total_put_oi),
        "max_pain":       max_pain,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_options_surface(
    spot: float,
    contracts: list[str] = None,
) -> dict:
    """
    Calcula la superficie de volatilidad completa para los contratos dados.

    Args:
      spot      : precio actual del futuro (SBN26 o similar)
      contracts : lista de contratos a procesar (default: SBN26, SBV26, SBH27)

    Devuelve:
      by_contract : {contract: {atm_iv, rr25, bf25, put_call_oi, ...}}
      term_structure : [atm_iv por fecha expiración]
      skew_bias : "CALL_SKEW" / "PUT_SKEW" / "FLAT"
      signal    : +1 (alcista) / -1 (bajista) / 0 (neutral)
      description
    """
    if contracts is None:
        contracts = ["SBN26", "SBV26", "SBH27"]

    result = {"by_contract": {}, "term_structure": [], "skew_bias": "FLAT",
              "signal": 0, "description": "Superficie vol: procesando..."}

    for ctrt in contracts:
        g_df = _parse_greeks(ctrt)
        c_df = _parse_chain(ctrt)

        atm = _atm_iv(g_df, spot)
        rr25, bf25 = _rr25_bf25(g_df)
        pc = _put_call_ratios(c_df)

        result["by_contract"][ctrt] = {
            "atm_iv":      round(atm * 100, 2) if atm else None,   # en %
            "rr25":        round(rr25 * 100, 2) if rr25 else None,
            "bf25":        round(bf25 * 100, 2) if bf25 else None,
            **pc,
        }
        if atm:
            result["term_structure"].append({
                "contract": ctrt,
                "atm_iv_pct": round(atm * 100, 2),
            })

    # Señal basada en skew del contrato front (SBN26)
    front = result["by_contract"].get("SBN26", {})
    rr    = front.get("rr25")
    pc_oi = front.get("put_call_oi")
    atm   = front.get("atm_iv")

    # Skew
    if rr is not None:
        if rr > 1.0:
            result["skew_bias"] = "CALL_SKEW"   # mercado compra calls → expectativa alcista
            result["signal"]    = 1
        elif rr < -1.0:
            result["skew_bias"] = "PUT_SKEW"    # mercado compra puts → cobertura bajista
            result["signal"]    = -1
        else:
            result["skew_bias"] = "FLAT"

    # Resumen
    atm_s  = f"ATM_IV={atm:.1f}%" if atm else "ATM_IV=N/D"
    rr_s   = f"RR25={rr:+.1f}%" if rr is not None else "RR25=N/D"
    pc_s   = f"P/C_OI={pc_oi:.2f}" if pc_oi else "P/C=N/D"
    mp_s   = f"MaxPain={front.get('max_pain', 'N/D')}"
    result["description"] = f"Vol surface SBN26: {atm_s}  {rr_s}  {pc_s}  {mp_s}  [{result['skew_bias']}]"

    return result


def get_vol_surface_for_score(spot: float) -> dict:
    """
    Wrapper para score_today.py — devuelve la superficie + señal C3 mejorada.
    """
    surf = compute_options_surface(spot)
    front = surf["by_contract"].get("SBN26", {})

    pc_oi = front.get("put_call_oi")
    atm   = front.get("atm_iv")
    rr25  = front.get("rr25")
    mp    = front.get("max_pain")

    # C3 — MaxPain como señal principal (gravedad del mercado de opciones).
    # El precio gravita hacia MaxPain en el vencimiento porque los market makers
    # de opciones tienen incentivo de neutralizar su delta llevando el precio ahí.
    #
    # LONG  [OK]: precio < MaxPain  (tiro gravitacional hacia ARRIBA)
    #             Confirmación: P/C > 1.2 (puts pesadas = posible squeeze alcista)
    # SHORT [OK]: precio > MaxPain  (tiro gravitacional hacia ABAJO)
    #             Confirmación: P/C < 0.85 (calls dominan = crowded long vulnerable)
    #
    # Si precio está dentro de ±0.15 del MaxPain: zona de equilibrio → 0 para ambos.
    # Señal mutuamente exclusiva: nunca [OK] en las dos direcciones simultáneamente.
    def _c3(direction, price):
        if mp is None:
            # Sin MaxPain: usar solo P/C como fallback estricto
            if pc_oi is None:
                return None
            if direction == "LONG":
                return 1 if pc_oi < 0.85 else 0   # más calls = momentum alcista
            else:
                return 1 if pc_oi > 1.20 else 0   # más puts = bearish sentiment

        dead_zone = 0.15                         # c/lb de zona muerta alrededor del MaxPain
        below_mp  = price < (mp - dead_zone)     # precio claramente bajo MaxPain
        above_mp  = price > (mp + dead_zone)     # precio claramente sobre MaxPain

        if direction == "LONG":
            return 1 if below_mp else 0
        else:
            return 1 if above_mp else 0

    surf["c3_long"]  = _c3("LONG",  spot)
    surf["c3_short"] = _c3("SHORT", spot)
    return surf
