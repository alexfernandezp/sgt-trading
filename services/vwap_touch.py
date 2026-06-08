"""
L2-8: VWAP multi-session rejection signal.

Rediseño 2026-06-08:
  Anterior: contaba toques intraday de HOY → ambigüedad, [OK] en ambas direcciones.
  Nuevo:    cuenta rechazos a nivel de SESION sobre niveles VWAP estructurales
            y aplica test de DOMINANCIA: solo activa señal si los rechazos en la
            dirección solicitada superan a los del lado contrario.

Lógica:
  1. Encontrar el mejor nivel VWAP para la dirección solicitada
     (SHORT → resistencia sobre el precio; LONG → soporte bajo el precio)
     Prioridad de timeframe: YTD > MTD > Session

  2. También encontrar el mejor nivel en la dirección contraria.

  3. Señal = 1 solo si:
       - rechazos propios >= SESSION_REJECTIONS_MIN (>=3)
       - rechazos propios > rechazos del lado contrario   (dominancia)

  Esto evita que tanto SHORT como LONG sean [OK] simultáneamente cuando el
  mercado está en rango entre soporte y resistencia igualmente fuertes.
  Si ambos lados tienen rechazos similares → [--] en los dos (rango, sin señal).
"""
import logging
from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)

SESSION_REJECTIONS_MIN = 3     # sesiones con rechazo para activar señal
SESSION_LOOKBACK       = 15    # sesiones históricas a revisar
TOUCH_TOLERANCE        = 0.20  # c/lb — ancho de zona "precio testó este nivel"


# ── Candidatos ────────────────────────────────────────────────────────────────

def _candidate_levels(vwap_bands: dict, price: float, direction: str) -> list:
    """
    Lista de niveles VWAP candidatos para la dirección dada.

    SHORT → niveles por ENCIMA del precio (resistencias)
    LONG  → niveles por DEBAJO del precio (soportes)

    Retorna: [(distancia, valor, etiqueta, prioridad_tf)]
    prioridad_tf: ytd=3, mtd=2, session=1
    """
    candidates = []
    for key, tf_prio in [("ytd", 3), ("mtd", 2), ("session", 1)]:
        vb = (vwap_bands or {}).get(key)
        if not vb:
            continue
        for lk, suffix in [("vwap", ""), ("upper_1", " +1s"), ("upper_2", " +2s"),
                            ("lower_1", " -1s"), ("lower_2", " -2s")]:
            val = vb.get(lk)
            if val is None:
                continue
            dist = val - price
            label = "VWAP %s%s" % (key.upper(), suffix)
            if direction == "SHORT" and dist > 0:
                candidates.append((dist, val, label, tf_prio))
            elif direction == "LONG" and dist < 0:
                candidates.append((abs(dist), val, label, tf_prio))
    return candidates


# ── Contador de rechazos de sesión ────────────────────────────────────────────

def count_session_rejections(
    session:   Session,
    level:     float,
    direction: str,
    lookback:  int   = SESSION_LOOKBACK,
    touch_tol: float = TOUCH_TOLERANCE,
) -> dict:
    """
    Sesiones en las últimas N donde precio testó el nivel y fue rechazado.

    Test     SHORT: high del día >= level - touch_tol
    Test     LONG:  low  del día <= level + touch_tol
    Rechazo  SHORT: test Y close < level
    Rechazo  LONG:  test Y close > level
    """
    rows = session.execute(text("""
        SELECT date, high, low, close
        FROM price_history
        WHERE instrument = 'SB_CONT'
          AND date >= CURRENT_DATE - :n
          AND date <  CURRENT_DATE
        ORDER BY date DESC
        LIMIT :lim
    """), {"n": lookback + 7, "lim": lookback + 7}).fetchall()

    if not rows:
        return {"n_sessions": 0, "n_tested": 0, "n_rejected": 0, "rejection_rate": None}

    n_sessions = 0
    n_tested   = 0
    n_rejected = 0

    for r in rows:
        if n_sessions >= lookback:
            break
        hi = float(r[1]) if r[1] is not None else 0.0
        lo = float(r[2]) if r[2] is not None else 0.0
        cl = float(r[3]) if r[3] is not None else 0.0
        n_sessions += 1

        if direction == "SHORT":
            tested   = hi >= (level - touch_tol)
            rejected = tested and cl < level
        else:
            tested   = lo <= (level + touch_tol)
            rejected = tested and cl > level

        if tested:
            n_tested += 1
        if rejected:
            n_rejected += 1

    rate = round(n_rejected / n_tested, 2) if n_tested > 0 else None
    return {
        "n_sessions":    n_sessions,
        "n_tested":      n_tested,
        "n_rejected":    n_rejected,
        "rejection_rate": rate,
    }


