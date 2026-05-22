"""
Zona de entrada intradía recomendada.

Mapea el precio actual contra todos los niveles tecnicos clave
y recomienda un precio/rango de entrada especifico con condicion
de activacion y calidad del setup.

Niveles usados (por prioridad):
  VWAP sesion, VWAP MTD +-1s/2s, VWAP YTD +-1s
  Prev day H/L/C/Mid
  Opening range H/L (primeras 2 barras 30m del dia)
  MA20(1h), MA20(4h)
"""
import logging
from datetime import date
from sqlalchemy.orm import Session
from sqlalchemy import text
from ingestion.intraday import calc_session_vwap
from services.scoring import get_current_price
from services.anchored_vwap import get_vwap_bands
from services.mtf_alignment import _bars, _ma, _prev_day
from services.fibonacci import get_fibonacci_levels

logger = logging.getLogger(__name__)

# Significance weights for level quality (higher = more important)
_SIG = {
    "VWAP Sesion":    3,
    "VWAP Ses -1s":   3,
    "VWAP Ses +1s":   3,
    "VWAP MTD":       3,
    "VWAP MTD -1s":   3,
    "VWAP MTD +1s":   3,
    "VWAP MTD -2s":   2,
    "VWAP MTD +2s":   2,
    "VWAP YTD":       2,
    "VWAP YTD -1s":   2,
    "VWAP YTD +1s":   2,
    "Prev Day High":  3,
    "Prev Day Low":   3,
    "Prev Day Close": 2,
    "Prev Day Mid":   1,
    "MA20(1h)":       2,
    "MA20(4h)":       2,
    "OR High":        2,
    "OR Low":         2,
    # Fibonacci (rellenados dinamicamente, sig por ratio)
    "Fib  0.0%":      2,
    "Fib 23.6%":      2,
    "Fib 38.2%":      3,
    "Fib 50.0%":      3,
    "Fib 61.8%":      3,
    "Fib 78.6%":      2,
    "Fib 100% ":      2,
}


def _opening_range(session, instrument):
    """High y low de las primeras 2 barras 30m del dia actual."""
    today = date.today()
    rows = session.execute(text("""
        SELECT high, low FROM price_bars
        WHERE instrument = :instr AND interval = '30m'
          AND DATE(datetime) = :d
        ORDER BY datetime ASC LIMIT 2
    """), {"instr": instrument, "d": today}).fetchall()
    if not rows:
        return None, None
    highs = [float(r[0]) for r in rows]
    lows  = [float(r[1]) for r in rows]
    return round(max(highs), 4), round(min(lows), 4)


def _collect_levels(session, instrument, vwap_bands):
    """Devuelve (dict {nombre: valor}, fib_data | None) de todos los niveles disponibles."""
    levels = {}

    # Session VWAP anclado con bandas (5m bars) — usar de vwap_bands si disponible
    sv_anchored = vwap_bands.get("session")
    if sv_anchored:
        levels["VWAP Sesion"]  = sv_anchored["vwap"]
        levels["VWAP Ses -1s"] = sv_anchored["lower_1"]
        levels["VWAP Ses +1s"] = sv_anchored["upper_1"]
    else:
        # Fallback al VWAP simple de sesion
        sv = calc_session_vwap(session, instrument)
        if sv is not None:
            levels["VWAP Sesion"] = round(sv, 4)

    # MTD VWAP + bands (incluyendo ±2s)
    mtd = vwap_bands.get("mtd")
    if mtd:
        levels["VWAP MTD"]     = mtd["vwap"]
        levels["VWAP MTD -1s"] = mtd["lower_1"]
        levels["VWAP MTD +1s"] = mtd["upper_1"]
        levels["VWAP MTD -2s"] = mtd["lower_2"]
        levels["VWAP MTD +2s"] = mtd["upper_2"]

    # YTD VWAP + bands
    ytd = vwap_bands.get("ytd")
    if ytd:
        levels["VWAP YTD"]     = ytd["vwap"]
        levels["VWAP YTD -1s"] = ytd["lower_1"]
        levels["VWAP YTD +1s"] = ytd["upper_1"]

    # Prev day H/L/C/Mid
    prev = _prev_day(session, instrument)
    if prev:
        levels["Prev Day High"]  = prev["high"]
        levels["Prev Day Low"]   = prev["low"]
        levels["Prev Day Close"] = prev["close"]
        levels["Prev Day Mid"]   = round((prev["high"] + prev["low"]) / 2, 4)

    # MA20(1h) and MA20(4h)
    b1h = _bars(session, instrument, "1h", 25)
    if len(b1h) >= 20:
        ma1h = _ma([float(b[4]) for b in b1h])
        if ma1h:
            levels["MA20(1h)"] = ma1h

    b4h = _bars(session, instrument, "4h", 25)
    if len(b4h) >= 20:
        ma4h = _ma([float(b[4]) for b in b4h])
        if ma4h:
            levels["MA20(4h)"] = ma4h

    # Opening range
    or_hi, or_lo = _opening_range(session, instrument)
    if or_hi is not None:
        levels["OR High"] = or_hi
        levels["OR Low"]  = or_lo

    # Fibonacci (SB_CONT continuo, ultimos 45 dias)
    fib_data = None
    try:
        fib_data = get_fibonacci_levels(session, "SB_CONT", lookback_days=45)
        if fib_data:
            for label, lv in fib_data["levels"].items():
                levels[label] = lv["value"]
    except Exception as e:
        logger.warning("fibonacci: %s", e)

    return levels, fib_data


