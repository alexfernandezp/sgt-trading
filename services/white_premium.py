"""
White Premium Signal — No.5 London vs No.11 ICE (WS_CONT - SB_CONT).

El spread WS - SB en USD/t refleja el margen de refinado global.
Revierte al coste de refinado histórico (~$80-100/t) en ventanas de 4-8 semanas.

Señal para ICE No.11 (SB):
  WP muy alto (>120 USD/t, Z > +1.5):
    Refinadores muy rentables → aumentan producción blanco → presión bajista en SB
    También señala demanda fuerte de azúcar refinado → bullish para raw subyacente
    Net: ambiguo, pero extremo histórico → mean reversion del WP bajista

  WP normal (80-120 USD/t):
    Margen de refinado sano → mercado equilibrado → NEUTRAL

  WP bajo (<70 USD/t, Z < -1.5):
    Refinadores pierden dinero → reducen producción → bullish para raw (menos competencia
    de azúcar blanco en el mercado global) → LONG SB

Fórmula conversión: WS (USD/t) - SB (c/lb) × 22.0462 = WP (USD/t)
  [22.0462 = 2204.62 lbs/tonne / 100 cents/dollar]

Datos: WS_CONT (London No.5 continuo) + SB_CONT (ICE No.11 continuo)
  Disponibles desde 2019-01-02. 1862 barras diarias.
"""
import logging
from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)

LBS_PER_MT       = 2204.62
REFINING_COST_LO = 80.0    # USD/t — umbral bajo, refinadores pierden dinero
REFINING_COST_HI = 120.0   # USD/t — umbral alto, refinadores muy rentables

# Ventanas de Z-score
WINDOW_SHORT = 52    # ~3 meses trading
WINDOW_LONG  = 260   # ~1 año trading


def compute_white_premium(session: Session) -> dict:
    """
    Calcula el white premium y su Z-score histórico.

    Returns dict con:
      wp_current, wp_mean_3m, wp_std_3m, wp_zscore_3m,
      wp_mean_1yr, wp_zscore_1yr,
      regime, signal_long (0/1), signal_short (0/1)
    """
    rows = session.execute(text("""
        SELECT w.date,
               w.close                                          AS ws_close,
               s.close                                          AS sb_close,
               w.close - s.close * :conv                        AS wp
        FROM price_history w
        JOIN price_history s ON w.date = s.date
            AND s.instrument = 'SB_CONT'
        WHERE w.instrument = 'WS_CONT'
          AND w.close IS NOT NULL
          AND s.close IS NOT NULL
        ORDER BY w.date DESC
        LIMIT :limit
    """), {"conv": LBS_PER_MT / 100.0, "limit": WINDOW_LONG + 50}).fetchall()

    if len(rows) < 20:
        return {"regime": "INSUFFICIENT_DATA", "signal_long": 0, "signal_short": 0}

    from services.stats_utils import robust_stats

    wps        = [float(r[3]) for r in rows]
    current_wp = wps[0]
    current_ws = float(rows[0][1])
    current_sb = float(rows[0][2])
    last_date  = rows[0][0]

    # Estadísticas robustas sobre ventana rolling 3 meses (52 sesiones)
    rs_short = robust_stats(wps[:WINDOW_SHORT], current_wp)

    # Estadísticas robustas sobre ventana rolling 1 año (260 sesiones)
    n_long   = min(WINDOW_LONG, len(wps))
    rs_long  = robust_stats(wps[:n_long], current_wp)

    # Régimen: niveles absolutos tienen prioridad (económicamente fundamentados),
    # luego AND gate estadístico (robusto a fat tails)
    if current_wp >= REFINING_COST_HI or rs_long.get("is_extreme_high"):
        regime   = "ELEVATED"
        sig_l, sig_s = 0, 1
    elif current_wp <= REFINING_COST_LO or rs_long.get("is_extreme_low"):
        regime   = "DEPRESSED"
        sig_l, sig_s = 1, 0
    elif (rs_long.get("percentile_rank") or 50) >= 65:
        regime   = "ABOVE_NORMAL"
        sig_l, sig_s = 0, 0
    elif (rs_long.get("percentile_rank") or 50) <= 35:
        regime   = "BELOW_NORMAL"
        sig_l, sig_s = 0, 0
    else:
        regime   = "NEUTRAL"
        sig_l, sig_s = 0, 0

    # Change vs 1 semana atrás (5 sesiones)
    wp_1w_ago = wps[min(5, len(wps) - 1)]
    wp_change_1w = round(current_wp - wp_1w_ago, 2)

    # Change vs 1 mes atrás (21 sesiones)
    wp_1m_ago = wps[min(21, len(wps) - 1)]
    wp_change_1m = round(current_wp - wp_1m_ago, 2)

    return {
        "wp_current":       round(current_wp, 2),
        "wp_ws_close":      round(current_ws, 2),
        "wp_sb_close":      round(current_sb, 4),
        "wp_last_date":     str(last_date),
        # Ventana 3 meses (rolling 52 sesiones)
        "wp_modified_z_3m": rs_short.get("modified_z"),
        "wp_pct_3m":        rs_short.get("percentile_rank"),
        "wp_median_3m":     rs_short.get("median"),
        "wp_mad_3m":        rs_short.get("mad"),
        "wp_conviction_3m": rs_short.get("conviction"),
        # Ventana 1 año (rolling 260 sesiones)
        "wp_modified_z_1yr": rs_long.get("modified_z"),
        "wp_pct_1yr":        rs_long.get("percentile_rank"),
        "wp_median_1yr":     rs_long.get("median"),
        "wp_mad_1yr":        rs_long.get("mad"),
        "wp_conviction_1yr": rs_long.get("conviction"),
        "wp_change_1w":     wp_change_1w,
        "wp_change_1m":     wp_change_1m,
        "wp_refining_lo":   REFINING_COST_LO,
        "wp_refining_hi":   REFINING_COST_HI,
        "regime":           regime,
        "signal_long":      sig_l,
        "signal_short":     sig_s,
    }


def score_white_premium(session: Session, direction: str) -> tuple[int, dict]:
    """
    Interfaz para scoring.py — retorna (score 0/1, ctx_dict).
    """
    ctx = compute_white_premium(session)
    if "INSUFFICIENT_DATA" in ctx.get("regime", ""):
        return None, ctx
    sig = ctx["signal_long"] if direction.upper() == "LONG" else ctx["signal_short"]
    return sig, ctx
