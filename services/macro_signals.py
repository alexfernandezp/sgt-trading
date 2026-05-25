"""
Señales macro intraday: BRL/USD, Brent crudo, y correlaciones con azúcar.

Tickers Yahoo Finance:
  BRL=X    → USD por BRL (precio de 1 BRL en USD; baja = BRL se debilita)
  BZ=F     → Brent Crude Futures ($/bbl)
  SB=F     → Sugar No.11 continuo (c/lb) — solo para correlación

Logica de señales:
  BRL_signal : BRL fuerte (sube) → favorable para azúcar (costos exportacion Brasil ↑
               hace que los productores vendan más caro → soporte precio).
               BRL débil → productores brazileños venden más barato en USD → presion bajista.

  Brent_signal: Brent alto → etanol caro → mills diverten cana a etanol → menos azucar
                disponible → alcista. Brent bajo → más azúcar producida → bajista.
                Correlacion actual (2026): ~0.42 en períodos activos.

  Correl intraday: rolling 30-60 min correlation entre retornos Brent y Sugar,
                   y entre BRL y Sugar. Si correlacion activa y Brent/BRL mueven,
                   predice direccion probable del azucar.
"""
import logging
from datetime import datetime, timedelta, date
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Yahoo Finance tickers
TICKER_BRL   = "BRL=X"    # USD/BRL (precio de 1 BRL en dólares)
TICKER_BRENT = "BZ=F"     # Brent Crude
TICKER_SB    = "SB=F"     # Sugar No.11 continuo

# Parámetros de señal
BRL_HIST_MEAN    = 0.195   # USD/BRL largo plazo ~5.15 BRL/USD → 1/5.15 ≈ 0.194
BRL_SD           = 0.010   # desviación estándar aproximada
BRENT_NEUTRAL    = 75.0    # $/bbl — zona neutral
BRENT_BULLISH    = 85.0    # $/bbl — por encima → alcista azucar (etanol parity)
BRENT_BEARISH    = 65.0    # $/bbl — por debajo → bajista azucar
CORREL_WINDOW    = 20      # barras 5m para correlacion rolling (~100 min)
CORREL_THRESHOLD = 0.45    # correlacion mínima para señal activa