def _classify_levels(levels, price, direction, atr_30m):
    """
    Para cada nivel calcula distancia y lo clasifica como
    support/resistance para la direccion dada.
    Devuelve lista ordenada por |dist| asc.
    """
    result = []
    for name, val in levels.items():
        dist    = round(val - price, 4)   # + = nivel por encima
        sig     = _SIG.get(name, 1)
        in_atr  = abs(dist) <= 2.0 * atr_30m  # dentro de 2 ATR30m

        # Para LONG: soporte = nivel por debajo (dist <= 0)
        # Para SHORT: resistencia = nivel por encima (dist >= 0)
        if direction == "LONG":
            role = "support" if dist <= 0 else "resistance"
        else:
            role = "resistance" if dist >= 0 else "support"

        result.append({
            "name":    name,
            "value":   val,
            "dist":    dist,
            "sig":     sig,
            "in_atr":  in_atr,
            "role":    role,
        })

    return sorted(result, key=lambda x: abs(x["dist"]))


def _detect_clusters(classified, direction, cluster_width):
    """
    Agrupa niveles de calidad (sig >= 2) del mismo rol en clusters de confluencia.
    Un cluster son 2+ niveles separados por <= cluster_width centavos.

    SHORT -> agrupa resistencias (niveles por encima del precio)
    LONG  -> agrupa soportes   (niveles por debajo del precio)

    Devuelve lista de clusters ordenada por proximidad al precio (mas cercano primero).
    Cada cluster: {levels, lo, hi, size, total_sig, names}
    """
    role_target = "resistance" if direction == "SHORT" else "support"
    relevant = sorted(
        [l for l in classified if l["role"] == role_target and l["sig"] >= 2],
        key=lambda x: x["value"]
    )
    if len(relevant) < 2:
        return []

    raw_clusters = []
    current = [relevant[0]]
    for lv in relevant[1:]:
        if lv["value"] - current[-1]["value"] <= cluster_width:
            current.append(lv)
        else:
            raw_clusters.append(current)
            current = [lv]
    raw_clusters.append(current)

    result = []
    for c in raw_clusters:
        if len(c) < 2:
            continue
        vals = [l["value"] for l in c]
        result.append({
            "levels":    c,
            "lo":        round(min(vals), 4),
            "hi":        round(max(vals), 4),
            "size":      len(c),
            "total_sig": sum(l["sig"] for l in c),
            "names":     [l["name"] for l in c],
        })

    # Closest cluster first
    if direction == "SHORT":
        result.sort(key=lambda x: x["lo"])   # cluster mas bajo por encima = mas cercano
    else:
        result.sort(key=lambda x: x["hi"], reverse=True)  # cluster mas alto por debajo

    return result


