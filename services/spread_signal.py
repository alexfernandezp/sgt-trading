"""
Calendar Spread & Term Structure — ICE No.11 Sugar.

Señal: estructura de plazos de los vencimientos SB como proxy de tensión de inventario físico.

Lógica:
  Backwardation (nearby > deferred) → escasez spot → señal LONG
  Contango > full carry            → exceso oferta, mercado paga almacenamiento → señal SHORT
  Contango ≈ full carry            → mercado equilibrado → NEUTRAL

Full carry estimado (mensual, en c/lb):
  SOFR (~5%) × precio / 1200  +  storage ICE Rule 11.20 (~$0.27/t/mes → ~0.012 c/lb/mes)
  ≈ 0.06-0.12 c/lb por mes dependiendo del nivel de precio

Vencimientos usados (por disponibilidad actual):
  SBN26 (Jul 26), SBV26 (Oct 26), SBH27 (Mar 27), SBK27 (May 27), SBN27 (Jul 27)

Spreads calculados:
  front_spread     : SBN26 - SBV26  (3 meses)
  front2_spread    : SBV26 - SBH27  (5 meses)
  mid_spread       : SBH27 - SBK27  (2 meses)
  deferred_spread  : SBK27 - SBN27  (2 meses)
"""
import logging
from datetime import date
from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)

# Yahoo Finance tickers para cada contrato
_YF_TICKERS = {
    "SBN26": "SBN26.NYB",
    "SBV26": "SBV26.NYB",
    "SBH27": "SBH27.NYB",
    "SBK27": "SBK27.NYB",
    "SBN27": "SBN27.NYB",
    "SBV27": "SBV27.NYB",
}


def _fetch_live_prices() -> dict[str, float]:
    """
    Descarga el precio más reciente de cada contrato de la curva via yfinance.
    Retorna {contract: last_close}. Silencia errores por contrato.
    """
    try:
        import yfinance as yf
        tickers = list(_YF_TICKERS.values())
        df = yf.download(tickers, period="2d", progress=False, auto_adjust=True)
        if df.empty:
            return {}
        closes = df["Close"] if "Close" in df.columns else df.xs("Close", axis=1, level=0)
        result = {}
        for contract, ticker in _YF_TICKERS.items():
            col = closes[ticker] if ticker in closes.columns else None
            if col is not None:
                last = col.dropna()
                if not last.empty:
                    result[contract] = round(float(last.iloc[-1]), 4)
        return result
    except Exception as exc:
        logger.warning("yfinance live fetch failed: %s", exc)
        return {}

SOFR_APPROX        = 0.045     # tasa libre de riesgo anual aproximada
STORAGE_CPP_MONTH  = 0.012     # c/lb por mes (ICE Rule 11.20 ~$0.27/t/mes)
QUALITY_CPP_MONTH  = 0.003     # deterioro de calidad ~$0.07/t/mes

# Contratos y número de meses entre cada par
_SPREAD_PAIRS = [
    ("SBN26", "SBV26", 3,  "front"),
    ("SBV26", "SBH27", 5,  "front2"),
    ("SBH27", "SBK27", 2,  "mid"),
    ("SBK27", "SBN27", 2,  "back"),
    ("SBN27", "SBV27", 3,  "back2"),
]

# Vencimientos en orden para detectar forma de la curva
_TERM_STRUCTURE_CONTRACTS = ["SBN26", "SBV26", "SBH27", "SBK27", "SBN27", "SBV27"]


def _full_carry_cpp_per_month(price_level: float) -> float:
    """Full carry en c/lb por mes dado el precio actual del contrato front."""
    financing = SOFR_APPROX * price_level / 12
    return round(financing + STORAGE_CPP_MONTH + QUALITY_CPP_MONTH, 4)


