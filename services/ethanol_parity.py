"""
Paridad etanol-azúcar usando precios físicos CEPEA/ESALQ.

Lógica:
  Los ingenios brasileños eligen entre producir azúcar o etanol
  según cuál genera más ingreso por tonelada de caña procesada.

  Factor de equivalencia Consecana-SP (estándar oficial Brasil):
    1 m³ etanol hidratado ≈ 1.4966 ton VHP azúcar equivalente

  Derivación del factor (rendimientos típicos São Paulo, UNICA/MAPA):
    Azúcar VHP   : ~130 kg / ton caña
    Etanol hidr. : ~87 L   / ton caña
    Factor = 130 kg / 87 L = 1.494 kg azúcar por litro = 1.494 ton/m³
    (Consecana-SP usa 1.4966, misma base, factor oficial)

  Expresión en c/lb (misma unidad que ICE No.11):
    ethanol_c_lb = hydrous_usd_m3 × 100 / (1.4966 × 2204.62)

  Verificación: 469.25 US$/m³ → 14.22 c/lb equivalente azúcar
  (históricamente el etanol cotiza muy cerca del ICE ~14-16 c/lb)

  Ratio de paridad (vs ICE No.11):
    parity_ratio = ethanol_c_lb / ice_c_lb
                 = ethanol_usd_ton / ice_usd_ton

  Interpretación:
    ratio > 1.05  → etanol +5% sobre ICE → mills prefieren etanol
                   → menos azúcar disponible → LONG SB
    ratio < 0.95  → ICE +5% sobre etanol → mills prefieren azúcar
                   → más oferta → SHORT SB
    0.95–1.05     → zona neutral (mezcla equilibrada)

Fuentes:
  Etanol:  CEPEA hydrous_paulinia_usd_m3 (diario, Paulínia SP)
  Azúcar físico: CEPEA crystal_sugar_usd_bag50kg (diario)
  ICE No.11: yfinance SB=F o DB price_history
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Factor Consecana-SP: 1 m³ etanol hidratado = 1.4966 ton VHP azúcar equiv
# Derivado de rendimientos típicos São Paulo: 130 kg azúcar / 87 L etanol por ton caña
# Fuente: Consecana-SP (https://www.consecana.com.br), UNICA/MAPA estadísticas
ATR_FACTOR  = 1.4966       # ton VHP azúcar equivalente por m³ etanol hidratado
BAG_KG      = 50           # bolsa CEPEA = 50 kg → 20 bolsas/ton
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
        "ethanol_c_lb":          None,   # etanol expresado en c/lb azúcar equivalente
        "spread_c_lb":           None,   # ethanol_c_lb − ice_c_lb (prima en c/lb)
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

    # Etanol en c/lb de azúcar equivalente (misma unidad que ICE No.11)
    # Formula: (usd_m3 / ATR_factor) * 100 cents / 2204.62 lbs_per_ton
    ethanol_c_lb = hydrous_usd_m3 * 100 / (ATR_FACTOR * LBS_PER_TON)

    result["hydrous_usd_m3"]  = round(hydrous_usd_m3, 2)
    result["hydrous_date"]    = hydrous["date"]
    result["ethanol_usd_ton"] = round(ethanol_usd_ton, 2)
    result["ethanol_c_lb"]    = round(ethanol_c_lb, 4)

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
        result["spread_c_lb"]  = round(ethanol_c_lb - ice_price_c_lb, 4) if ice_price_c_lb else None

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