def _entry_recommendation(classified, price, direction, atr_30m):
    """
    Determina el tipo de entrada y precio recomendado.

    Tipos (en orden de prioridad):
      AT_CLUSTER       - precio en zona de cluster de confluencia (>= 2 niveles)
      PULLBACK_CLUSTER - esperar que el precio llegue al cluster
      AT_LEVEL         - precio en un nivel de calidad individual
      PULLBACK         - esperar pullback al nivel mas cercano
      BREAKOUT         - esperar confirmacion de ruptura
      WAIT             - sin nivel claro cerca
    """
    threshold_at    = 0.35 * atr_30m
    threshold_close = 1.5  * atr_30m
    cluster_width   = 1.0  * atr_30m   # niveles dentro de 1xATR forman cluster

    role_target = "support" if direction == "LONG" else "resistance"
    near_target = [l for l in classified if l["role"] == role_target and l["sig"] >= 2]
    near_opp    = [l for l in classified if l["role"] != role_target and l["sig"] >= 2]

    if not near_target:
        return {
            "type": "WAIT", "quality": "ESPERAR",
            "entry": None, "zone_lo": None, "zone_hi": None,
            "condition": "Sin niveles de soporte/resistencia cercanos identificados",
            "rationale": "Esperar que el precio se ubique cerca de un nivel tecnico clave",
            "ref_level": None, "cluster": None, "cluster_stop": None,
        }

    # ── CASO CLUSTER: buscar confluencia de 2+ niveles ───────────────────────
    clusters = _detect_clusters(classified, direction, cluster_width)
    if clusters:
        best_c = clusters[0]
        # Distancia del precio al borde mas cercano del cluster
        if direction == "SHORT":
            dist_near = best_c["lo"] - price        # positivo = cluster por encima
        else:
            dist_near = price - best_c["hi"]        # positivo = cluster por debajo

        if dist_near <= threshold_close:
            # Entrada optima: SHORT -> techo del cluster; LONG -> suelo del cluster
            if direction == "SHORT":
                entry        = best_c["hi"]
                cluster_stop = round(best_c["hi"] + 1.0 * atr_30m, 4)
                zone_lo      = best_c["lo"]
                zone_hi      = round(best_c["hi"] + 0.10 * atr_30m, 4)
                cond = "SHORT en techo cluster %.4f  [cluster: %.4f - %.4f]" % (
                    entry, best_c["lo"], best_c["hi"])
            else:
                entry        = best_c["lo"]
                cluster_stop = round(best_c["lo"] - 1.0 * atr_30m, 4)
                zone_lo      = round(best_c["lo"] - 0.10 * atr_30m, 4)
                zone_hi      = best_c["hi"]
                cond = "LONG en suelo cluster %.4f  [cluster: %.4f - %.4f]" % (
                    entry, best_c["lo"], best_c["hi"])

            entry_type = "AT_CLUSTER" if abs(dist_near) <= threshold_at else "PULLBACK_CLUSTER"
            quality = ("OPTIMA"  if best_c["total_sig"] >= 7 else
                       "BUENA"   if best_c["total_sig"] >= 5 else "MODERADA")

            names_str = " + ".join(best_c["names"][:3])
            if len(best_c["names"]) > 3:
                names_str += " +%d" % (len(best_c["names"]) - 3)
            riesgo_c = round(abs(entry - cluster_stop), 4)

            return {
                "type":         entry_type,
                "quality":      quality,
                "entry":        entry,
                "zone_lo":      zone_lo,
                "zone_hi":      zone_hi,
                "condition":    cond,
                "rationale":    "%d niveles confluentes: %s  |  stop %.4f (%.4fc riesgo)" % (
                    best_c["size"], names_str, cluster_stop, riesgo_c),
                "ref_level":    best_c["levels"][-1 if direction == "SHORT" else 0],
                "cluster":      best_c,
                "cluster_stop": cluster_stop,
            }

    # ── CASO NIVEL INDIVIDUAL ────────────────────────────────────────────────
    best = near_target[0]

    if abs(best["dist"]) <= threshold_at:
        entry = best["value"]
        lo    = round(entry - 0.5 * atr_30m, 4) if direction == "LONG" else entry
        hi    = entry if direction == "LONG" else round(entry + 0.5 * atr_30m, 4)
        return {
            "type":         "AT_LEVEL",
            "quality":      "OPTIMA" if best["sig"] == 3 else "BUENA",
            "entry":        entry,
            "zone_lo":      lo,
            "zone_hi":      hi,
            "condition":    "Precio en %s (%.4f) - entrada inmediata" % (best["name"], best["value"]),
            "rationale":    "Nivel de alta significancia, stop al otro lado",
            "ref_level":    best,
            "cluster":      None,
            "cluster_stop": None,
        }

    if abs(best["dist"]) <= threshold_close:
        entry = best["value"]
        pad   = 0.3 * atr_30m
        lo    = round(entry - pad, 4)
        hi    = round(entry + pad, 4)
        cond  = ("Orden limite en %.4f (pullback a %s)" if direction == "LONG"
                 else "Orden limite en %.4f (rally a %s)") % (entry, best["name"])
        return {
            "type":         "PULLBACK",
            "quality":      "BUENA" if best["sig"] == 3 else "MODERADA",
            "entry":        entry,
            "zone_lo":      lo,
            "zone_hi":      hi,
            "condition":    cond,
            "rationale":    "%s a %.4fc del precio" % (best["name"], abs(best["dist"])),
            "ref_level":    best,
            "cluster":      None,
            "cluster_stop": None,
        }

    if near_opp and abs(near_opp[0]["dist"]) <= threshold_close and near_opp[0]["sig"] >= 2:
        brk = near_opp[0]
        if direction == "LONG":
            entry = round(brk["value"] + 0.1 * atr_30m, 4)
            lo    = brk["value"]
            hi    = round(brk["value"] + 0.5 * atr_30m, 4)
            cond  = "Ruptura de %s (%.4f) - entrada %.4f" % (brk["name"], brk["value"], entry)
        else:
            entry = round(brk["value"] - 0.1 * atr_30m, 4)
            lo    = round(brk["value"] - 0.5 * atr_30m, 4)
            hi    = brk["value"]
            cond  = "Ruptura bajo %s (%.4f) - entrada %.4f" % (brk["name"], brk["value"], entry)
        return {
            "type":         "BREAKOUT",
            "quality":      "MODERADA",
            "entry":        entry,
            "zone_lo":      lo,
            "zone_hi":      hi,
            "condition":    cond,
            "rationale":    "Setup de ruptura - requiere cierre de vela confirmando",
            "ref_level":    brk,
            "cluster":      None,
            "cluster_stop": None,
        }

    return {
        "type":         "WAIT",
        "quality":      "ESPERAR",
        "entry":        None,
        "zone_lo":      None,
        "zone_hi":      None,
        "condition":    "Niveles mas cercanos a %.4fc - tierra de nadie" % abs(best["dist"]),
        "rationale":    "Esperar que el precio se acerque a %s (%.4f)" % (best["name"], best["value"]),
        "ref_level":    best,
        "cluster":      None,
        "cluster_stop": None,
    }


