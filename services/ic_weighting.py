"""
IC Weighting — calcula el Information Coefficient (IC) rolling por señal
y produce un macro score ponderado en lugar del equal-weight actual.

IC = Spearman rank correlation entre (direction, fwd_ret_10d) en ventana rolling.

Señales sin suficiente historia (< min_obs días con retorno relleno) reciben
peso 1.0 por defecto (equal-weight hasta que acumulen historia).

Resultado:
  weights: {signal_name: ic}   — IC raw (puede ser negativo si la señal es inversamente predictiva)
  weighted_score: float         — suma de (direction × ic) para señales macro
  n_signals_calibrated: int     — cuántas señales tienen IC calculado con historia real
"""
import logging
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Señales macro que componen el score ponderado (equivalentes al macro_score actual)
MACRO_SIGNAL_NAMES = [
    "macro_brl_vs_ma20",
    "macro_brent_1d_chg",
    "macro_corr_brent_sb",
    "macro_parity_ratio",
    "macro_enso_oni",
    "macro_climate_deficit90",
    "macro_carry_ratio",
    "macro_comex_yoy",
    "macro_fires_signal",
    "macro_conab_revision",
    "macro_harvest_pace",
    "macro_crop_stress",
    "macro_rainfall_spi",
]

# Señales COT/spread que pueden calibrarse también
COT_SIGNAL_NAMES = [
    "cot_spec_pct_alltime",
    "cot_spec_pct_3m",
    "cot_spec_change_1w",
    "cot_spec_change_4w",
    "cot_comm_vs_13w_mean",
    "spread_b1_sbn_sbv",
    "price_b2_z26w",
]

FUNDAMENTAL_SIGNAL_NAMES = [
    "fundamental_a4_composite",
    "fundamental_a5_composite",
]


