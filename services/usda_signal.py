"""
Señal USDA WASDE — balance global azúcar (Stocks-to-Use ratio).

Thresholds históricos para azúcar centrifugado (STU mundial):
  < 28%  → STRONG LONG   (escasez crítica)
  28-32% → LONG          (mercado ajustado)
  32-38% → NEUTRAL       (zona equilibrada)
  38-42% → SHORT         (excedente moderado)
  > 42%  → STRONG SHORT  (surplus evidente)

Fuente: USDA FAS PSD via ingestion/usda_psd.py
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Thresholds stocks-to-use (%)
STU_STRONG_LONG  = 28.0
STU_LONG         = 32.0
STU_SHORT        = 38.0
STU_STRONG_SHORT = 42.0

# Peso de cada país en la producción mundial (para breakdown)
_COUNTRY_NAMES = {
    "WB": "Mundo",
    "BR": "Brasil",
    "IN": "India",
    "TH": "Tailandia",
    "EU": "UE",
    "AU": "Australia",
    "CN": "China",
    "US": "EEUU",
    "PK": "Pakistán",
    "MX": "México",
}


def compute_usda_signal(session, marketing_year: Optional[int] = None) -> dict:
    """
    Calcula señal USDA WASDE basada en el ratio Stocks-to-Use mundial.

    Returns dict con:
      signal         : +1 / 0 / -1
      bias           : "LONG" / "NEUTRAL" / "SHORT"
      description    : texto explicativo
      stu_pct        : float (%)
      marketing_year : int
      production_mt  : float (Mt)
      consumption_mt : float (Mt)
      ending_stocks_mt: float (Mt)
      country_production: list[dict]  (Brasil, India, Tailandia últimos 2 años)
      data_source    : "db" | "no_data"
    """
    try:
        from ingestion.usda_psd import get_world_balance, get_country_production
    except ImportError as e:
        logger.warning("usda_signal: import error: %s", e)
        return _no_data("import error: %s" % e)

    try:
        wb = get_world_balance(session, marketing_year)
    except Exception as e:
        logger.warning("usda_signal: get_world_balance error: %s", e)
        return _no_data("DB error: %s" % e)

    if not wb:
        return _no_data("sin datos en BD — ejecutar: py scripts/fetch_usda.py")

    stu       = wb.get("stocks_to_use_pct")
    prod      = wb.get("production_mt")
    cons      = wb.get("consumption_mt")
    end_s     = wb.get("ending_stocks_mt")
    beg_s     = wb.get("beginning_stocks_mt")
    exports   = wb.get("exports_mt")
    mkt_year  = wb.get("marketing_year")

    if stu is None:
        return _no_data("STU no calculable — datos parciales en BD")

    # Señal STU
    if stu < STU_STRONG_LONG:
        signal = 1; bias = "LONG"
        desc = ("USDA STU=%.1f%% (<%.0f%%) — escasez crítica global "
                "→ soporte estructural alcista") % (stu, STU_STRONG_LONG)
    elif stu < STU_LONG:
        signal = 1; bias = "LONG"
        desc = ("USDA STU=%.1f%% (%.0f-%.0f%%) — mercado ajustado "
                "→ sesgo alcista") % (stu, STU_STRONG_LONG, STU_LONG)
    elif stu < STU_SHORT:
        signal = 0; bias = "NEUTRAL"
        desc = ("USDA STU=%.1f%% (%.0f-%.0f%%) — balance equilibrado "
                "→ neutral") % (stu, STU_LONG, STU_SHORT)
    elif stu < STU_STRONG_SHORT:
        signal = -1; bias = "SHORT"
        desc = ("USDA STU=%.1f%% (%.0f-%.0f%%) — excedente moderado "
                "→ presión bajista") % (stu, STU_SHORT, STU_STRONG_SHORT)
    else:
        signal = -1; bias = "SHORT"
        desc = ("USDA STU=%.1f%% (>%.0f%%) — surplus evidente "
                "→ sesgo bajista fuerte") % (stu, STU_STRONG_SHORT)

    # Breakdown por país (últimos 2 años de producción)
    country_prod = {}
    for cc in ("BR", "IN", "TH", "EU", "AU"):
        try:
            rows = get_country_production(session, cc, n_years=2)
            if rows:
                country_prod[cc] = rows
        except Exception:
            pass

    return {
        "signal":           signal,
        "bias":             bias,
        "description":      desc,
        "stu_pct":          stu,
        "marketing_year":   mkt_year,
        "production_mt":    prod,
        "consumption_mt":   cons,
        "ending_stocks_mt": end_s,
        "beginning_stocks_mt": beg_s,
        "exports_mt":       exports,
        "country_production": country_prod,
        "data_source":      "db",
    }


def _no_data(reason: str) -> dict:
    return {
        "signal":    0,
        "bias":      "NEUTRAL",
        "description": "USDA WASDE: %s" % reason,
        "stu_pct":   None,
        "marketing_year": None,
        "production_mt": None,
        "consumption_mt": None,
        "ending_stocks_mt": None,
        "country_production": {},
        "data_source": "no_data",
    }
