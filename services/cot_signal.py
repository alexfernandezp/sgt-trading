"""
COT Signal — modelo compuesto HÍBRIDO: nivel MM × velocidad Spec.

Backtest comparativo 18yr (N=979) mostró que cada serie tiene ventaja en una dimensión:

  NIVEL     → MM net (Disaggregated, hedge funds/CTAs puros):
              excluye commodity-index money pasivo y retail noise.
              OOS: EXTREME_SHORT LONG=56%, EXTREME_LONG SHORT=42% (vs SPEC 55%/32%)

  VELOCIDAD → Spec net (Legacy NC+NonRep, idioma del sector):
              al capturar más actores (retail incluido), la capitulación colectiva
              es más diagnóstica del agotamiento.
              OOS: GRAN_REDUCCION LONG=85% (vs MM 71%) — el WR más alto del sistema

La velocidad contextualiza el nivel: reducción de 30k desde -256k (máx corto)
es cualitativamente distinto a reducción de 30k desde posición neutral.

Ref: Negrini de Mattos & Correa (SSRN 4651233) — contrarian COT soft commodities
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)

# ── Umbrales de nivel (percentil rolling 3yr) ────────────────────────────────
WINDOW_WEEKS  = 156   # 3 años — ventana primaria
EXTREME_HIGH  = 90    # specs extremadamente largos  → contrarian SHORT
ELEVATED_HIGH = 75    # specs elevados               → zona vigilancia SHORT
ELEVATED_LOW  = 25    # specs deprimidos             → zona vigilancia LONG
EXTREME_LOW   = 10    # specs extremadamente cortos  → contrarian LONG

# ── Umbral de velocidad (z-score cambio semanal) ─────────────────────────────
VELOCITY_Z = 1.5   # |z| > 1.5 = sorpresa significativa (backtest-validated)

# ── Matriz nivel × velocidad → estado compuesto ──────────────────────────────
#
#                        GRAN_REDUCCION    NORMAL             GRAN_ADICION
#   EXTREME_SHORT    →   CAPITULACION_C    SUELO_POTENCIAL    SUELO_CONFIRMADO
#   DEPRESSED        →   SUELO_POTENCIAL   NEUTRO             NEUTRO
#   NEUTRAL          →   NEUTRO            NEUTRO             NEUTRO
#   ELEVATED         →   NEUTRO            NEUTRO             TECHO_POTENCIAL
#   EXTREME_LONG     →   TECHO_CONFIRMADO  CAPITULACION_L     CAPITULACION_L
#
_MATRIX: dict[tuple[str, str], str] = {
    ("EXTREME_SHORT", "GRAN_REDUCCION"): "CAPITULACION_CORTA",   # LONG ★★★
    ("EXTREME_SHORT", "NORMAL"):          "SUELO_POTENCIAL",      # LONG ★★
    ("EXTREME_SHORT", "GRAN_ADICION"):    "SUELO_CONFIRMADO",     # LONG ★★ (specs cubriendo)
    ("DEPRESSED",     "GRAN_REDUCCION"):  "SUELO_POTENCIAL",      # LONG ★★
    ("DEPRESSED",     "NORMAL"):          "NEUTRO",
    ("DEPRESSED",     "GRAN_ADICION"):    "NEUTRO",
    ("NEUTRAL",       "GRAN_REDUCCION"):  "NEUTRO",
    ("NEUTRAL",       "NORMAL"):          "NEUTRO",
    ("NEUTRAL",       "GRAN_ADICION"):    "NEUTRO",
    ("ELEVATED",      "GRAN_REDUCCION"):  "NEUTRO",
    ("ELEVATED",      "NORMAL"):          "NEUTRO",
    ("ELEVATED",      "GRAN_ADICION"):    "TECHO_POTENCIAL",      # SHORT ★★
    ("EXTREME_LONG",  "GRAN_REDUCCION"):  "TECHO_CONFIRMADO",     # SHORT ★★ (specs empezando a huir)
    ("EXTREME_LONG",  "NORMAL"):          "CAPITULACION_LARGA",   # SHORT ★★★
    ("EXTREME_LONG",  "GRAN_ADICION"):    "CAPITULACION_LARGA",   # SHORT ★★★ (crowding máximo)
}

# (signal_long, signal_short, conviction 0-3)
_SIGNALS: dict[str, tuple[int, int, int]] = {
    "CAPITULACION_CORTA":  (1, 0, 3),
    "SUELO_POTENCIAL":     (1, 0, 2),
    "SUELO_CONFIRMADO":    (1, 0, 2),
    "NEUTRO":              (0, 0, 0),
    "TECHO_POTENCIAL":     (0, 1, 2),
    "TECHO_CONFIRMADO":    (0, 1, 2),
    "CAPITULACION_LARGA":  (0, 1, 3),
}

_STARS = {3: "★★★", 2: "★★ ", 1: "★  ", 0: "   "}


@dataclass
class CotSignal:
    # Nivel — MM net (Disaggregated)
    mm_net:          int
    mm_pct_3yr:      float
    mm_pct_1yr:      float
    mm_pct_alltime:  float
    mm_modified_z:   float | None
    mm_3yr_min:      int
    mm_3yr_max:      int
    mm_n_weeks:      int

    # Velocidad — Spec net (Legacy NC+NonRep): 85% OOS WR GRAN_REDUCCION
    spec_net:        int     # referencia industria (idioma del sector)
    spec_change_1wk: int     # cambio semanal spec_net (base del weekly_z)
    mm_change_1wk:   int     # cambio semanal mm_net (contexto adicional)
    mm_change_4wk:   int
    mm_trend_4wk:    float
    mm_weekly_z:     float   # z-score sobre spec_net (ver docstring del módulo)
    mm_velocity_std: float   # std de cambios spec_net (contexto para z)

    # Compuesto
    level_regime:    str     # EXTREME_SHORT | DEPRESSED | NEUTRAL | ELEVATED | EXTREME_LONG
    velocity_class:  str     # GRAN_REDUCCION | NORMAL | GRAN_ADICION
    composite_state: str     # CAPITULACION_CORTA … CAPITULACION_LARGA
    conviction:      int     # 0-3 (para sizing)
    signal_long:     int
    signal_short:    int

    # Texto
    context_str:     str


def _level(pct: float) -> str:
    if pct <= EXTREME_LOW:   return "EXTREME_SHORT"
    if pct <= ELEVATED_LOW:  return "DEPRESSED"
    if pct >= EXTREME_HIGH:  return "EXTREME_LONG"
    if pct >= ELEVATED_HIGH: return "ELEVATED"
    return "NEUTRAL"


def _velocity(z: float) -> str:
    if z <= -VELOCITY_Z: return "GRAN_REDUCCION"
    if z >= +VELOCITY_Z: return "GRAN_ADICION"
    return "NORMAL"


def get_cot_signal(session: Session) -> CotSignal:
    """Única fuente de verdad COT para el sistema. Llama solo desde aquí."""
    rows = session.execute(text(
        "SELECT mm_net, speculator_net FROM cot_data "
        "WHERE mm_net IS NOT NULL "
        "ORDER BY report_date DESC "
        f"LIMIT {WINDOW_WEEKS + 10}"
    )).fetchall()

    if len(rows) < 8:
        logger.warning("cot_signal | datos insuficientes: %d semanas", len(rows))
        return _insufficient()

    mm_vals   = [float(r[0]) for r in rows]
    spec_vals = [float(r[1]) for r in rows if r[1] is not None]

    v3      = mm_vals[:WINDOW_WEEKS]
    current = v3[0]
    n       = len(v3)

    # ── Nivel: MM net (Disaggregated) ────────────────────────────────────────
    pct_3yr = sum(1 for v in v3 if v <= current) / n * 100
    v1yr    = v3[:min(52, n)]
    pct_1yr = sum(1 for v in v1yr if v <= current) / len(v1yr) * 100

    all_rows = session.execute(text(
        "SELECT mm_net FROM cot_data WHERE mm_net IS NOT NULL ORDER BY report_date DESC"
    )).fetchall()
    all_vals = [float(r[0]) for r in all_rows]
    pct_all  = sum(1 for v in all_vals if v <= current) / len(all_vals) * 100

    try:
        from services.stats_utils import robust_stats
        mm_modified_z = robust_stats(v3[1:], current).get("modified_z")
    except Exception:
        mm_modified_z = None

    # MM: tendencia 4w y cambio 4w (contexto)
    ma4_now   = sum(v3[:4]) / 4
    ma4_prev  = sum(v3[1:5]) / 4 if n >= 5 else ma4_now
    trend_4wk = ma4_now - ma4_prev
    mm_chg1w  = int(v3[0] - v3[1]) if n >= 2 else 0
    chg_4wk   = v3[0] - v3[min(4, n - 1)]

    # ── Velocidad: Spec net (Legacy NC+NonRep) ───────────────────────────────
    # Backtest comparativo: GRAN_REDUCCION LONG OOS WR = 85% spec vs 71% mm.
    # La liquidación colectiva (retail + hedge fund juntos) es más diagnóstica
    # del agotamiento que solo mm_net.
    sv3      = spec_vals[:WINDOW_WEEKS]
    spec_cur = sv3[0] if sv3 else 0
    s_chg1w  = int(sv3[0] - sv3[1]) if len(sv3) >= 2 else 0

    if len(sv3) >= 4:
        s_changes = [sv3[i] - sv3[i + 1] for i in range(len(sv3) - 1)]
        s_mean    = sum(s_changes) / len(s_changes)
        s_std     = (sum((c - s_mean) ** 2 for c in s_changes) / len(s_changes)) ** 0.5
        weekly_z  = (s_chg1w - s_mean) / s_std if s_std > 0 else 0.0
    else:
        s_std, weekly_z = 0.0, 0.0

    # ── Compuesto ────────────────────────────────────────────────────────────
    lv  = _level(pct_3yr)
    vel = _velocity(weekly_z)
    cs  = _MATRIX[(lv, vel)]
    sl, ss, conv = _SIGNALS[cs]

    stars  = _STARS[conv]
    dirstr = "LONG" if sl else ("SHORT" if ss else "NEUTRO")
    ctx_str = (
        f"MM={int(current):+,} P3yr={pct_3yr:.0f}% {lv} | "
        f"Spec Δ1wk={s_chg1w:+,} z={weekly_z:+.2f} {vel} "
        f"→ {cs} ({dirstr} {stars})"
    )
    logger.debug("cot_signal | %s", ctx_str)

    return CotSignal(
        mm_net=int(current),
        mm_pct_3yr=round(pct_3yr, 1),
        mm_pct_1yr=round(pct_1yr, 1),
        mm_pct_alltime=round(pct_all, 1),
        mm_modified_z=mm_modified_z,
        mm_3yr_min=int(min(v3)),
        mm_3yr_max=int(max(v3)),
        mm_n_weeks=n,
        spec_net=int(spec_cur),
        spec_change_1wk=s_chg1w,
        mm_change_1wk=mm_chg1w,
        mm_change_4wk=int(chg_4wk),
        mm_trend_4wk=round(trend_4wk),
        mm_weekly_z=round(weekly_z, 3),
        mm_velocity_std=round(s_std),
        level_regime=lv,
        velocity_class=vel,
        composite_state=cs,
        conviction=conv,
        signal_long=sl,
        signal_short=ss,
        context_str=ctx_str,
    )


def _insufficient() -> CotSignal:
    return CotSignal(
        mm_net=0, mm_pct_3yr=50.0, mm_pct_1yr=50.0, mm_pct_alltime=50.0,
        mm_modified_z=None, mm_3yr_min=0, mm_3yr_max=0, mm_n_weeks=0,
        spec_net=0, spec_change_1wk=0,
        mm_change_1wk=0, mm_change_4wk=0, mm_trend_4wk=0.0,
        mm_weekly_z=0.0, mm_velocity_std=0.0,
        level_regime="INSUFFICIENT_DATA", velocity_class="NORMAL",
        composite_state="NEUTRO", conviction=0, signal_long=0, signal_short=0,
        context_str="COT: datos insuficientes",
    )


def score_cot(session: Session, direction: str) -> tuple[int | None, dict]:
    """Interfaz limpia para scoring.py. Retorna (score 0/1/None, ctx_dict)."""
    sig = get_cot_signal(session)
    if sig.level_regime == "INSUFFICIENT_DATA":
        return None, asdict(sig)
    score = sig.signal_long if direction.upper() == "LONG" else sig.signal_short
    return score, asdict(sig)
