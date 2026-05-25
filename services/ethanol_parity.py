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

# Umbrales calibrados sobre distribución histórica 2010-2026 (N=3947 días)
# Media ratio: 0.9011  |  Mediana: 0.8858  |  Std: 0.1730
#
# P75 = 1.028 → cuartil superior de atractivo etanol (mills claramente prefieren etanol)
# P25 = 0.788 → cuartil inferior de atractivo etanol (mills claramente prefieren azúcar)
# Señal activa ~25% LONG + ~25% SHORT + ~50% neutral (distribución equilibrada)
#
# Umbrales viejos (0.95/1.05) eran SHORT el 64% del tiempo → mal calibrados
PARITY_BULLISH    = 1.028  # ratio > P75 → mills prefieren etanol → LONG SB
PARITY_BEARISH    = 0.788  # ratio < P25 → mills prefieren azúcar → SHORT SB
PARITY_HIST_MEAN  = 0.9011 # media histórica (para referencia en display)
PARITY_HIST_MED   = 0.8858 # mediana histórica


def _get_cepea_history(session, days: int = 70) -> "pd.Series":
    """Retorna Serie pandas (index=date, values=hydrous_usd_m3) últimos N días."""
    try:
        import pandas as pd
        from sqlalchemy import text
        from datetime import date, timedelta
        cutoff = date.today() - timedelta(days=days)
        rows = session.execute(text("""
            SELECT price_date, price_usd FROM cepea_prices
            WHERE series_name = 'hydrous_paulinia_usd_m3'
              AND price_date >= :cutoff
            ORDER BY price_date
        """), {"cutoff": cutoff}).fetchall()
        if not rows:
            return pd.Series(dtype=float)
        s = pd.Series(
            {r[0]: float(r[1]) for r in rows if r[1] is not None}
        )
        s.index = pd.to_datetime(s.index)
        return s
    except Exception as e:
        logger.warning("ethanol_parity _get_cepea_history: %s", e)
        import pandas as pd
        return pd.Series(dtype=float)


