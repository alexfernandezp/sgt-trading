"""
Analisis de toques multiples a niveles VWAP.

Tres capas:
  1. Contador de toques hoy (barras 5m)
  2. Probabilidad historica de rechazo en este nivel (datos diarios SB_CONT)
  3. Microestructura: mechas de rechazo en las ultimas velas 5m en el nivel

Un "toque" = evento en que el precio visita el nivel y se aleja.
Barras consecutivas que tocan el nivel = un solo evento.

Signal L2-8:
  SHORT: 1 si toques >= 2 Y rechazo_hist >= 45%
  LONG:  1 si toques >= 2 Y bounce_hist  >= 45%
  0/None en caso contrario
"""
import logging
from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)

TOUCH_PROB_MIN  = 0.45   # probabilidad minima de rechazo historico para signal=1
TOUCH_MIN_TODAY = 2      # toques minimos hoy para activar la senal


# ── Utilidades ────────────────────────────────────────────────────────────────

def _nearest_vwap_level(vwap_bands, price, direction):
    """
    Encuentra el nivel VWAP de mayor relevancia mas cercano al precio
    en la direccion del trade.
    SHORT -> nivel por encima (resistencia)
    LONG  -> nivel por debajo (soporte)

    Returns (level_value, label) o (None, None).
    """
    # Prioridad: MTD > Session > YTD (el MTD es el mas significativo para multi-dia)
    candidates = []
    for key, prefix in [("mtd", "VWAP MTD"), ("session", "VWAP Ses"), ("ytd", "VWAP YTD")]:
        vb = (vwap_bands or {}).get(key)
        if not vb:
            continue
        for lk, suffix in [("upper_2", " +2s"), ("upper_1", " +1s"), ("vwap", ""),
                            ("lower_1", " -1s"), ("lower_2", " -2s")]:
            val = vb.get(lk)
            if val is None:
                continue
            dist = val - price
            label = prefix + suffix
            if direction == "SHORT" and dist >= -0.005:   # en o por encima del precio
                candidates.append((abs(dist), val, label))
            elif direction == "LONG" and dist <= 0.005:   # en o por debajo del precio
                candidates.append((abs(dist), val, label))

    if not candidates:
        return None, None

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1], candidates[0][2]


# ── Capa 1: contador de toques hoy ───────────────────────────────────────────

def count_today_touches(session: Session, instrument: str, level: float, tolerance: float):
    """
    Cuenta eventos de toque al nivel usando barras 5m de hoy.
    Barras consecutivas que tocan el nivel = un solo evento.

    Returns: (n_events, touch_bars_list)
    """
    rows = session.execute(text("""
        SELECT datetime, high, low, close, volume
        FROM price_bars
        WHERE instrument = :instr AND interval = '1m'
          AND DATE(datetime) = CURRENT_DATE
        ORDER BY datetime ASC
    """), {"instr": instrument}).fetchall()

    if not rows:
        return 0, []

    touch_bars = []
    n_events   = 0
    in_touch   = False

    for r in rows:
        hi = float(r[1]) if r[1] is not None else 0
        lo = float(r[2]) if r[2] is not None else 0
        touching = (lo <= level + tolerance) and (hi >= level - tolerance)

        if touching and not in_touch:
            n_events += 1
            in_touch = True
        elif not touching:
            in_touch = False

        if touching:
            touch_bars.append({
                "dt":  r[0],
                "hi":  hi,
                "lo":  lo,
                "cl":  float(r[3]) if r[3] is not None else 0,
                "vol": float(r[4]) if r[4] is not None else 0,
            })

    return n_events, touch_bars


# ── Capa 2: historial de rechazos en este nivel ───────────────────────────────

def get_rejection_history(
    session:      Session,
    level:        float,
    direction:    str,
    lookback_days: int = 45,
    tolerance:    float = 0.15,
) -> dict:
    """
    En los ultimos N dias, cuantas veces toco el precio este nivel y cuantos rechazaron.

    SHORT: rechazo = el dia cerro por debajo del nivel (vino a resistencia y bajo)
    LONG:  rechazo = el dia cerro por encima del nivel (vino a soporte y subio)

    Returns: {n_hist, n_rejections, rejection_prob, avg_rejection_size}
    """
    rows = session.execute(text("""
        SELECT high, low, close
        FROM price_history
        WHERE instrument = 'SB_CONT'
          AND date >= CURRENT_DATE - :n
          AND date <  CURRENT_DATE
          AND high >= :lo
          AND low  <= :hi
        ORDER BY date DESC
        LIMIT 60
    """), {
        "n":  lookback_days,
        "lo": level - tolerance,
        "hi": level + tolerance,
    }).fetchall()

    if not rows:
        return {"n_hist": 0, "n_rejections": 0, "rejection_prob": None, "avg_rej_size": 0.0}

    n_rejections = 0
    rej_sizes    = []

    for r in rows:
        hi, lo, cl = float(r[0]), float(r[1]), float(r[2])
        if direction == "SHORT":
            # Precio toco la zona alta y cerro por debajo = rechazo bajista
            if cl < level:
                n_rejections += 1
                rej_sizes.append(round(hi - cl, 4))
        else:
            # Precio toco la zona baja y cerro por encima = rebote alcista
            if cl > level:
                n_rejections += 1
                rej_sizes.append(round(cl - lo, 4))

    n = len(rows)
    return {
        "n_hist":        n,
        "n_rejections":  n_rejections,
        "rejection_prob": round(n_rejections / n, 2) if n > 0 else None,
        "avg_rej_size":  round(sum(rej_sizes) / len(rej_sizes), 4) if rej_sizes else 0.0,
    }


