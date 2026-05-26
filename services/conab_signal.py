"""
Señal CONAB — Boletim da Safra de Cana-de-Açúcar.

Lógica de dos capas:
  1. Revisión intra-temporada (vs levantamento anterior):
       revision_sugar > +2% → más oferta → SHORT (-1)
       revision_sugar < -2% → menos oferta → LONG (+1)
     Este es el driver de mercado más inmediato.

  2. YoY (temporada actual vs anterior):
       yoy_sugar > +3% → más oferta global → SHORT (-1)
       yoy_sugar < -3% → menos oferta global → LONG (+1)
     Confirma o mitiga la revisión intra-season.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Umbrales de señal
REVISION_BULLISH_THRESHOLD = -2.0   # revisión baja → menos oferta → LONG
REVISION_BEARISH_THRESHOLD = +2.0   # revisión sube → más oferta → SHORT
YOY_BULLISH_THRESHOLD      = -3.0
YOY_BEARISH_THRESHOLD      = +3.0


def compute_conab_signal(session) -> dict:
    """
    Lee el levantamento más reciente de la DB y genera señal.

    Returns dict:
      signal         : +1 / -1 / 0
      bias           : LONG / SHORT / NEUTRAL
      season         : "2025/26"
      levantamento   : int
      pub_date       : str
      sugar_total_mt : float
      revision_pct   : float (vs lev anterior, None si 1er lev)
      yoy_pct        : float
      description    : str
    """
    base = {
        "signal": 0, "bias": "NEUTRAL",
        "season": None, "levantamento": None, "pub_date": None,
        "cane_total_mt": None, "sugar_total_mt": None,
        "revision_sugar_pct": None, "yoy_sugar_pct": None,
        "sp_sugar_mt": None,
        "description": "CONAB: sin datos (ejecutar fetch_conab.py)",
    }

    if session is None:
        return base

    try:
        from ingestion.conab_cana import get_latest_conab
        data = get_latest_conab(session)
    except Exception as e:
        logger.warning("conab_signal: %s", e)
        base["description"] = f"CONAB: error — {e}"
        return base

    if data is None:
        return base

    base.update({
        "season":         data.get("season"),
        "levantamento":   data.get("levantamento"),
        "pub_date":       data.get("pub_date"),
        "cane_total_mt":  data.get("cane_total_mt"),
        "sugar_total_mt": data.get("sugar_total_mt"),
        "revision_sugar_pct": data.get("revision_sugar_pct"),
        "yoy_sugar_pct":  data.get("yoy_sugar_pct"),
        "sp_sugar_mt":    data.get("sp_sugar_mt"),
    })

    rev  = data.get("revision_sugar_pct")
    yoy  = data.get("yoy_sugar_pct")
    lev  = data.get("levantamento", 0)
    szn  = data.get("season", "?")
    sug  = data.get("sugar_total_mt")
    sug_s = f"{sug:.1f}Mt" if sug else "?"

    # ── Señal principal: revisión intra-temporada ──────────────────────────
    signal = 0
    bias = "NEUTRAL"
    reasons = []

    if rev is not None and lev > 1:
        if rev <= REVISION_BULLISH_THRESHOLD:
            signal += 1
            reasons.append(f"revisión {rev:+.1f}% ↓ LONG")
        elif rev >= REVISION_BEARISH_THRESHOLD:
            signal -= 1
            reasons.append(f"revisión {rev:+.1f}% ↑ SHORT")
        else:
            reasons.append(f"revisión {rev:+.1f}% neutral")
    elif lev == 1:
        reasons.append("1er lev — sin revisión previa")

    # ── Señal secundaria: YoY ─────────────────────────────────────────────
    if yoy is not None:
        if yoy <= YOY_BULLISH_THRESHOLD:
            signal = max(signal, 0) + 1 if signal >= 0 else signal
            reasons.append(f"YoY {yoy:+.1f}% ↓ bullish")
        elif yoy >= YOY_BEARISH_THRESHOLD:
            signal = min(signal, 0) - 1 if signal <= 0 else signal
            reasons.append(f"YoY {yoy:+.1f}% ↑ bearish")
        else:
            reasons.append(f"YoY {yoy:+.1f}% neutral")

    signal = max(-1, min(1, signal))

    if signal > 0:
        bias = "LONG"
    elif signal < 0:
        bias = "SHORT"

    desc = (
        f"CONAB {szn} {lev}º lev — azúcar {sug_s} "
        + " | ".join(reasons)
    )

    return {**base, "signal": signal, "bias": bias, "description": desc}