def _get_ice_history_yf(days: int = 70) -> "pd.Series":
    """Retorna Serie pandas (index=date, values=ice_c_lb) últimos N días de SB=F."""
    try:
        import pandas as pd
        import yfinance as yf
        df = yf.download("SB=F", period=f"{days}d", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or len(df) < 2:
            return pd.Series(dtype=float)
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        s = df["Close"].dropna()
        s.index = pd.to_datetime(s.index)
        return s.astype(float)
    except Exception as e:
        logger.warning("ethanol_parity _get_ice_history_yf: %s", e)
        import pandas as pd
        return pd.Series(dtype=float)


def _compute_trend(result: dict, session, current_ratio: float) -> None:
    """
    Calcula tendencia del ratio a 2w/4w/8w y escribe campos en result in-place.
    Usa ~10/20/40 días hábiles como proxy de 2/4/8 semanas.
    """
    try:
        import pandas as pd
        cepea_hist = _get_cepea_history(session, days=70)
        ice_hist   = _get_ice_history_yf(days=70)

        if cepea_hist.empty or ice_hist.empty:
            return

        # Alinear por fecha (join inner)
        df = pd.DataFrame({"hydrous": cepea_hist, "ice": ice_hist}).dropna()
        if len(df) < 12:
            return

        # Convertir hydrous a ratio usando mismos factores
        df["ratio"] = (df["hydrous"] * 100 / (ATR_FACTOR * LBS_PER_TON)) / df["ice"]

        # Valor del ratio N días hábiles atrás (aproximado como N-ésimo registro desde el final)
        def ratio_n_ago(n: int):
            """ratio n registros (días con datos) antes del último."""
            if len(df) <= n:
                return None
            return float(df["ratio"].iloc[-(n + 1)])

        r_2w = ratio_n_ago(10)  # ~2 semanas hábiles
        r_4w = ratio_n_ago(20)  # ~4 semanas hábiles
        r_8w = ratio_n_ago(40)  # ~8 semanas hábiles

        result["ratio_2w_ago"] = round(r_2w, 4) if r_2w is not None else None
        result["ratio_4w_ago"] = round(r_4w, 4) if r_4w is not None else None
        result["ratio_8w_ago"] = round(r_8w, 4) if r_8w is not None else None

        def pct_change(old):
            if old is None or old == 0:
                return None
            return round((current_ratio - old) / abs(old) * 100, 2)

        t2 = pct_change(r_2w)
        t4 = pct_change(r_4w)
        t8 = pct_change(r_8w)

        result["trend_2w"] = t2
        result["trend_4w"] = t4
        result["trend_8w"] = t8

        # Dirección dominante: usa tendencia de 4 semanas como referencia
        ref = t4 if t4 is not None else t2
        if ref is None:
            result["trend_direction"] = "→"
            result["trend_label"]     = "sin historial suficiente"
            return

        if ref > 2.0:
            result["trend_direction"] = "↑"
        elif ref < -2.0:
            result["trend_direction"] = "↓"
        else:
            result["trend_direction"] = "→"

        # Etiqueta descriptiva: aceleración vs desaceleración
        bull_now  = current_ratio >= PARITY_BULLISH
        bear_now  = current_ratio <= PARITY_BEARISH
        going_up  = ref > 2.0
        going_dn  = ref < -2.0

        if bull_now and going_up:
            label = "acelerando LONG"
        elif bull_now and going_dn:
            label = "LONG pero cediendo"
        elif bear_now and going_dn:
            label = "acelerando SHORT"
        elif bear_now and going_up:
            label = "SHORT pero recuperando"
        elif going_up:
            label = "mejorando hacia LONG"
        elif going_dn:
            label = "deteriorando hacia SHORT"
        else:
            label = "estable"

        result["trend_label"] = label

    except Exception as e:
        logger.warning("ethanol_parity _compute_trend: %s", e)


def compute_ethanol_parity(
    session,
    ice_price_c_lb: Optional[float] = None,
) -> dict:
    """
    Calcula la paridad etanol-azúcar usando los últimos precios CEPEA.
    Incluye tendencia del ratio a 2w / 4w / 8w.

    Args:
        session        : SQLAlchemy session con acceso a cepea_prices
        ice_price_c_lb : precio ICE No.11 en c/lb (si None se obtiene de yfinance)

    Returns dict con todos los campos de paridad más:
        trend_2w, trend_4w, trend_8w   : % cambio del ratio en esos períodos
        trend_direction                 : "↑" / "↓" / "→"
        ratio_2w_ago, ratio_4w_ago     : valores históricos del ratio
    """
    result = {
        "hydrous_usd_m3":        None,
        "hydrous_date":          None,
        "ethanol_usd_ton":       None,
        "ethanol_c_lb":          None,
        "spread_c_lb":           None,
        "crystal_usd_ton":       None,
        "crystal_date":          None,
        "ice_usd_ton":           None,
        "ice_c_lb":              None,
        "parity_ratio":          None,
        "parity_ratio_physical": None,
        # Tendencia del ratio
        "ratio_2w_ago":          None,
        "ratio_4w_ago":          None,
        "ratio_8w_ago":          None,
        "trend_2w":              None,   # % cambio últimas 2 semanas
        "trend_4w":              None,   # % cambio últimas 4 semanas
        "trend_8w":              None,   # % cambio últimas 8 semanas
        "trend_direction":       None,   # "↑" / "↓" / "→"
        "trend_label":           None,   # "acelerando LONG", "desacelerando", etc.
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
            _compute_trend(result, session, parity_ratio)

            # Percentil aproximado para display (referencia histórica)
            if parity_ratio >= 1.093:
                pct_str = ">P85"
            elif parity_ratio >= 1.028:
                pct_str = "P75-P85"
            elif parity_ratio >= 0.886:
                pct_str = "P50-P75"
            elif parity_ratio >= 0.788:
                pct_str = "P25-P50"
            elif parity_ratio >= 0.731:
                pct_str = "P15-P25"
            else:
                pct_str = "<P15"

            if parity_ratio >= PARITY_BULLISH:
                result["signal"] = 1
                result["bias"]   = "LONG"
                result["description"] = (
                    f"Paridad etanol/ICE = {parity_ratio:.3f} [{pct_str}] "
                    f"(cuartil superior histórico) "
                    f"→ mills prefieren ETANOL → menos azúcar → LONG SB"
                )
            elif parity_ratio <= PARITY_BEARISH:
                result["signal"] = -1
                result["bias"]   = "SHORT"
                result["description"] = (
                    f"Paridad etanol/ICE = {parity_ratio:.3f} [{pct_str}] "
                    f"(cuartil inferior histórico) "
                    f"→ mills prefieren AZÚCAR → más oferta → SHORT SB"
                )
            else:
                result["signal"] = 0
                result["bias"]   = "NEUTRAL"
                result["description"] = (
                    f"Paridad etanol/ICE = {parity_ratio:.3f} [{pct_str}] "
                    f"— zona neutral histórica [P25–P75: 0.788–1.028]"
                )
    else:
        # Sin precio ICE, usamos azúcar físico como referencia
        phys = result.get("parity_ratio_physical")
        if phys is not None:
            if phys >= PARITY_BULLISH:
                result["signal"] = 1; result["bias"] = "LONG"
                result["description"] = (
                    f"Paridad etanol/cristal = {phys:.3f} [>P75] "
                    f"→ mills prefieren ETANOL → LONG SB (ref. físico)"
                )
            elif phys <= PARITY_BEARISH:
                result["signal"] = -1; result["bias"] = "SHORT"
                result["description"] = (
                    f"Paridad etanol/cristal = {phys:.3f} [<P25] "
                    f"→ mills prefieren AZÚCAR → SHORT SB (ref. físico)"
                )
            else:
                result["description"] = (
                    f"Paridad etanol/cristal = {phys:.3f} — neutral (sin precio ICE)"
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