# ── Capa 3: microestructura de velas en el nivel ─────────────────────────────

def get_candle_microstructure(touch_bars: list, level: float, direction: str) -> dict:
    """
    Analiza las velas 5m que tocaron el nivel.

    Detecta:
      wick_rejection  — hay mechas de rechazo significativas (mecha > cuerpo)
      vol_declining   — el volumen en toques sucesivos va bajando (agotamiento)
      last_close_away — el ultimo toque cerro alejandose del nivel (confirmacion)
    """
    if not touch_bars:
        return {"wick_rejection": False, "vol_declining": False, "last_close_away": False}

    wick_ratios = []
    for b in touch_bars:
        body      = abs(b["cl"] - (b["hi"] + b["lo"]) / 2)
        if direction == "SHORT":
            wick  = b["hi"] - max(b["cl"], (b["hi"] + b["lo"]) / 2)
        else:
            wick  = min(b["cl"], (b["hi"] + b["lo"]) / 2) - b["lo"]
        wick_ratio = wick / (b["hi"] - b["lo"]) if (b["hi"] - b["lo"]) > 0 else 0
        wick_ratios.append(wick_ratio)

    # Rechazo por mecha: promedio de mecha en direccion adversa > 35% del rango
    wick_rejection = len(wick_ratios) > 0 and (sum(wick_ratios) / len(wick_ratios)) > 0.35

    # Volumen declinante: cada toque con menos volumen que el anterior
    vols = [b["vol"] for b in touch_bars if b["vol"] > 0]
    vol_declining = (len(vols) >= 2) and (vols[-1] < vols[0] * 0.80)

    # Ultimo toque cerro lejos del nivel en la direccion favorable
    last = touch_bars[-1]
    if direction == "SHORT":
        last_close_away = last["cl"] < level - 0.01
    else:
        last_close_away = last["cl"] > level + 0.01

    return {
        "wick_rejection":  wick_rejection,
        "vol_declining":   vol_declining,
        "last_close_away": last_close_away,
    }


# ── Funcion principal ─────────────────────────────────────────────────────────

def compute_vwap_touch_signal(
    session:    Session,
    instrument: str,
    vwap_bands: dict,
    direction:  str,
    price:      float,
    atr_30m:    float,
) -> dict:
    """
    Calcula la senal de multi-toque VWAP para L2-8.

    Returns dict:
      signal         — 1 / 0 / None
      level          — nivel VWAP monitoreado
      level_name     — nombre del nivel
      touches_today  — eventos de toque hoy
      rejection_prob — probabilidad historica de rechazo (0-1)
      n_hist         — dias historicos usados
      avg_rej_size   — tamano medio de rechazo historico (centavos)
      wick_rejection — mechas de rechazo en velas actuales
      vol_declining  — volumen declinante en los toques
    """
    level, level_name = _nearest_vwap_level(vwap_bands, price, direction)

    if level is None:
        return {"signal": None, "level": None, "level_name": None,
                "touches_today": 0, "rejection_prob": None, "n_hist": 0,
                "avg_rej_size": 0, "wick_rejection": False, "vol_declining": False}

    # Verificacion direccional estricta: soporte solo activa LONG, resistencia solo SHORT
    # Un nivel por debajo del precio es soporte → solo valido para LONG
    # Un nivel por encima del precio es resistencia → solo valido para SHORT
    level_above = level > price
    if direction == "SHORT" and not level_above:
        return {"signal": 0, "level": round(level, 4), "level_name": level_name,
                "touches_today": 0, "rejection_prob": None, "n_hist": 0,
                "avg_rej_size": 0, "wick_rejection": False, "vol_declining": False}
    if direction == "LONG" and level_above:
        return {"signal": 0, "level": round(level, 4), "level_name": level_name,
                "touches_today": 0, "rejection_prob": None, "n_hist": 0,
                "avg_rej_size": 0, "wick_rejection": False, "vol_declining": False}

    # Tolerancia para toque intraday: 0.40 x ATR30m
    touch_tol = round(0.40 * atr_30m, 4)

    n_touches, touch_bars = count_today_touches(session, instrument, level, touch_tol)
    hist                  = get_rejection_history(session, level, direction)
    micro                 = get_candle_microstructure(touch_bars, level, direction)

    rej_prob = hist["rejection_prob"]

    # Senal activa si: >= 2 toques HOY y rechazo historico >= 45%
    # Si no hay historia (n_hist < 5): activar solo con >= 3 toques
    if n_touches >= TOUCH_MIN_TODAY:
        if rej_prob is not None and rej_prob >= TOUCH_PROB_MIN:
            signal = 1
        elif hist["n_hist"] < 5 and n_touches >= 3:
            signal = 1   # sin historia suficiente, 3+ toques activan la senal
        else:
            signal = 0
    else:
        signal = 0

    return {
        "signal":         signal,
        "level":          round(level, 4),
        "level_name":     level_name,
        "touches_today":  n_touches,
        "rejection_prob": rej_prob,
        "n_hist":         hist["n_hist"],
        "avg_rej_size":   hist["avg_rej_size"],
        "wick_rejection": micro["wick_rejection"],
        "vol_declining":  micro["vol_declining"],
        "last_close_away": micro["last_close_away"],
    }