def compute_ic_weights(session, window_days: int = 180,
                       min_obs: int = 30, fwd_horizon: str = "10d") -> dict:
    """
    Calcula IC rolling (Spearman) para cada señal en la ventana especificada.

    Args:
        window_days: días hacia atrás a considerar (default 180 = ~6 meses)
        min_obs: mínimo de observaciones con fwd_ret relleno para calcular IC
        fwd_horizon: "5d", "10d" o "20d"

    Returns:
        dict con keys:
          "weights": {signal_name: ic_value}   (float, puede ser None si sin historia)
          "n_calibrated": int
          "n_equal_weight": int
          "coverage_days": int   (días de historia disponibles)
    """
    try:
        from scipy.stats import spearmanr
        import numpy as np
    except ImportError:
        logger.warning("ic_weighting: scipy/numpy no disponible — usando equal weights")
        return {"weights": {}, "n_calibrated": 0, "n_equal_weight": 0, "coverage_days": 0}

    from models.market_data import SignalDailyLog
    from sqlalchemy import select, and_

    col_attr = {"5d": "fwd_ret_5d", "10d": "fwd_ret_10d", "20d": "fwd_ret_20d"}.get(fwd_horizon, "fwd_ret_10d")
    cutoff = date.today() - timedelta(days=window_days)

    rows = session.execute(
        select(
            SignalDailyLog.signal_name,
            SignalDailyLog.direction,
            getattr(SignalDailyLog, col_attr),
        )
        .where(and_(
            SignalDailyLog.date >= cutoff,
            getattr(SignalDailyLog, col_attr).isnot(None),
            SignalDailyLog.direction.isnot(None),
        ))
        .order_by(SignalDailyLog.date)
    ).fetchall()

    if not rows:
        return {"weights": {}, "n_calibrated": 0, "n_equal_weight": 0, "coverage_days": 0}

    # Agrupar por señal con forward fill de gaps
    # Un gap ocurre cuando una señal infrecuente (CONAB, GEE) no tiene dato ese día.
    # El carry forward ya lo hace signal_logger; esto es una red de seguridad adicional.
    from collections import defaultdict

    # Índice: {(signal_name, date): (direction, fwd_ret)}
    raw_by_signal_date = defaultdict(dict)
    all_dates_set = set()
    for signal_name, direction, fwd_ret in rows:
        # Nota: rows es (signal_name, direction, fwd_ret_Nd) — date no está en SELECT
        # Recuperar date requires a different query — simplificamos agrupando solo por señal
        raw_by_signal_date[signal_name]  # touch

    # Re-query con fecha para hacer forward fill correcto
    rows_with_date = session.execute(
        select(
            SignalDailyLog.date,
            SignalDailyLog.signal_name,
            SignalDailyLog.direction,
            getattr(SignalDailyLog, col_attr),
        )
        .where(and_(
            SignalDailyLog.date >= cutoff,
            getattr(SignalDailyLog, col_attr).isnot(None),
            SignalDailyLog.direction.isnot(None),
        ))
        .order_by(SignalDailyLog.date)
    ).fetchall()

    # Construir series por señal con forward fill de gaps entre observaciones reales
    signal_series: dict[str, list[tuple]] = defaultdict(list)
    all_dates_set = sorted(set(r[0] for r in rows_with_date))

    last_known: dict[str, tuple] = {}
    for d in all_dates_set:
        day_data = {r[1]: (r[2], float(r[3])) for r in rows_with_date if r[0] == d}
        for sig_name in (set(last_known) | set(day_data)):
            if sig_name in day_data:
                last_known[sig_name] = day_data[sig_name]
            # Si no hay dato hoy pero hay último conocido: forward fill silencioso
            if sig_name in last_known:
                signal_series[sig_name].append(last_known[sig_name])

    grouped = signal_series

    weights = {}
    n_calibrated = 0
    n_equal = 0

    for signal_name, pairs in grouped.items():
        if len(pairs) < min_obs:
            n_equal += 1
            continue

        directions = np.array([p[0] for p in pairs], dtype=float)
        fwd_rets   = np.array([p[1] for p in pairs], dtype=float)

        # Spearman IC
        if np.std(directions) == 0 or np.std(fwd_rets) == 0:
            weights[signal_name] = 0.0
            continue

        ic, pval = spearmanr(directions, fwd_rets)
        if np.isnan(ic):
            weights[signal_name] = 0.0
        else:
            # Clip a [-0.5, 0.5] para evitar pesos extremos
            weights[signal_name] = float(np.clip(ic, -0.5, 0.5))
            n_calibrated += 1

    # Cobertura de días
    all_dates = session.execute(
        select(SignalDailyLog.date)
        .where(SignalDailyLog.date >= cutoff)
        .distinct()
        .order_by(SignalDailyLog.date)
    ).scalars().all()
    coverage_days = len(all_dates)

    return {
        "weights": weights,
        "n_calibrated": n_calibrated,
        "n_equal_weight": n_equal,
        "coverage_days": coverage_days,
    }


