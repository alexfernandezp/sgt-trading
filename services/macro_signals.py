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


def _fetch_last_price(ticker: str) -> Optional[float]:
    """
    Precio más reciente del ticker, en orden de preferencia:
      1. yfinance fast_info  (cuasi-tiempo-real)
      2. yfinance intraday 1m / 5m
    Retorna None si el mercado está cerrado o sin datos intraday.
    """
    try:
        import yfinance as yf

        # 1. fast_info — el más actual en días normales
        try:
            fi = yf.Ticker(ticker).fast_info
            price = getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None)
            if price and float(price) > 0:
                return float(price)
        except Exception as exc:
            logger.debug("fetch_last_price fast_info %s: %s", ticker, exc)

        # 2. Intraday 1m / 5m
        for iv in ("1m", "5m"):
            try:
                df = yf.download(ticker, period="1d", interval=iv,
                                 progress=False, auto_adjust=True)
                if df is not None and not df.empty:
                    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
                    val = df["Close"].dropna()
                    if len(val) > 0:
                        return float(val.iloc[-1])
            except Exception as exc:
                logger.debug("fetch_last_price intraday %s %s: %s", ticker, iv, exc)
                continue

    except Exception as e:
        logger.debug("fetch_last_price %s: %s", ticker, e)

    return None


# ---------------------------------------------------------------------------
# BRL / USD signal
# ---------------------------------------------------------------------------