# ── Toques intraday hoy (confirmación secundaria) ────────────────────────────

def count_today_touches(session: Session, instrument: str, level: float, tolerance: float) -> int:
    """Eventos de toque al nivel en barras 1m de hoy."""
    rows = session.execute(text("""
        SELECT high, low
        FROM price_bars
        WHERE instrument = :instr AND interval = '1m'
          AND DATE(datetime) = CURRENT_DATE
        ORDER BY datetime ASC
    """), {"instr": instrument}).fetchall()

    if not rows:
        return 0

    n_events = 0
    in_touch = False
    for r in rows:
        hi = float(r[0]) if r[0] is not None else 0.0
        lo = float(r[1]) if r[1] is not None else 0.0
        touching = (lo <= level + tolerance) and (hi >= level - tolerance)
        if touching and not in_touch:
            n_events += 1
            in_touch = True
        elif not touching:
            in_touch = False
    return n_events


# ── Helper: mejor nivel para una dirección ───────────────────────────────────

def _best_level_for_direction(
    session:    Session,
    vwap_bands: dict,
    price:      float,
    direction:  str,
) -> dict:
    """
    Retorna el nivel VWAP con más rechazos de sesión para la dirección dada.
    Prioridad: más rechazos > mayor timeframe > más cercano.
    """
    candidates = _candidate_levels(vwap_bands, price, direction)
    if not candidates:
        return {"n_rejected": 0, "n_tested": 0, "rejection_rate": None,
                "level": None, "level_name": None}

    candidates.sort(key=lambda x: (-x[3], x[0]))  # TF desc, distancia asc

    best_n   = -1
    best_tf  = -1
    best_res = None

    for dist, lv, label, tf_prio in candidates[:5]:
        hist = count_session_rejections(session, lv, direction)
        n = hist["n_rejected"]
        if n > best_n or (n == best_n and tf_prio > best_tf):
            best_n  = n
            best_tf = tf_prio
            best_res = {
                "level":          round(lv, 4),
                "level_name":     label,
                "n_rejected":     n,
                "n_tested":       hist["n_tested"],
                "n_sessions":     hist["n_sessions"],
                "rejection_rate": hist["rejection_rate"],
            }

    return best_res or {"n_rejected": 0, "n_tested": 0, "rejection_rate": None,
                        "level": None, "level_name": None}


# ── Función principal ─────────────────────────────────────────────────────────

def compute_vwap_touch_signal(
    session:    Session,
    instrument: str,
    vwap_bands: dict,
    direction:  str,
    price:      float,
    atr_30m:    float,
) -> dict:
    """
    L2-8: señal de rechazo multi-sesión en niveles VWAP estructurales.

    Activa señal solo si:
      1. El mejor nivel en la dirección solicitada tiene >= 3 rechazos de sesión
      2. Esos rechazos SUPERAN a los del lado contrario (dominancia)

    Esto evita [OK]/[OK] simultáneo cuando el mercado está en rango.

    Retorna dict: signal, level, level_name, n_rejected, n_tested,
                  rejection_rate, opposing_rejected, touches_today.
    """
    null_result = {
        "signal": None, "level": None, "level_name": None,
        "n_rejected": 0, "n_tested": 0, "rejection_rate": None,
        "opposing_rejected": 0, "touches_today": 0,
    }

    opp_dir = "LONG" if direction == "SHORT" else "SHORT"

    mine = _best_level_for_direction(session, vwap_bands, price, direction)
    opp  = _best_level_for_direction(session, vwap_bands, price, opp_dir)

    mine_rej = mine.get("n_rejected", 0)
    opp_rej  = opp.get("n_rejected",  0)

    if mine.get("level") is None:
        return null_result

    touch_tol = round(0.40 * atr_30m, 4)
    n_touches = count_today_touches(session, instrument, mine["level"], touch_tol)

    # Dominancia: mis rechazos deben superar al lado contrario
    signal = 1 if (mine_rej >= SESSION_REJECTIONS_MIN and mine_rej > opp_rej) else 0

    return {
        "signal":             signal,
        "level":              mine["level"],
        "level_name":         mine["level_name"],
        "n_rejected":         mine_rej,
        "n_tested":           mine.get("n_tested", 0),
        "n_sessions":         mine.get("n_sessions", 0),
        "rejection_rate":     mine.get("rejection_rate"),
        "opposing_rejected":  opp_rej,
        "opp_level_name":     opp.get("level_name"),
        "touches_today":      n_touches,
    }