def compute_weighted_macro_score(session, macro: Optional[dict],
                                 window_days: int = 180) -> dict:
    """
    Reemplaza el macro_score equal-weight con uno ponderado por IC.

    Cuando una señal no tiene IC calibrado (< min_obs), usa peso 1.0.
    El score resultante ya no está en escala fija [-13,+13] — está en unidades IC.
    Se normaliza a [-13,+13] para compatibilidad con el display actual.

    Returns dict con:
      "weighted_score": float (normalizado)
      "raw_score": float (sin normalizar)
      "weights": {signal_name: ic}
      "signal_contributions": {signal_name: direction * weight}
      "n_calibrated": int
      "method": "ic_weighted" | "equal_weight"
    """
    ic_data = compute_ic_weights(session, window_days=window_days)
    weights  = ic_data["weights"]
    n_cal    = ic_data["n_calibrated"]
    coverage = ic_data["coverage_days"]

    mac = macro or {}

    # Extraer direcciones actuales de cada señal macro
    brl    = mac.get("brl",     {}) or {}
    brent  = mac.get("brent",   {}) or {}
    corr   = mac.get("corr",    {}) or {}
    parity = mac.get("parity",  {}) or {}
    enso   = mac.get("enso",    {}) or {}
    clim   = mac.get("climate", {}) or {}
    carry  = mac.get("carry",   {}) or {}
    comex  = mac.get("comex",   {}) or {}
    fire   = mac.get("fire",    {}) or {}
    conab  = mac.get("conab",   {}) or {}
    hp     = mac.get("harvest_pace", {}) or {}
    cs     = mac.get("crop_stress",  {}) or {}
    rf     = mac.get("rainfall",     {}) or {}

    def _b(bias_str):
        if not bias_str:
            return 0
        b = (bias_str or "").upper()
        if "LONG" in b or "BULL" in b:
            return +1
        if "SHORT" in b or "BEAR" in b or "CONTRA" in b:
            return -1
        return 0

    # signal_name → current direction
    current_directions = {
        "macro_brl_vs_ma20":     _b(brl.get("bias")),
        "macro_brent_1d_chg":    _b(brent.get("bias")),
        "macro_corr_brent_sb":   _b(corr.get("bias")),
        "macro_parity_ratio":    _b(parity.get("bias")),
        "macro_enso_oni":        _b(enso.get("bias")),
        "macro_climate_deficit90": _b(clim.get("bias")),
        "macro_carry_ratio":     _b(carry.get("bias")),
        "macro_comex_yoy":       _b(comex.get("bias")),
        "macro_fires_signal":    _b(fire.get("bias")),
        "macro_conab_revision":  _b(conab.get("bias")),
        "macro_harvest_pace":    _b(hp.get("bias")),
        "macro_crop_stress":     _b(cs.get("bias")),
        "macro_rainfall_spi":    _b(rf.get("bias")),
    }

    raw_score = 0.0
    contributions = {}
    for name in MACRO_SIGNAL_NAMES:
        direction = current_directions.get(name, 0)
        ic = weights.get(name)
        w = ic if ic is not None else 1.0  # equal-weight si sin historia
        contrib = direction * w
        raw_score += contrib
        contributions[name] = round(contrib, 3)

    # Normalizar a [-13, +13] para compatibilidad display
    # Max posible con IC=0.5: 13 × 0.5 = 6.5 → normalizamos por max_possible
    max_possible = len(MACRO_SIGNAL_NAMES) * 0.5 if n_cal > 0 else len(MACRO_SIGNAL_NAMES)
    if max_possible > 0:
        weighted_score_norm = raw_score / max_possible * len(MACRO_SIGNAL_NAMES)
    else:
        weighted_score_norm = raw_score

    method = "ic_weighted" if n_cal >= 5 else "equal_weight"

    return {
        "weighted_score":    round(weighted_score_norm, 2),
        "raw_score":         round(raw_score, 3),
        "weights":           weights,
        "signal_contributions": contributions,
        "n_calibrated":      n_cal,
        "coverage_days":     coverage,
        "method":            method,
    }


def format_ic_summary(ic_result: dict) -> str:
    """
    Genera una línea de resumen del IC weighting para mostrar en score_today.
    """
    n_cal = ic_result.get("n_calibrated", 0)
    n_eq  = ic_result.get("n_equal_weight", 0)
    cov   = ic_result.get("coverage_days", 0)
    meth  = ic_result.get("method", "equal_weight")
    score = ic_result.get("weighted_score", 0)

    if meth == "ic_weighted":
        top = sorted(
            [(k, v) for k, v in ic_result.get("weights", {}).items() if v is not None],
            key=lambda x: abs(x[1]), reverse=True
        )[:3]
        top_str = "  ".join("%s=%.2f" % (k.replace("macro_", ""), v) for k, v in top)
        return ("  IC Weighting: %d señales calibradas / %d cov=%dd  "
                "score_IC=%.2f  top: %s") % (n_cal, n_cal + n_eq, cov, score, top_str)
    else:
        return ("  IC Weighting: acumulando historia (%d días, %d señales calibradas) "
                "— equal weight activo") % (cov, n_cal)