def compute_entry_zone(session: Session, direction: str, instrument: str = "SBN26") -> dict | None:
    """
    Calcula la zona de entrada recomendada para el instrumento y direccion dados.
    """
    direction = direction.upper()

    price = get_current_price(session, instrument)
    if price is None:
        return None

    # ATR 30m para calibrar umbrales
    rows = session.execute(text("""
        SELECT high, low, close FROM price_bars
        WHERE instrument = :instr AND interval = '30m'
        ORDER BY datetime DESC LIMIT 20
    """), {"instr": instrument}).fetchall()
    if len(rows) < 15:
        return None

    import pandas as pd, numpy as np
    df = pd.DataFrame(rows, columns=["high", "low", "close"])
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.iloc[::-1].reset_index(drop=True)
    prev_c = df["close"].shift(1)
    tr = pd.concat([(df["high"]-df["low"]), (df["high"]-prev_c).abs(), (df["low"]-prev_c).abs()], axis=1).max(axis=1)
    atr_30m = float(tr.rolling(14).mean().dropna().iloc[-1])
    if atr_30m <= 0:
        return None

    # Collect all levels
    try:
        vwap_bands = get_vwap_bands(session, instrument)
    except Exception:
        vwap_bands = {}

    levels_raw, fib_data = _collect_levels(session, instrument, vwap_bands)
    classified = _classify_levels(levels_raw, price, direction, atr_30m)
    rec        = _entry_recommendation(classified, price, direction, atr_30m)

    return {
        "direction":  direction,
        "price":      price,
        "atr_30m":    round(atr_30m, 4),
        "levels":     classified,
        "rec":        rec,
        "prev_day":   _prev_day(session, instrument),
        "fib_data":   fib_data,
    }
