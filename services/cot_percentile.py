"""
COT Extreme Percentile Model — Managed Money (CFTC Disaggregated).

Señal primaria: percentil de mm_net dentro de ventana rolling 3 años (156 semanas).
Managed money = hedge funds + CTAs — el grupo que se equivoca en los extremos.

Lógica contrarian documentada (Negrini de Mattos & Correa, SSRN 4651233):
  Extremo largo (>90th pct 3yr)  → crowding insostenible → mean reversion bajista
  Capitulación (<10th pct 3yr)   → exceso de cortos → squeeze/rebote alcista

Thresholds:
  EXTREME_LONG  : pct_3yr >= 90  → SHORT alta convicción
  CROWDED_LONG  : pct_3yr >= 75  → SHORT moderado
  NEUTRAL       : 25 < pct_3yr < 75
  DEPRESSED     : pct_3yr <= 25  → LONG moderado
  EXTREME_SHORT : pct_3yr <= 10  → LONG alta convicción
"""
import logging
from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)

WINDOW_WEEKS   = 156   # 3 años
EXTREME_HIGH   = 90
CROWDED_HIGH   = 75
CROWDED_LOW    = 25
EXTREME_LOW    = 10


def compute_mm_percentile(session: Session) -> dict:
    """
    Calcula el percentil de mm_net (managed money) en ventana rolling 3 años.

    Returns dict con:
      mm_net, mm_pct_3yr, mm_pct_1yr, mm_pct_alltime,
      mm_trend_4wk, mm_change_1wk, mm_change_4wk,
      regime, signal_long (0/1), signal_short (0/1)
    """
    rows = session.execute(text(
        "SELECT mm_net FROM cot_data "
        "WHERE mm_net IS NOT NULL "
        "ORDER BY report_date DESC "
        f"LIMIT {WINDOW_WEEKS + 10}"
    )).fetchall()

    if len(rows) < 8:
        return {"regime": "INSUFFICIENT_DATA", "signal_long": 0, "signal_short": 0}

    from services.stats_utils import robust_stats

    vals_3yr = [float(r[0]) for r in rows[:WINDOW_WEEKS]]
    current  = vals_3yr[0]
    n_3yr    = len(vals_3yr)

    # Percentil 3 años (ventana primaria) — distribution-free, ya es robusto
    pct_3yr = sum(1 for v in vals_3yr if v <= current) / n_3yr * 100

    # Modified Z-score sobre la misma ventana rolling 3 años (MAD-based, fat-tail safe)
    # Complementa el percentile: detecta anomalías de corto plazo dentro del régimen actual
    rs_3yr       = robust_stats(vals_3yr[1:], current)  # excluye current para evitar autocorrelación
    mm_modified_z = rs_3yr.get("modified_z")

    # Percentil 52 semanas (contexto anual)
    vals_1yr = vals_3yr[:min(52, n_3yr)]
    pct_1yr  = sum(1 for v in vals_1yr if v <= current) / len(vals_1yr) * 100

    # Percentil all-time (todos los datos en DB — hasta 10 años)
    all_rows = session.execute(text(
        "SELECT mm_net FROM cot_data WHERE mm_net IS NOT NULL ORDER BY report_date DESC"
    )).fetchall()
    all_vals    = [float(r[0]) for r in all_rows]
    pct_alltime = sum(1 for v in all_vals if v <= current) / len(all_vals) * 100

    # Tendencia 4 semanas
    ma4_now  = sum(vals_3yr[:4]) / 4
    ma4_prev = sum(vals_3yr[1:5]) / 4
    trend_4wk = ma4_now - ma4_prev

    change_1wk = vals_3yr[0] - vals_3yr[1] if n_3yr >= 2 else 0.0
    change_4wk = vals_3yr[0] - vals_3yr[min(4, n_3yr - 1)]

    # Clasificación de régimen basada en percentil 3yr
    if pct_3yr >= EXTREME_HIGH:
        regime    = "EXTREME_LONG"
        sig_l, sig_s = 0, 1
    elif pct_3yr >= CROWDED_HIGH:
        regime    = "CROWDED_LONG"
        sig_l, sig_s = 0, 1
    elif pct_3yr <= EXTREME_LOW:
        regime    = "EXTREME_SHORT"
        sig_l, sig_s = 1, 0
    elif pct_3yr <= CROWDED_LOW:
        regime    = "DEPRESSED"
        sig_l, sig_s = 1, 0
    else:
        regime    = "NEUTRAL"
        sig_l, sig_s = 0, 0

    # Override: si está en zona moderada pero la tendencia acelera hacia extremo
    if regime == "NEUTRAL" and pct_3yr >= 65 and trend_4wk > 0:
        regime = "BUILDING_LONG"   # informativo, no genera señal binaria
    if regime == "NEUTRAL" and pct_3yr <= 35 and trend_4wk < 0:
        regime = "BUILDING_SHORT"

    # AND gate conviction: percentil extremo en múltiples ventanas + modified_z confirma
    # Previene señales falsas de fat tails: un outlier mueve mZ pero no los percentiles cruzados
    high_conviction = (
        (sig_s == 1 and pct_alltime >= 80 and pct_1yr >= 80) or
        (sig_l == 1 and pct_alltime <= 20 and pct_1yr <= 20)
    ) and (mm_modified_z is None or abs(mm_modified_z) >= 2.0)

    return {
        "mm_net":           int(current),
        "mm_pct_3yr":       round(pct_3yr, 1),
        "mm_pct_1yr":       round(pct_1yr, 1),
        "mm_pct_alltime":   round(pct_alltime, 1),
        "mm_modified_z":    mm_modified_z,
        "mm_trend_4wk":     round(trend_4wk),
        "mm_change_1wk":    round(change_1wk),
        "mm_change_4wk":    round(change_4wk),
        "mm_3yr_min":       int(min(vals_3yr)),
        "mm_3yr_max":       int(max(vals_3yr)),
        "mm_n_weeks":       n_3yr,
        "regime":           regime,
        "high_conviction":  high_conviction,
        "signal_long":      sig_l,
        "signal_short":     sig_s,
    }


def score_cot_percentile(session: Session, direction: str) -> tuple[int, dict]:
    """
    Interfaz para scoring.py — retorna (score 0/1, ctx_dict).
    """
    ctx = compute_mm_percentile(session)
    if "INSUFFICIENT_DATA" in ctx.get("regime", ""):
        return None, ctx
    sig = ctx["signal_long"] if direction.upper() == "LONG" else ctx["signal_short"]
    return sig, ctx