def _fetch_yf(ticker: str, period: str = "5d", interval: str = "5m") -> Optional[pd.DataFrame]:
    """Descarga datos de Yahoo Finance con manejo de errores."""
    try:
        import yfinance as yf
        df = yf.download(ticker, period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if df is None or len(df) < 5:
            return None
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        return df
    except Exception as e:
        logger.warning("yf download %s: %s", ticker, e)
        return None


def _fetch_daily(ticker: str, period: str = "60d") -> Optional[pd.DataFrame]:
    try:
        import yfinance as yf
        df = yf.download(ticker, period=period, interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or len(df) < 10:
            return None
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        return df
    except Exception as e:
        logger.warning("yf daily %s: %s", ticker, e)
        return None


# ---------------------------------------------------------------------------
# BRL / USD signal
# ---------------------------------------------------------------------------

def compute_brl_signal(price: Optional[float] = None) -> dict:
    """
    Señal BRL/USD.

    Devuelve:
      brl_usd      : precio actual USD/BRL (cuantos BRL vale 1 USD)
      usd_brl      : inverso (cuantos dólares vale 1 BRL)
      signal       : +1 (alcista azucar) / -1 (bajista) / 0 (neutral)
      change_1d_pct: variacion % en 1 dia
      change_5d_pct: variacion % en 5 dias
      bias         : "LONG" / "SHORT" / "NEUTRAL"
      description  : texto
    """
    df = _fetch_daily(TICKER_BRL, period="30d")

    if df is None or len(df) < 5:
        return {"signal": 0, "bias": "NEUTRAL", "brl_usd": None,
                "description": "BRL/USD: sin datos"}

    # BRL=X es USD/BRL (cuantos USD vale 1 BRL)
    # Si BRL=X sube → BRL se fortalece → señal LONG azúcar
    latest   = float(df["Close"].iloc[-1])
    prev1d   = float(df["Close"].iloc[-2])   if len(df) >= 2  else latest
    prev5d   = float(df["Close"].iloc[-6])   if len(df) >= 6  else latest
    ma20     = float(df["Close"].tail(20).mean())

    chg_1d = (latest - prev1d) / prev1d * 100
    chg_5d = (latest - prev5d) / prev5d * 100
    vs_ma20 = (latest - ma20) / ma20 * 100

    # BRL/USD sube = BRL se fortalece = alcista para azúcar (más costoso producir en USD)
    # BRL/USD baja = BRL se debilita = bajista para azúcar
    if vs_ma20 > 1.5 and chg_5d > 1.0:
        signal = 1; bias = "LONG"
        desc = f"BRL fuerte: {latest:.4f} USD/BRL (+{vs_ma20:.1f}% vs MA20) → presión alcista azucar"
    elif vs_ma20 < -1.5 and chg_5d < -1.0:
        signal = -1; bias = "SHORT"
        desc = f"BRL débil: {latest:.4f} USD/BRL ({vs_ma20:.1f}% vs MA20) → presión bajista azucar"
    else:
        signal = 0; bias = "NEUTRAL"
        desc = f"BRL neutral: {latest:.4f} USD/BRL ({vs_ma20:+.1f}% vs MA20)"

    # En términos de BRL por USD (lo que el mercado suele citar)
    brl_per_usd = round(1 / latest, 4) if latest > 0 else None

    return {
        "usd_per_brl":    round(latest, 5),
        "brl_per_usd":    brl_per_usd,
        "change_1d_pct":  round(chg_1d, 3),
        "change_5d_pct":  round(chg_5d, 3),
        "vs_ma20_pct":    round(vs_ma20, 3),
        "signal":         signal,
        "bias":           bias,
        "description":    desc,
    }


# ---------------------------------------------------------------------------
# Brent signal
# ---------------------------------------------------------------------------

def compute_brent_signal() -> dict:
    """
    Señal Brent crudo.

    Logica etanol parity:
      Brent > 85 → etanol caro → mills Brazil prefieren etanol → menos azucar → LONG
      Brent < 65 → etanol barato → mills prefieren azucar → mas oferta → SHORT
      Zona neutral 65-85.

    Devuelve signal, bias, brent_price, change_1d_pct.
    """
    df = _fetch_daily(TICKER_BRENT, period="30d")

    if df is None or len(df) < 5:
        return {"signal": 0, "bias": "NEUTRAL", "brent_price": None,
                "description": "Brent: sin datos"}

    latest  = float(df["Close"].iloc[-1])
    prev1d  = float(df["Close"].iloc[-2]) if len(df) >= 2 else latest
    prev5d  = float(df["Close"].iloc[-6]) if len(df) >= 6 else latest
    ma20    = float(df["Close"].tail(20).mean())

    chg_1d = (latest - prev1d) / prev1d * 100
    chg_5d = (latest - prev5d) / prev5d * 100

    if latest >= BRENT_BULLISH:
        signal = 1; bias = "LONG"
        desc = f"Brent ${latest:.1f}/bbl (>{BRENT_BULLISH}) → etanol parity activa → LONG azucar"
    elif latest <= BRENT_BEARISH:
        signal = -1; bias = "SHORT"
        desc = f"Brent ${latest:.1f}/bbl (<{BRENT_BEARISH}) → mills prefieren azucar → SHORT"
    else:
        # Zona neutral: usar momentum
        signal = 0; bias = "NEUTRAL"
        if chg_5d > 3.0:
            desc = f"Brent ${latest:.1f}/bbl (neutral, subiendo {chg_5d:+.1f}% 5d) → vigilar"
        elif chg_5d < -3.0:
            desc = f"Brent ${latest:.1f}/bbl (neutral, cayendo {chg_5d:+.1f}% 5d) → vigilar"
        else:
            desc = f"Brent ${latest:.1f}/bbl (neutral {chg_5d:+.1f}% 5d)"

    return {
        "brent_price":   round(latest, 2),
        "change_1d_pct": round(chg_1d, 3),
        "change_5d_pct": round(chg_5d, 3),
        "vs_ma20":       round(latest - ma20, 2),
        "signal":        signal,
        "bias":          bias,
        "description":   desc,
    }


# ---------------------------------------------------------------------------
# Intraday correlation signal
# ---------------------------------------------------------------------------

def compute_intraday_correlation(direction: str = "LONG") -> dict:
    """
    Calcula correlacion rolling 5m entre:
      - Brent % returns y Sugar % returns
      - BRL % returns y Sugar % returns

    Si la correlacion es fuerte (>0.45) y la tendencia del driver (Brent/BRL)
    es consistente con la direccion del trade, amplifica la señal.

    Devuelve:
      corr_brent_sugar : float (-1 a +1), correlacion 5m ultimas 2h
      corr_brl_sugar   : float
      brent_trend_5m   : % change en ultimas 12 barras 5m (1h)
      brl_trend_5m     : % change
      signal           : +1 / -1 / 0
      bias             : "LONG" / "SHORT" / "NEUTRAL"
    """
    # Descargar 5 dias de datos 5m para cada instrumento
    sb   = _fetch_yf(TICKER_SB,    period="5d", interval="5m")
    bz   = _fetch_yf(TICKER_BRENT, period="5d", interval="5m")
    brl  = _fetch_yf(TICKER_BRL,   period="5d", interval="5m")

    result = {
        "corr_brent_sugar": None,
        "corr_brl_sugar":   None,
        "brent_trend_5m":   None,
        "brl_trend_5m":     None,
        "signal": 0, "bias": "NEUTRAL",
        "description": "Correlacion intraday: datos insuficientes",
    }

    if sb is None or len(sb) < CORREL_WINDOW:
        return result

    ret_sb = sb["Close"].pct_change().dropna()

    # Correlacion Brent - Sugar
    corr_brent = None
    brent_trend = None
    if bz is not None and len(bz) >= CORREL_WINDOW:
        ret_bz = bz["Close"].pct_change().dropna()
        aligned = ret_sb.align(ret_bz, join="inner")
        if len(aligned[0]) >= CORREL_WINDOW:
            window_sb  = aligned[0].iloc[-CORREL_WINDOW:]
            window_bz  = aligned[1].iloc[-CORREL_WINDOW:]
            corr_brent = float(window_sb.corr(window_bz))
            # Tendencia Brent ultima hora (12 barras 5m)
            brent_recent = bz["Close"].iloc[-12:]
            brent_trend  = float((brent_recent.iloc[-1] - brent_recent.iloc[0]) / brent_recent.iloc[0] * 100)

    # Correlacion BRL - Sugar
    corr_brl = None
    brl_trend = None
    if brl is not None and len(brl) >= CORREL_WINDOW:
        ret_brl = brl["Close"].pct_change().dropna()
        aligned = ret_sb.align(ret_brl, join="inner")
        if len(aligned[0]) >= CORREL_WINDOW:
            window_sb  = aligned[0].iloc[-CORREL_WINDOW:]
            window_brl = aligned[1].iloc[-CORREL_WINDOW:]
            corr_brl  = float(window_sb.corr(window_brl))
            brl_recent = brl["Close"].iloc[-12:]
            brl_trend  = float((brl_recent.iloc[-1] - brl_recent.iloc[0]) / brl_recent.iloc[0] * 100)

    result["corr_brent_sugar"] = round(corr_brent, 3) if corr_brent is not None else None
    result["corr_brl_sugar"]   = round(corr_brl, 3)   if corr_brl   is not None else None
    result["brent_trend_5m"]   = round(brent_trend, 3) if brent_trend is not None else None
    result["brl_trend_5m"]     = round(brl_trend, 3)   if brl_trend  is not None else None

    # Señal combinada: correlacion activa + driver confirma direccion
    sign_mult = 1 if direction.upper() == "LONG" else -1
    signals   = []

    if corr_brent is not None and abs(corr_brent) >= CORREL_THRESHOLD and brent_trend is not None:
        # Si correlacion positiva y Brent sube → azucar sube → LONG confirma
        expected_sb_move = corr_brent * brent_trend   # positivo = alcista
        if expected_sb_move * sign_mult > 0:
            signals.append(("Brent", corr_brent, brent_trend, "confirma"))
        elif abs(expected_sb_move) > 0.10:
            signals.append(("Brent", corr_brent, brent_trend, "contradice"))

    if corr_brl is not None and abs(corr_brl) >= CORREL_THRESHOLD and brl_trend is not None:
        expected_sb_move = corr_brl * brl_trend
        if expected_sb_move * sign_mult > 0:
            signals.append(("BRL", corr_brl, brl_trend, "confirma"))
        elif abs(expected_sb_move) > 0.10:
            signals.append(("BRL", corr_brl, brl_trend, "contradice"))

    n_confirm = sum(1 for s in signals if s[3] == "confirma")
    n_contra  = sum(1 for s in signals if s[3] == "contradice")

    if n_confirm > n_contra and n_confirm >= 1:
        result["signal"] = 1
        result["bias"]   = direction.upper()
    elif n_contra > n_confirm and n_contra >= 1:
        result["signal"] = -1
        result["bias"]   = "SHORT" if direction.upper() == "LONG" else "LONG"
    else:
        result["signal"] = 0
        result["bias"]   = "NEUTRAL"

    parts = []
    if corr_brent is not None:
        parts.append(f"ρ(Brent/SB)={corr_brent:+.2f}")
        if brent_trend is not None:
            parts.append(f"Brent1h={brent_trend:+.1f}%")
    if corr_brl is not None:
        parts.append(f"ρ(BRL/SB)={corr_brl:+.2f}")
        if brl_trend is not None:
            parts.append(f"BRL1h={brl_trend:+.1f}%")

    sig_strs = [f"{s[0]}:{s[3]}" for s in signals] if signals else ["sin señal activa"]
    result["description"] = "Correlacion 5m: " + "  ".join(parts) + "  [" + "  ".join(sig_strs) + "]"

    return result


# ---------------------------------------------------------------------------
# Combined macro signal
# ---------------------------------------------------------------------------

def compute_macro_signals(direction: str = "LONG", session=None) -> dict:
    """
    Combina BRL, Brent, correlacion intraday y paridad etanol-azúcar.

    Devuelve dict con todos los sub-signals y un macro_score (-4 a +4).
    La paridad etanol (CEPEA) es el indicador fundamental más directo del
    mix mills Brasil; Brent es el confirmador macro secundario.
    """
    brl   = compute_brl_signal()
    brent = compute_brent_signal()
    corr  = compute_intraday_correlation(direction)

    # Paridad etanol (requiere session de DB con datos CEPEA)
    parity = {"signal": 0, "bias": "NEUTRAL",
               "description": "Paridad etanol: session no disponible"}
    if session is not None:
        try:
            from services.ethanol_parity import compute_ethanol_parity
            ice_c_lb = brent.get("_ice_c_lb")   # None → yf internamente
            parity = compute_ethanol_parity(session, ice_price_c_lb=ice_c_lb)
        except Exception as e:
            logger.warning("macro_signals: parity error: %s", e)

    # Score: cada sub-señal aporta -1/0/+1 ajustado por direccion
    dir_mult = 1 if direction.upper() == "LONG" else -1

    score_brl    = brl["signal"]    * dir_mult
    score_brent  = brent["signal"]  * dir_mult
    score_corr   = corr["signal"]               # ya ajustado por direction
    score_parity = parity["signal"] * dir_mult  # +1 = confirma direccion

    macro_score = score_brl + score_brent + score_corr + score_parity   # -4 a +4

    if macro_score >= 3:
        macro_bias = "STRONG_" + direction.upper()
    elif macro_score >= 1:
        macro_bias = direction.upper()
    elif macro_score <= -3:
        macro_bias = "STRONG_CONTRA"
    elif macro_score <= -1:
        macro_bias = "CONTRA"
    else:
        macro_bias = "NEUTRAL"

    return {
        "brl":         brl,
        "brent":       brent,
        "corr":        corr,
        "parity":      parity,
        "macro_score": macro_score,
        "macro_bias":  macro_bias,
        "direction":   direction.upper(),
    }
