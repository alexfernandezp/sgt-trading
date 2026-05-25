"""
Full Carry Dinámico — Sugar No.11 ICE (SBN26 / SBV26)

El Full Carry es el coste teórico completo de mantener azúcar físico
desde el vencimiento cercano hasta el lejano. Define el límite superior
matemático del contango en futuros — ningún spread racional puede superar
el coste de financiar y almacenar la posición física equivalente.

Componentes del Full Carry Dinámico:
─────────────────────────────────────────────────────────────────────
  1. Storage ICE (fija)
       $0.0202 / MT / día  (ICE Rule 11.20, almacén certificado)

  2. Seguro + deterioro (función del valor del cargo)
       cargo_value × 0.20% / año  ×  días/365
       A $350/ton: ~$0.17/ton para 91 días

  3. Financiación (SOFR + spread Brasil)
       cargo_value × (SOFR% + 2.0%) / 100  ×  días/365
       Lógica: ingenios y trading houses brasileños financian
       inventario en USD al coste SOFR + spread soberano Brasil (~200bps)
       Con SOFR=4.30%: tasa efectiva 6.30%  → ~$5.50/ton / 91d

  4. Prima logística indexada al Bunker Fuel
       base_logistics × (bunker_price / bunker_mean_usd_ton)
       Proxy: bunker_price ≈ Brent ($/bbl) × 6.5  (VLSFO ≈ 6.5× Brent)
       bunker_mean = $617/ton  (≡ Brent ~$95/bbl, media post-2020)
       base_logistics = $2.80/ton  (prima logística en condiciones normales)
       Lógica: bunker caro → operadores prefieren embarcar ahora →
               necesitan mayor prima forward para diferir → full carry sube

─────────────────────────────────────────────────────────────────────
Señal de trading:
  spread = SBV26 − SBN26  (positivo = contango)

  carry_ratio = spread / full_carry_dynamic

  spread < −0.05 c/lb   → BACKWARDATION → +1 LONG nearby
  carry_ratio > 0.85    → Near Full Carry → −1 SHORT (exceso oferta)
  else                  → 0 neutral

Indicador adelantado:
  carry_cost_30d_change_pct: si full carry se encarece >10% en 30 días
  pero el spread no sube → presión vendedora institucional oculta
  (comerciales no consiguen trasladar el coste al mercado → venden)
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

LBS_PER_TON = 2204.62

# Constantes del modelo
ICE_WAREHOUSE_RATE  = 0.0202   # $/MT/día — ICE Rule 11.20 (nunca cambia)
INSURANCE_RATE_PA   = 0.0020   # 0.20% anual sobre valor del cargo
COUNTRY_RISK_PCT    = 2.00     # spread Brasil EMBI+ proxy (bps/100), fijo
LOGISTICS_BASE_USD  = 2.80     # $/MT prima logística en bunker normal
BUNKER_MEAN_USD_TON = 617.0    # media VLSFO post-2020 (~Brent $95/bbl)
BRENT_TO_VLSFO      = 6.50     # factor conversión Brent $/bbl → VLSFO $/ton (aprox)

# Umbrales señal
BACKWARDATION_THRESHOLD = -0.05   # c/lb → backwardation significativa → LONG
NEAR_FULL_CARRY_RATIO   = 0.85    # ≥85% del full carry dinámico → SHORT


# ── Fuentes de datos ─────────────────────────────────────────────────────────

def _get_sofr() -> float:
    """SOFR actual de FRED (CSV). Fallback al configurado en config.py."""
    try:
        import pandas as pd
        from config import SOFR_DEFAULT_PCT
        df = pd.read_csv(
            "https://fred.stlouisfed.org/graph/fredgraph.csv?id=SOFR",
            parse_dates=["DATE"],
        ).dropna()
        if df.empty:
            return SOFR_DEFAULT_PCT
        return float(df["SOFR"].iloc[-1])
    except Exception as e:
        logger.debug("SOFR fetch: %s — usando default", e)
        try:
            from config import SOFR_DEFAULT_PCT
            return SOFR_DEFAULT_PCT
        except ImportError:
            return 4.30


def _get_brent() -> Optional[float]:
    """Último precio Brent de yfinance ($/bbl). Retorna None si no disponible."""
    try:
        import yfinance as yf
        df = yf.download("BZ=F", period="5d", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        return float(df["Close"].dropna().iloc[-1])
    except Exception as e:
        logger.debug("Brent fetch: %s", e)
        return None


def _get_spread_prices() -> Optional[dict]:
    """Descarga precios SBN26 y SBV26 de Yahoo Finance."""
    try:
        import yfinance as yf
        df = yf.download(["SBN26.NYB", "SBV26.NYB"], period="5d", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None

        if isinstance(df.columns, pd.MultiIndex if hasattr(df.columns, 'levels') else type(None)):
            try:
                close = df["Close"]
            except KeyError:
                import pandas as pd
                close = df.xs("Close", axis=1, level=0)
        else:
            import pandas as pd
            close = df["Close"] if "Close" in df.columns else df

        import pandas as pd
        if not isinstance(close, pd.DataFrame):
            return None

        close.columns = [str(c).replace(".NYB", "").replace("SBN26", "SBN26").replace("SBV26", "SBV26")
                         for c in close.columns]
        close.columns = [c.split(".")[0] for c in close.columns]

        if "SBN26" not in close.columns or "SBV26" not in close.columns:
            return None

        near     = float(close["SBN26"].dropna().iloc[-1])
        deferred = float(close["SBV26"].dropna().iloc[-1])
        return {
            "near_c_lb":     round(near, 4),
            "deferred_c_lb": round(deferred, 4),
            "spread_c_lb":   round(deferred - near, 4),
        }
    except Exception as e:
        logger.warning("spread_prices: %s", e)
        return None


# ── CarryCalculator ───────────────────────────────────────────────────────────

@dataclass
class CarryCalculator:
    """
    Calcula el Full Carry dinámico para ICE Sugar No.11.

    Inputs:
      near_price_c_lb   : precio contrato cercano (c/lb)
      sofr_pct          : SOFR actual (%, e.g. 4.30)
      brent_usd_bbl     : precio Brent ($/bbl), para indexar bunker
      storage_days      : días entre vencimientos (default 91)
      country_risk_pct  : spread país Brasil (%, default 2.00)

    Outputs via calculate(): dict con desglose completo
    """
    near_price_c_lb  : float
    sofr_pct         : float
    brent_usd_bbl    : Optional[float] = None
    storage_days     : int   = 91
    country_risk_pct : float = COUNTRY_RISK_PCT

    def _cargo_value_usd_ton(self) -> float:
        return self.near_price_c_lb * LBS_PER_TON / 100

    def storage_cost(self) -> float:
        """Componente 1: tasa almacén ICE certificado ($/ton)."""
        return ICE_WAREHOUSE_RATE * self.storage_days

    def insurance_cost(self) -> float:
        """Componente 2: seguro + deterioro sobre valor del cargo ($/ton)."""
        return self._cargo_value_usd_ton() * INSURANCE_RATE_PA * self.storage_days / 365

    def financing_cost(self) -> float:
        """Componente 3: SOFR + spread Brasil sobre valor del cargo ($/ton)."""
        total_rate = self.sofr_pct + self.country_risk_pct   # % anual
        return self._cargo_value_usd_ton() * total_rate / 100 * self.storage_days / 365

    def logistics_cost(self) -> float:
        """Componente 4: prima logística indexada al bunker ($/ton)."""
        if self.brent_usd_bbl is None:
            return LOGISTICS_BASE_USD   # sin datos bunker → usamos base
        bunker_est = self.brent_usd_bbl * BRENT_TO_VLSFO
        bunker_ratio = bunker_est / BUNKER_MEAN_USD_TON
        return LOGISTICS_BASE_USD * bunker_ratio

    def calculate(self) -> dict:
        """
        Devuelve dict con desglose completo del full carry dinámico.

        Campos:
          storage_usd_ton   : componente 1 ($/ton)
          insurance_usd_ton : componente 2 ($/ton)
          financing_usd_ton : componente 3 ($/ton)
          logistics_usd_ton : componente 4 ($/ton)
          total_usd_ton     : suma total ($/ton)
          full_carry_clb    : total en c/lb (misma unidad que el spread)
          effective_rate_pct: tasa efectiva anual total (%)
          bunker_est_usd_ton: estimación bunker VLSFO ($/ton)
          bunker_vs_mean_pct: bunker actual vs media histórica (%)
        """
        s = self.storage_cost()
        i = self.insurance_cost()
        f = self.financing_cost()
        l = self.logistics_cost()
        total_usd = s + i + f + l

        full_carry_clb = total_usd * 100 / LBS_PER_TON

        # Tasa efectiva anual implícita
        cargo = self._cargo_value_usd_ton()
        eff_rate = (total_usd / cargo / self.storage_days * 365 * 100) if cargo > 0 else 0

        # Bunker info
        bunker_est = (self.brent_usd_bbl * BRENT_TO_VLSFO
                      if self.brent_usd_bbl else BUNKER_MEAN_USD_TON)
        bunker_vs_mean = (bunker_est - BUNKER_MEAN_USD_TON) / BUNKER_MEAN_USD_TON * 100

        return {
            "storage_usd_ton":    round(s, 3),
            "insurance_usd_ton":  round(i, 3),
            "financing_usd_ton":  round(f, 3),
            "logistics_usd_ton":  round(l, 3),
            "total_usd_ton":      round(total_usd, 3),
            "full_carry_clb":     round(full_carry_clb, 4),
            "effective_rate_pct": round(eff_rate, 2),
            "bunker_est_usd_ton": round(bunker_est, 0),
            "bunker_vs_mean_pct": round(bunker_vs_mean, 1),
            "sofr_pct":           round(self.sofr_pct, 2),
            "country_risk_pct":   round(self.country_risk_pct, 2),
            "financing_rate_total": round(self.sofr_pct + self.country_risk_pct, 2),
            "storage_days":       self.storage_days,
        }


# ── Señal pública ─────────────────────────────────────────────────────────────

def compute_full_carry_signal(storage_days: int = 91) -> dict:
    """
    Calcula Full Carry dinámico y genera señal para el spread SBN26/SBV26.

    Returns dict con:
      near_c_lb, deferred_c_lb, spread_c_lb    — precios y spread observado
      carry                                     — dict completo del CarryCalculator
      carry_ratio                               — spread / full_carry_clb
      carry_pct_str                             — e.g. '118% del Full Carry'
      signal                                    — −1 / 0 / +1
      bias                                      — str
      description                               — str explicativo
      carry_cost_30d_change_pct                 — % cambio full carry vs 30d atrás
                                                  (None si no hay histórico)
    """
    result = {
        "near_c_lb":               None,
        "deferred_c_lb":           None,
        "spread_c_lb":             None,
        "carry":                   {},
        "carry_ratio":             None,
        "carry_pct_str":           None,
        "signal":                  0,
        "bias":                    "NEUTRAL",
        "description":             "Full Carry: sin datos de precios",
        "carry_cost_30d_change_pct": None,
    }

    # Precios spread
    prices = _get_spread_prices()
    if prices is None:
        return result

    near     = prices["near_c_lb"]
    deferred = prices["deferred_c_lb"]
    spread   = prices["spread_c_lb"]

    result["near_c_lb"]     = near
    result["deferred_c_lb"] = deferred
    result["spread_c_lb"]   = spread

    # Datos dinámicos
    sofr  = _get_sofr()
    brent = _get_brent()

    # Calcular full carry
    calc   = CarryCalculator(
        near_price_c_lb  = near,
        sofr_pct         = sofr,
        brent_usd_bbl    = brent,
        storage_days     = storage_days,
    )
    carry_data = calc.calculate()
    result["carry"] = carry_data

    full_carry = carry_data["full_carry_clb"]

    # Carry ratio
    if full_carry > 0:
        carry_ratio     = spread / full_carry
        carry_pct_str   = "%.0f%% del Full Carry" % (carry_ratio * 100)
    else:
        carry_ratio   = 0.0
        carry_pct_str = "N/D"

    result["carry_ratio"]   = round(carry_ratio, 3)
    result["carry_pct_str"] = carry_pct_str

    # Indicador adelantado: variación del full carry en 30 días
    # Aproximamos el full carry hace 30 días usando los mismos parámetros
    # pero con SOFR −0 (SOFR no cambia rápido; la variación viene de precio y bunker)
    # Por ahora registramos el valor actual y lo comparamos en futuras sesiones
    # (requiere histórico en DB — se implementará en Sprint 2)
    result["carry_cost_30d_change_pct"] = None

    # ── Señal ───────────────────────────────────────────────────────────────
    if spread < BACKWARDATION_THRESHOLD:
        signal = 1
        bias   = "LONG"
        bw_label = "FUERTE" if spread < -0.20 else ""
        desc = (
            f"BACKWARDATION {bw_label}: spread={spread:+.4f}c/lb "
            f"→ demanda física > oferta inmediata → LONG nearby SBN26"
            f"  [Full Carry dinámico={full_carry:.4f}c/lb, "
            f"SOFR={sofr:.2f}%+{COUNTRY_RISK_PCT:.0f}%BR, "
            f"Bunker≈${carry_data['bunker_est_usd_ton']:.0f}/t]"
        )
    elif carry_ratio >= NEAR_FULL_CARRY_RATIO:
        signal = -1
        bias   = "SHORT"
        pressure = ""
        if carry_data.get("bunker_vs_mean_pct", 0) > 15:
            pressure = "  [!] Bunker +%.0f%% vs media → Full Carry elevado → presión vendedora real" % carry_data["bunker_vs_mean_pct"]
        desc = (
            f"Near Full Carry: spread={spread:+.4f}c/lb = {carry_pct_str} "
            f"(dinámico={full_carry:.4f}c/lb | "
            f"storage={carry_data['storage_usd_ton']:.2f} "
            f"+ seguro={carry_data['insurance_usd_ton']:.2f} "
            f"+ financ.={carry_data['financing_usd_ton']:.2f} "
            f"+ logist.={carry_data['logistics_usd_ton']:.2f} $/ton)"
            f"  SOFR={sofr:.2f}%+{COUNTRY_RISK_PCT:.0f}%BR={carry_data['financing_rate_total']:.2f}%"
            f"{pressure}"
            f"  → abundancia estructural → SHORT SB"
        )
    else:
        signal = 0
        bias   = "NEUTRAL"
        desc = (
            f"Contango normal: spread={spread:+.4f}c/lb = {carry_pct_str} "
            f"(full carry dinámico={full_carry:.4f}c/lb, "
            f"SOFR={sofr:.2f}%+{COUNTRY_RISK_PCT:.0f}%BR, "
            f"Bunker≈${carry_data['bunker_est_usd_ton']:.0f}/t) — neutral"
        )

    result["signal"]      = signal
    result["bias"]        = bias
    result["description"] = desc
    return result