def compute_term_structure(session: Session) -> dict:
    """
    Calcula spreads y estructura de plazos completa.

    Precios: yfinance live (2d) como fuente primaria → DB como fallback por contrato.
    Returns dict con spreads individuales, carry implícito, régimen de la curva,
    y señales direccionales.
    """
    # 1. Fetch live desde Yahoo Finance
    live = _fetch_live_prices()

    # 2. Para contratos sin dato live, usar último registro en DB
    prices = {}
    for contract in _TERM_STRUCTURE_CONTRACTS:
        if contract in live:
            prices[contract] = {"close": live[contract], "date": date.today(), "source": "yfinance_live"}
        else:
            row = session.execute(text(
                "SELECT close, date FROM price_history "
                "WHERE instrument = :c ORDER BY date DESC LIMIT 1"
            ), {"c": contract}).fetchone()
            if row:
                prices[contract] = {"close": float(row[0]), "date": row[1], "source": "db_fallback"}

    if live:
        logger.info("term_structure: %d live / %d db-fallback",
                    sum(1 for c in _TERM_STRUCTURE_CONTRACTS if c in live),
                    sum(1 for c in _TERM_STRUCTURE_CONTRACTS if c not in live and c in prices))

    if len(prices) < 2:
        return {"regime": "INSUFFICIENT_DATA", "signal_long": 0, "signal_short": 0}

    # Precio front para calcular carry
    front_price = prices.get("SBN26", prices.get("SBV26", {})).get("close", 15.0)
    carry_month = _full_carry_cpp_per_month(front_price)

    # Calcular spreads y comparar con carry esperado
    spreads = {}
    carry_ratios = {}

    for near, far, months, label in _SPREAD_PAIRS:
        if near not in prices or far not in prices:
            continue
        spread = prices[near]["close"] - prices[far]["close"]   # positivo = backwardation
        fair_carry = carry_month * months                        # carry esperado (siempre positivo en contango)
        # carry_ratio > 1 → spread supera full carry (super-contango, bearish)
        # carry_ratio < 0 → backwardation (bullish)
        # 0 < carry_ratio < 1 → contango pero bajo full carry (neutral)
        carry_ratio = (-spread / fair_carry) if fair_carry > 0 else 0
        spreads[f"{label}_spread"] = round(spread, 4)
        carry_ratios[f"{label}_carry_ratio"] = round(carry_ratio, 2)

    # Régimen de la curva completa
    front_spread = spreads.get("front_spread")    # SBN26 - SBV26
    f2_spread    = spreads.get("front2_spread")   # SBV26 - SBH27
    front_ratio  = carry_ratios.get("front_carry_ratio", 0)
    f2_ratio     = carry_ratios.get("front2_carry_ratio", 0)

    # Clasificación del régimen
    if front_spread is not None and front_spread > 0:
        regime = "BACKWARDATION"        # nearby > deferred → escasez físico
        sig_l, sig_s = 1, 0
    elif front_ratio is not None and front_ratio > 1.5:
        regime = "SUPER_CONTANGO"       # spread > full carry × 1.5 → exceso oferta extremo
        sig_l, sig_s = 0, 1
    elif front_ratio is not None and front_ratio > 1.0:
        regime = "FULL_CARRY_CONTANGO"  # spread ≈ full carry → mercado normal, supply ok
        sig_l, sig_s = 0, 1
    elif front_ratio is not None and front_ratio < 0.5 and front_spread is not None and front_spread < 0:
        regime = "WEAK_CONTANGO"        # contango menor que carry → tensión latente
        sig_l, sig_s = 1, 0
    else:
        regime = "NEUTRAL_CONTANGO"
        sig_l, sig_s = 0, 0

    # Estadísticas robustas del front spread vs historia (ventana rolling 52 semanas)
    from services.stats_utils import robust_stats
    hist_spreads = _get_historical_front_spreads(session)
    rs_front: dict = {}
    if hist_spreads and front_spread is not None:
        rs_front = robust_stats(hist_spreads, front_spread)

    # Lista ordenada de precios para visualización de la curva
    curve = {c: prices[c]["close"] for c in _TERM_STRUCTURE_CONTRACTS if c in prices}

    return {
        "term_structure":       curve,
        "carry_month_cpp":      round(carry_month, 4),
        "front_price":          round(front_price, 4),
        **spreads,
        **carry_ratios,
        "front_modified_z_52w": rs_front.get("modified_z"),
        "front_pct_52w":        rs_front.get("percentile_rank"),
        "front_conviction":     rs_front.get("conviction"),
        "regime":               regime,
        "signal_long":          sig_l,
        "signal_short":         sig_s,
    }


def _get_historical_front_spreads(session: Session, weeks: int = 52) -> list[float]:
    """
    Obtiene historia de spreads SBN/SBV para Z-score.
    Usa price_history de ambos contratos con fechas solapadas.
    """
    rows = session.execute(text("""
        SELECT n.close - v.close AS spread
        FROM price_history n
        JOIN price_history v ON n.date = v.date AND v.instrument = 'SBV26'
        WHERE n.instrument = 'SBN26'
        ORDER BY n.date DESC
        LIMIT :w
    """), {"w": weeks}).fetchall()
    return [float(r[0]) for r in rows if r[0] is not None]


def score_spread(session: Session, direction: str) -> tuple[int, dict]:
    """
    Interfaz para scoring.py — retorna (score 0/1, ctx_dict).
    """
    ctx = compute_term_structure(session)
    if "INSUFFICIENT_DATA" in ctx.get("regime", ""):
        return None, ctx
    sig = ctx["signal_long"] if direction.upper() == "LONG" else ctx["signal_short"]
    return sig, ctx