def compute_brl_signal(price: Optional[float] = None) -> dict:
    """
    Señal BRL/USD.

    NOTA: Yahoo Finance BRL=X devuelve USDBRL (BRL por dólar, ≈5.0).
    Cuando USDBRL SUBE → BRL se DEBILITA → bajista para azúcar (productores BR venden
    más barato en USD) → SHORT.  Cuando USDBRL BAJA → BRL se FORTALECE → LONG.

    Devuelve:
      brl_per_usd  : BRL por dólar (quote de mercado, ≈5.0)
      usd_per_brl  : USD por BRL (inverso, ≈0.20)
      signal       : +1 LONG / -1 SHORT / 0 neutral
      change_1d_pct, change_5d_pct, vs_ma20_pct
      bias, description
    """
    df = _fetch_daily(TICKER_BRL, period="30d")

    if df is None or len(df) < 5:
        return {"signal": 0, "bias": "NEUTRAL", "brl_per_usd": None,
                "description": "BRL/USD: sin datos"}

    # BRL=X de Yahoo Finance = USDBRL ≈ 5.0 (cuántos BRL cuesta 1 USD)
    latest = float(df["Close"].iloc[-1])
    prev1d = float(df["Close"].iloc[-2]) if len(df) >= 2 else latest
    prev5d = float(df["Close"].iloc[-6]) if len(df) >= 6 else latest
    ma20   = float(df["Close"].tail(20).mean())

    chg_1d  = (latest - prev1d) / prev1d * 100
    chg_5d  = (latest - prev5d) / prev5d * 100
    vs_ma20 = (latest - ma20) / ma20 * 100

    # USDBRL baja (BRL se fortalece) → productores necesitan más USD → soporte precio → LONG
    # USDBRL sube (BRL se debilita) → productores venden más barato en USD → presión bajista → SHORT
    if vs_ma20 < -1.5 and chg_5d < -1.0:
        signal = 1; bias = "LONG"
        desc = (f"BRL fuerte: USDBRL={latest:.4f} ({vs_ma20:+.1f}% vs MA20) "
                f"→ costes producción suben en USD → alcista azúcar")
    elif vs_ma20 > 1.5 and chg_5d > 1.0:
        signal = -1; bias = "SHORT"
        desc = (f"BRL débil: USDBRL={latest:.4f} ({vs_ma20:+.1f}% vs MA20) "
                f"→ productores BR venden más barato en USD → bajista azúcar")
    else:
        signal = 0; bias = "NEUTRAL"
        desc = f"BRL neutral: USDBRL={latest:.4f} ({vs_ma20:+.1f}% vs MA20)"

    usd_per_brl = round(1.0 / latest, 5) if latest > 0 else None

    return {
        "brl_per_usd":   round(latest, 4),       # cuántos BRL por 1 USD (≈5.0)
        "usd_per_brl":   usd_per_brl,             # cuántos USD por 1 BRL (≈0.20)
        "change_1d_pct": round(chg_1d, 3),
        "change_5d_pct": round(chg_5d, 3),
        "vs_ma20_pct":   round(vs_ma20, 3),
        "signal":        signal,
        "bias":          bias,
        "description":   desc,
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

    # Precio fresco intraday (5m); la barra diaria puede estar atrasada horas
    latest_intraday = _fetch_last_price(TICKER_BRENT)
    latest = latest_intraday if latest_intraday is not None else float(df["Close"].iloc[-1])

    # Cierre de la sesión anterior (último daily completado)
    # yfinance incluye barra parcial de hoy en iloc[-1] → iloc[-2] = cierre previo real
    n = len(df)
    prev1d = float(df["Close"].iloc[-2]) if n >= 2 else latest
    prev5d = float(df["Close"].iloc[-6]) if n >= 6 else latest
    ma20   = float(df["Close"].tail(20).mean())

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
# Brent correlation regime (uses price_history DB — daily returns)
# ---------------------------------------------------------------------------

BRENT_CORR_HIGH  = 0.35   # corr60d > this = HIGH regime (hist ~pct 85%)
BRENT_ALERT_PCT  = 1.5    # |change_today| > this in HIGH regime = intraday alert

def compute_brent_regime(session, brent_change_1d_pct: float = None) -> dict:
    """
    Calcula el régimen de correlación Brent-Azúcar (rolling 60d, daily returns)
    desde price_history y emite alerta intraday si el régimen es alto y Brent
    mueve más de ±1.5% en la sesión.

    Returns:
      corr_60d        : Pearson corr últimas 60 sesiones (daily ret)
      corr_percentile : percentil histórico de ese valor (0-100)
      regime          : "HIGH" | "NORMAL" | "LOW" | "UNKNOWN"
      alert           : True si HIGH y |brent_change_today| > 1.5%
      alert_direction : "BEARISH_PRESSURE" | "BULLISH_PRESSURE" | None
      description     : str resumen
    """
    from sqlalchemy import text
    import pandas as pd

    _empty = {"corr_60d": None, "corr_percentile": None, "regime": "UNKNOWN",
              "alert": False, "alert_direction": None,
              "description": "Brent regime: sin datos DB"}

    if session is None:
        return _empty

    try:
        # Carga últimos ~2 años de cierres para tener contexto histórico del percentil
        rows_b = session.execute(text(
            "SELECT date, close FROM price_history "
            "WHERE instrument='BRENT' AND close IS NOT NULL "
            "ORDER BY date DESC LIMIT 520"
        )).fetchall()
        rows_s = session.execute(text(
            "SELECT date, close FROM price_history "
            "WHERE instrument='SB_CONT' AND close IS NOT NULL "
            "ORDER BY date DESC LIMIT 520"
        )).fetchall()

        if len(rows_b) < 62 or len(rows_s) < 62:
            return {**_empty, "description": "Brent regime: datos insuficientes en DB"}

        sb = pd.Series({pd.Timestamp(r[0]): float(r[1]) for r in rows_s}).sort_index()
        bz = pd.Series({pd.Timestamp(r[0]): float(r[1]) for r in rows_b}).sort_index()

        df = pd.DataFrame({"bz": bz, "sb": sb}).dropna()
        df["rb"] = df["bz"].pct_change()
        df["rs"] = df["sb"].pct_change()
        df = df.dropna()

        if len(df) < 62:
            return {**_empty, "description": "Brent regime: dias comunes insuficientes"}

        roll = df["rb"].rolling(60).corr(df["rs"]).dropna()
        corr_now = float(roll.iloc[-1])
        pct_rank = float((roll < corr_now).mean() * 100)

        if corr_now > BRENT_CORR_HIGH:
            regime = "HIGH"
        elif corr_now > 0.10:
            regime = "NORMAL"
        else:
            regime = "LOW"

        alert = False
        alert_dir = None
        if regime == "HIGH" and brent_change_1d_pct is not None:
            if abs(brent_change_1d_pct) >= BRENT_ALERT_PCT:
                alert = True
                alert_dir = "BEARISH_PRESSURE" if brent_change_1d_pct < 0 else "BULLISH_PRESSURE"

        chg_s  = (f"  hoy={brent_change_1d_pct:+.1f}%" if brent_change_1d_pct is not None else "")
        alrt_s = (f"  -> {alert_dir}" if alert else "")
        desc = (f"Brent-SB corr60d={corr_now:.3f} (pct={pct_rank:.0f}%)"
                f"  regime={regime}{chg_s}{alrt_s}")

        return {
            "corr_60d":        round(corr_now, 3),
            "corr_percentile": round(pct_rank, 1),
            "regime":          regime,
            "alert":           alert,
            "alert_direction": alert_dir,
            "description":     desc,
        }

    except Exception as exc:
        logger.warning("compute_brent_regime: %s", exc)
        return _empty


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

def compute_dxy_signal() -> dict:
    """
    Señal DXY (US Dollar Index).

    DXY alto (USD fuerte) → commodities cotizados en USD se abaratan → bajista azúcar.
    DXY bajo  (USD débil) → commodities en USD se encarecen → alcista azúcar.

    Devuelve signal, bias, dxy_value, vs_ma20_pct.
    """
    try:
        import yfinance as yf
        df = yf.download("DX-Y.NYB", period="30d", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or len(df) < 5:
            return {"signal": 0, "bias": "NEUTRAL", "dxy_value": None,
                    "description": "DXY: sin datos"}
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    except Exception as e:
        logger.warning("dxy_signal: %s", e)
        return {"signal": 0, "bias": "NEUTRAL", "dxy_value": None,
                "description": "DXY: error descarga"}

    latest = float(df["Close"].iloc[-1])
    prev1d = float(df["Close"].iloc[-2]) if len(df) >= 2 else latest
    ma20   = float(df["Close"].tail(20).mean())

    chg_1d  = (latest - prev1d) / prev1d * 100
    vs_ma20 = (latest - ma20) / ma20 * 100

    if vs_ma20 < -1.0:
        signal = 1; bias = "LONG"
        desc = "DXY=%.2f (%+.1f%% vs MA20) — USD débil → alcista commodities → LONG azúcar" % (latest, vs_ma20)
    elif vs_ma20 > 1.0:
        signal = -1; bias = "SHORT"
        desc = "DXY=%.2f (%+.1f%% vs MA20) — USD fuerte → bajista commodities → SHORT azúcar" % (latest, vs_ma20)
    else:
        signal = 0; bias = "NEUTRAL"
        desc = "DXY=%.2f (%+.1f%% vs MA20) — USD neutral" % (latest, vs_ma20)

    return {
        "dxy_value":     round(latest, 3),
        "vs_ma20_pct":   round(vs_ma20, 3),
        "change_1d_pct": round(chg_1d, 3),
        "signal":        signal,
        "bias":          bias,
        "description":   desc,
    }


def compute_macro_signals(direction: str = "LONG", session=None) -> dict:
    """
    Combina 15 señales macro para azúcar ICE No.11.

    Señales incluidas:
      1.  BRL/USD          — tipo de cambio real brasileño
      2.  Brent            — paridad etanol/energía
      3.  Correl intraday  — correlaciones 5m Brent/BRL vs SB
      4.  Paridad etanol   — CEPEA hydrous vs ICE (fundamental mills Brasil)
      5.  ENSO / ONI       — El Niño/La Niña: impacto estacional producción
      6.  Déficit hídrico  — P-ET30/90d SP + NDVI Sentinel-2
      7.  Full Carry       — spread SBN/SBV vs coste teórico de almacenamiento
      8.  Comex Stat       — ritmo exportaciones YoY azúcar Brasil (MDIC)
      9.  INPE Fuego       — anomalía focos incendio SP vs baseline estacional
      10. CONAB            — Boletim Safra Cana (revisión producción Brasil)
      11. GEE Harvest Pace — NDVI BR+TH+IN vs baseline 5yr
      12. GEE Crop Stress  — LST + NDWI BR+TH+IN
      13. GEE Rainfall SPI — CHIRPS BR+TH+IN
      14. USDA WASDE       — Stocks-to-Use global azúcar (balance oferta/demanda)
      15. DXY              — Índice dólar (USD fuerte = bajista commodities)

    macro_score: −15 a +15
    Thresholds bias:
      ≥ 10 → STRONG direction
      ≥  4 → direction
      ≤ −4 → CONTRA
      ≤ −10 → STRONG_CONTRA
      else  → NEUTRAL
    """
    brl   = compute_brl_signal()
    brent = compute_brent_signal()
    corr  = compute_intraday_correlation(direction)

    # Régimen de correlación Brent-Azúcar (daily, 60d rolling desde DB)
    brent_regime = {"regime": "UNKNOWN", "corr_60d": None, "corr_percentile": None,
                    "alert": False, "alert_direction": None,
                    "description": "Brent regime: session no disponible"}
    if session is not None:
        try:
            brent_regime = compute_brent_regime(session, brent.get("change_1d_pct"))
        except Exception as exc:
            logger.warning("macro_signals: brent_regime: %s", exc)

    # Paridad etanol (requiere session de DB con datos CEPEA)
    parity = {"signal": 0, "bias": "NEUTRAL",
               "description": "Paridad etanol: session no disponible"}
    if session is not None:
        try:
            from services.ethanol_parity import compute_ethanol_parity
            parity = compute_ethanol_parity(session)
        except Exception as e:
            logger.warning("macro_signals: parity error: %s", e)

    # ENSO / ONI signal
    enso = {"signal": 0, "bias": "NEUTRAL",
             "description": "ENSO: sin datos (ejecutar fetch_oni)"}
    if session is not None:
        try:
            from services.enso_signal import compute_enso_signal
            enso = compute_enso_signal(session)
        except Exception as e:
            logger.warning("macro_signals: enso error: %s", e)

    # Déficit hídrico + NDVI
    climate = {"signal": 0, "bias": "NEUTRAL",
                "description": "Déficit hídrico: sin datos (ejecutar fetch_climate)"}
    if session is not None:
        try:
            from services.water_deficit import compute_water_deficit_signal
            climate = compute_water_deficit_signal(session)
        except Exception as e:
            logger.warning("macro_signals: water_deficit error: %s", e)

    # Full Carry calendar spread (no requiere session — usa yfinance)
    carry = {"signal": 0, "bias": "NEUTRAL",
              "description": "Full Carry: sin datos"}
    try:
        from services.full_carry import compute_full_carry_signal
        carry = compute_full_carry_signal()
    except Exception as e:
        logger.warning("macro_signals: full_carry error: %s", e)

    # Comex Stat — ritmo exportaciones YoY
    comex = {"signal": 0, "bias": "NEUTRAL",
             "description": "Comex Stat: sin datos (ejecutar fetch_comex_stat)"}
    if session is not None:
        try:
            from services.comex_signal import compute_comex_signal
            comex = compute_comex_signal(session)
        except Exception as e:
            logger.warning("macro_signals: comex error: %s", e)

    # INPE fuego — anomalía focos incendio SP+PR
    fire = {"signal": 0, "bias": "NEUTRAL",
            "description": "INPE fuego: sin datos (ejecutar fetch_fires)"}
    if session is not None:
        try:
            from services.fire_signal import compute_fire_signal
            fire = compute_fire_signal(session, state="SP+PR")
        except Exception as e:
            logger.warning("macro_signals: fire error: %s", e)

    # CONAB — Boletim Safra Cana (revisión intra-temporada + YoY)
    conab = {"signal": 0, "bias": "NEUTRAL",
             "description": "CONAB: sin datos (ejecutar fetch_conab.py)"}
    if session is not None:
        try:
            from services.conab_signal import compute_conab_signal
            conab = compute_conab_signal(session)
        except Exception as e:
            logger.warning("macro_signals: conab error: %s", e)

    # GEE — Harvest Pace (NDVI BR+TH+IN vs baseline 5yr)
    harvest_pace = {"signal": 0, "bias": "NEUTRAL",
                    "description": "Harvest pace: sin datos GEE (ejecutar run_gee_crops.py)"}
    if session is not None:
        try:
            from services.harvest_pace_signal import compute_harvest_pace_signal
            harvest_pace = compute_harvest_pace_signal(session)
        except Exception as e:
            logger.warning("macro_signals: harvest_pace error: %s", e)

    # GEE — Crop Stress (LST + NDWI BR+TH+IN)
    crop_stress = {"signal": 0, "bias": "NEUTRAL",
                   "description": "Crop stress: sin datos GEE (ejecutar run_gee_crops.py)"}
    if session is not None:
        try:
            from services.crop_stress_signal import compute_crop_stress_signal
            crop_stress = compute_crop_stress_signal(session)
        except Exception as e:
            logger.warning("macro_signals: crop_stress error: %s", e)

    # GEE — Rainfall SPI-90 (CHIRPS BR+TH+IN)
    rainfall = {"signal": 0, "bias": "NEUTRAL",
                "description": "Rainfall SPI: sin datos GEE (ejecutar run_gee_crops.py)"}
    if session is not None:
        try:
            from services.rainfall_signal import compute_rainfall_signal
            rainfall = compute_rainfall_signal(session)
        except Exception as e:
            logger.warning("macro_signals: rainfall error: %s", e)

    # USDA WASDE — Stocks-to-Use ratio global azúcar
    usda = {"signal": 0, "bias": "NEUTRAL",
            "description": "USDA WASDE: sin datos (py scripts/fetch_usda.py)"}
    if session is not None:
        try:
            from services.usda_signal import compute_usda_signal
            usda = compute_usda_signal(session)
        except Exception as e:
            logger.warning("macro_signals: usda error: %s", e)

    # DXY — Índice dólar (USD fuerte = bajista commodities)
    dxy = {"signal": 0, "bias": "NEUTRAL",
           "description": "DXY: sin datos"}
    try:
        dxy = compute_dxy_signal()
    except Exception as e:
        logger.warning("macro_signals: dxy error: %s", e)

    # ── Scoring ──────────────────────────────────────────────────────────────
    dir_mult = 1 if direction.upper() == "LONG" else -1

    score_brl          = brl["signal"]          * dir_mult
    score_brent        = brent["signal"]        * dir_mult
    score_corr         = corr["signal"]                      # ajustado internamente
    score_parity       = parity["signal"]       * dir_mult
    score_enso         = enso["signal"]         * dir_mult
    score_climate      = climate["signal"]      * dir_mult
    score_carry        = carry["signal"]        * dir_mult
    score_comex        = comex["signal"]        * dir_mult
    score_fire         = fire["signal"]         * dir_mult
    score_conab        = conab["signal"]        * dir_mult
    score_harvest_pace = harvest_pace["signal"] * dir_mult
    score_crop_stress  = crop_stress["signal"]  * dir_mult
    score_rainfall     = rainfall["signal"]     * dir_mult
    score_usda         = usda["signal"]         * dir_mult
    score_dxy          = dxy["signal"]          * dir_mult

    macro_score = (score_brl + score_brent + score_corr + score_parity
                   + score_enso + score_climate + score_carry
                   + score_comex + score_fire + score_conab
                   + score_harvest_pace + score_crop_stress + score_rainfall
                   + score_usda + score_dxy)  # −15 a +15

    if macro_score >= 10:
        macro_bias = "STRONG_" + direction.upper()
    elif macro_score >= 4:
        macro_bias = direction.upper()
    elif macro_score <= -10:
        macro_bias = "STRONG_CONTRA"
    elif macro_score <= -4:
        macro_bias = "CONTRA"
    else:
        macro_bias = "NEUTRAL"

    return {
        "brl":          brl,
        "brent":        brent,
        "brent_regime": brent_regime,
        "corr":         corr,
        "parity":       parity,
        "enso":         enso,
        "climate":      climate,
        "carry":        carry,
        "comex":        comex,
        "fire":         fire,
        "conab":        conab,
        "harvest_pace": harvest_pace,
        "crop_stress":  crop_stress,
        "rainfall":     rainfall,
        "usda":         usda,
        "dxy":          dxy,
        "macro_score":  macro_score,
        "macro_bias":   macro_bias,
        "direction":    direction.upper(),
    }
