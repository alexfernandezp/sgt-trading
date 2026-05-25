"""
Paridad etanol-azúcar usando precios físicos CEPEA/ESALQ.

Lógica:
  Los ingenios brasileños eligen entre producir azúcar o etanol
  según cuál genera más ingreso por tonelada de caña procesada.

  Conversión ATR estándar (CTC/Consecana):
    1 m³ etanol hidratado ≈ 1.20 ton azúcar equivalente

  Precio ICE No.11 en c/lb → US$/ton:
    ice_usd_ton = (ice_c_per_lb / 100) * 2204.62

  Ratio de paridad:
    parity_ratio = (hydrous_paulinia_usd_m3 / 1.20) / ice_usd_ton

  Interpretación:
    ratio > 1.05  → etanol vale +5% más que azúcar por ton ATR
                   → mills diverten caña a etanol → menos azúcar → LONG SB
    ratio < 0.95  → azúcar vale +5% más que etanol
                   → mills producen más azúcar → más oferta → SHORT SB
    0.95–1.05     → zona neutral

Fuentes:
  Etanol:  CEPEA hydrous_paulinia_usd_m3 (diario)
  Azúcar físico: CEPEA crystal_sugar_usd_bag50kg (diario, bag 50 kg)
  ICE No.11: yfinance SB=F o DB price_history
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Factor de conversión ATR (Consecana/CTC industria estándar)
ATR_FACTOR = 1.20          # 1 m³ etanol hidratado ≈ 1.20 ton azúcar equiv
BAG_KG     = 50            # bolsa 50 kg → 20 bolsas/ton
LBS_PER_TON = 2204.62      # lbs por tonelada métrica

# Umbrales de señal
PARITY_BULLISH = 1.05      # etanol > 5% más caro → LONG azúcar
PARITY_BEARISH = 0.95      # azúcar > 5% más cara → SHORT azúcar


def compute_ethanol_parity(
    session,
    ice_price_c_lb: Optional[float] = None,
) -> dict:
    """
    Calcula la paridad etanol-azúcar usando los últimos precios CEPEA.

    Args:
        session        : SQLAlchemy session con acceso a cepea_prices
        ice_price_c_lb : precio ICE No.11 en c/lb (opcional; si None se obtiene de yfinance)

    Returns dict con:
        hydrous_usd_m3       : precio etanol hidratado Paulínia (US$/m³)
        hydrous_date         : fecha del dato CEPEA
        ethanol_usd_ton      : equivalente azúcar del etanol (US$/ton)
        crystal_usd_ton      : precio azúcar cristal físico (US$/ton)
        crystal_date         : fecha del dato CEPEA
        ice_usd_ton          : precio ICE No.11 referencia (US$/ton)
        ice_c_lb             : precio ICE No.11 (c/lb)
        parity_ratio         : ethanol_usd_ton / ice_usd_ton
        parity_ratio_physical: ethanol_usd_ton / crystal_usd_ton (azúcar físico vs etanol)
        signal               : +1 (LONG) / -1 (SHORT) / 0 (NEUTRAL)
        bias                 : "LONG" / "SHORT" / "NEUTRAL"
        description          : texto interpretativo
    """
    result = {
        "hydrous_usd_m3":        None,
        "hydrous_date":          None,
        "ethanol_usd_ton":       None,
        "crystal_usd_ton":       None,
        "crystal_date":          None,
        "ice_usd_ton":           None,
        "ice_c_lb":              None,
        "parity_ratio":          None,
        "parity_ratio_physical": None,
        "signal":                0,
        "bias":                  "NEUTRAL",
        "description":           "Paridad etanol: sin datos CEPEA",
    }

    # --- CEPEA desde DB ---
    try:
        from sqlalchemy import text
        rows = session.execute(text("""
            SELECT DISTINCT ON (series_name)
                   series_name, price_usd, price_date
            FROM cepea_prices
            WHERE series_name IN ('hydrous_paulinia_usd_m3', 'crystal_sugar_usd_bag50kg')
            ORDER BY series_name, price_date DESC
        """)).fetchall()
        cepea = {r[0]: {"price": float(r[1]), "date": str(r[2])} for r in rows if r[1]}
    except Exception as e:
        logger.warning("ethanol_parity: error leyendo DB: %s", e)
        cepea = {}

    hydrous = cepea.get("hydrous_paulinia_usd_m3")
    crystal = cepea.get("crystal_sugar_usd_bag50kg")

    if not hydrous:
        result["description"] = "Paridad etanol: sin datos CEPEA hydrous_paulinia"
        return result

    hydrous_usd_m3 = hydrous["price"]
    ethanol_usd_ton = hydrous_usd_m3 / ATR_FACTOR  # US$/ton azúcar equivalente

    result["hydrous_usd_m3"]  = round(hydrous_usd_m3, 2)
    result["hydrous_date"]    = hydrous["date"]
    result["ethanol_usd_ton"] = round(ethanol_usd_ton, 2)

    if crystal:
        crystal_usd_bag = crystal["price"]
        crystal_usd_ton = crystal_usd_bag * (1000 / BAG_KG)   # bag 50kg → US$/ton
        result["crystal_usd_ton"] = round(crystal_usd_ton, 2)
        result["crystal_date"]    = crystal["date"]

        # Paridad etanol vs azúcar físico brasileño
        if crystal_usd_ton > 0:
            result["parity_ratio_physical"] = round(ethanol_usd_ton / crystal_usd_ton, 4)

    # --- Precio ICE No.11 ---
    if ice_price_c_lb is None:
        ice_price_c_lb = _get_ice_price_yf()

    if ice_price_c_lb and ice_price_c_lb > 0:
        ice_usd_ton = (ice_price_c_lb / 100) * LBS_PER_TON
        result["ice_usd_ton"] = round(ice_usd_ton, 2)
        result["ice_c_lb"]    = round(ice_price_c_lb, 4)

        parity_ratio = ethanol_usd_ton / ice_usd_ton if ice_usd_ton > 0 else None
        result["parity_ratio"] = round(parity_ratio, 4) if parity_ratio else None

        if parity_ratio is not None:
            if parity_ratio >= PARITY_BULLISH:
                result["signal"] = 1
                result["bias"]   = "LONG"
                result["description"] = (
                    f"Paridad etanol/azúcar ICE = {parity_ratio:.3f} "
                    f"(etanol {(parity_ratio-1)*100:+.1f}% vs ICE) "
                    f"→ mills prefieren ETANOL → menos azúcar → LONG SB"
                )
            elif parity_ratio <= PARITY_BEARISH:
                result["signal"] = -1
                result["bias"]   = "SHORT"
                result["description"] = (
                    f"Paridad etanol/azúcar ICE = {parity_ratio:.3f} "
                    f"(azúcar {(1-parity_ratio)*100:+.1f}% vs etanol) "
                    f"→ mills prefieren AZÚCAR → más oferta → SHORT SB"
                )
            else:
                result["signal"] = 0
                result["bias"]   = "NEUTRAL"
                result["description"] = (
                    f"Paridad etanol/azúcar ICE = {parity_ratio:.3f} "
                    f"({(parity_ratio-1)*100:+.1f}% vs ICE) — zona neutral [0.95–1.05]"
                )
    else:
        # Sin precio ICE, usamos azúcar físico como referencia
        phys = result.get("parity_ratio_physical")
        if phys is not None:
            if phys >= PARITY_BULLISH:
                result["signal"] = 1; result["bias"] = "LONG"
                result["description"] = (
                    f"Paridad etanol/azúcar físico = {phys:.3f} "
                    f"→ mills prefieren ETANOL → LONG SB (ref. física)"
                )
            elif phys <= PARITY_BEARISH:
                result["signal"] = -1; result["bias"] = "SHORT"
                result["description"] = (
                    f"Paridad etanol/azúcar físico = {phys:.3f} "
                    f"→ mills prefieren AZÚCAR → SHORT SB (ref. física)"
                )
            else:
                result["description"] = (
                    f"Paridad etanol/azúcar físico = {phys:.3f} — neutral (sin precio ICE)"
                )

    return result


def _get_ice_price_yf() -> Optional[float]:
    """Precio último ICE No.11 continuo de yfinance (SB=F)."""
    try:
        import yfinance as yf
        df = yf.download("SB=F", period="5d", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or len(df) < 1:
            return None
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        return float(df["Close"].iloc[-1])
    except Exception as e:
        logger.warning("ethanol_parity yf SB=F: %s", e)
        return None
