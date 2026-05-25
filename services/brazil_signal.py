"""
Senal fundamental A4: produccion sucroalcooleira de Brasil (MAPA).

Dos sub-senales:
  A4a — Ritmo YoY de caña molida (vs misma quincena temporada anterior).
         Caña↓ YoY  → oferta menor → sesgo LONG
         Caña↑ YoY  → oferta mayor → sesgo SHORT

  A4b — Mix azucar vs etanol (sugar_mix_pct, 0-100).
         Alta proporcion de azucar → mas oferta exportable → presion bajista.
         Baja proporcion (etanol) → menos azucar disponible → presion alcista.
         Benchmark: 45% (media historica de la temporada).

Score combinado A4: promedio ponderado A4a(60%) + A4b(40%).
Rango: -1 (muy bajista) → +1 (muy alcista).
"""
import logging
from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)

SUGAR_MIX_NEUTRAL   = 45.0   # % — benchmark historico
SUGAR_MIX_RANGE     = 10.0   # +/- % para saturacion de la senal
YOY_RANGE_PCT       = 10.0   # +/- % para saturacion YoY


def _fetch_yoy(session: Session) -> dict | None:
    """
    Busca el ultimo dato disponible y el mismo fortnight_seq de la temporada anterior.
    Devuelve dict con cane_current, cane_prev, yoy_pct o None.
    """
    rows = session.execute(text("""
        SELECT harvest_year, fortnight_seq, cane_crushed_t, sugar_t,
               ethanol_total_m3, sugar_mix_pct, report_date
        FROM brazil_production
        WHERE cane_crushed_t IS NOT NULL
        ORDER BY report_date DESC
        LIMIT 1
    """)).fetchall()

    if not rows:
        return None

    latest = rows[0]
    curr_year, curr_seq = latest[0], latest[1]

    # Misma quincena del año anterior
    prev_years = curr_year.split("-")
    try:
        prev_harvest = f"{int(prev_years[0])-1}-{int(prev_years[1])-1}"
    except Exception:
        return None

    prev_row = session.execute(text("""
        SELECT cane_crushed_t, sugar_t, ethanol_total_m3, sugar_mix_pct
        FROM brazil_production
        WHERE harvest_year = :hy AND fortnight_seq = :seq
        LIMIT 1
    """), {"hy": prev_harvest, "seq": curr_seq}).fetchone()

    return {
        "report_date":    str(latest[6]),
        "harvest_year":   curr_year,
        "fortnight_seq":  curr_seq,
        "cane_current":   float(latest[2]),
        "sugar_current":  float(latest[3]) if latest[3] else None,
        "ethanol_current": float(latest[4]) if latest[4] else None,
        "sugar_mix_pct":  float(latest[5]) if latest[5] else None,
        "cane_prev":      float(prev_row[0]) if prev_row and prev_row[0] else None,
        "sugar_prev":     float(prev_row[1]) if prev_row and prev_row[1] else None,
        "prev_harvest":   prev_harvest,
    }


def compute_brazil_signal(session: Session) -> dict | None:
    """
    Calcula la senal fundamental A4 basada en datos MAPA.

    Devuelve:
      signal_a4    : float en [-1, +1]  (+1 = muy alcista)
      signal_a4a   : sub-senal YoY caña
      signal_a4b   : sub-senal mix azucar/etanol
      bias         : "LONG" / "SHORT" / "NEUTRAL"
      description  : texto para el score card
      data         : dict con los valores crudos
    """
    data = _fetch_yoy(session)
    if data is None:
        logger.warning("brazil_signal: sin datos en brazil_production")
        return None

    # --- A4a: YoY caña ---
    a4a = 0.0
    yoy_pct = None
    if data["cane_prev"] and data["cane_prev"] > 0:
        yoy_pct = (data["cane_current"] - data["cane_prev"]) / data["cane_prev"] * 100
        # Caña↑ YoY = más oferta = bearish → senal negativa
        a4a = -max(-1.0, min(1.0, yoy_pct / YOY_RANGE_PCT))

    # --- A4b: mix azucar ---
    a4b = 0.0
    mix = data.get("sugar_mix_pct")
    if mix is not None:
        # Mix alto (>45%) = mas azucar = presion bajista → senal negativa
        deviation = mix - SUGAR_MIX_NEUTRAL
        a4b = -max(-1.0, min(1.0, deviation / SUGAR_MIX_RANGE))

    # --- Combinada ---
    if data["cane_prev"] is not None:
        signal_a4 = round(0.60 * a4a + 0.40 * a4b, 3)
    else:
        signal_a4 = round(a4b, 3)   # solo mix si no hay YoY

    if signal_a4 >= 0.20:
        bias = "LONG"
    elif signal_a4 <= -0.20:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    # Texto descriptivo
    yoy_str = f"{yoy_pct:+.1f}% YoY" if yoy_pct is not None else "sin dato YoY"
    mix_str  = f"mix {mix:.1f}%" if mix is not None else "sin dato mix"
    desc = (
        f"Caña Brasil {yoy_str} ({data['harvest_year']} Q{data['fortnight_seq']}) | "
        f"{mix_str} azúcar/etanol | A4={signal_a4:+.2f} [{bias}]"
    )

    return {
        "signal_a4":  signal_a4,
        "signal_a4a": round(a4a, 3),
        "signal_a4b": round(a4b, 3),
        "yoy_pct":    round(yoy_pct, 2) if yoy_pct is not None else None,
        "bias":       bias,
        "description": desc,
        "data":        data,
    }
