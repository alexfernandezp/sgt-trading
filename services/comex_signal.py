"""
Señal Comex Stat — ritmo exportaciones azúcar Brasil vs año anterior.

Fuente: MAPA/CGDA PDF (Ministério da Agricultura e Pecuária).
  URL: https://www.gov.br/agricultura/pt-br/assuntos/sustentabilidade/agroenergia/acucar-comercio-exterior-brasileiro
  Lag: ~12 días (actualizado alrededor del día 13 de cada mes).

Lógica YoY (Jan-MesActual actual vs mismo periodo año anterior):
  YoY < −5%  → menos azúcar saliendo de Brasil → alcista  → +1 LONG
  YoY > +5%  → más oferta en mercado           → bajista  → -1 SHORT
  Entre ±5%  → neutral → 0
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

YOY_BULLISH_THRESHOLD = -5.0
YOY_BEARISH_THRESHOLD = +5.0


def compute_comex_signal(session) -> dict:
    """
    Señal de exportaciones YoY de azúcar Brasil.

    Primero intenta leer desde DB (datos almacenados por fetch_comex_stat).
    Si no hay datos, descarga el PDF MAPA y parsea directamente.

    Returns dict con:
      signal          : +1 LONG / -1 SHORT / 0 neutral
      bias            : str
      yoy_change_pct  : % cambio YTD actual vs mismo período año anterior
      ytd_curr_t      : toneladas YTD año actual
      ytd_prev_t      : toneladas YTD año anterior
      latest_period   : "Jan-Abr" etc.
      description     : texto resumen
    """
    base = {
        "signal": 0, "bias": "NEUTRAL",
        "yoy_change_pct": None, "ytd_curr_t": None,
        "ytd_prev_t": None, "latest_period": None,
        "description": "Comex Stat: sin datos (ejecutar fetch_comex_stat)",
    }

    if session is None:
        return base

    # ── PDF en vivo primero (comparativa YTD exacta) ──────────────────────────
    # La DB almacena series anuales completas; comparar YTD actual vs año
    # completo anterior da YoY incorrecto. El PDF incluye la fila YTD
    # explícita (e.g. "Jan-Abr 2026 vs Jan-Abr 2025") que es la referencia
    # correcta. Solo caemos a DB si el PDF no está disponible.
    yoy = None
    ytd_curr_t = None
    ytd_prev_t = None
    latest_period = None

    try:
        from ingestion.comex_stat import (
            _get_page_html, _find_pdf_url, _download_pdf, _parse_anual_pdf,
        )
        html = _get_page_html()
        if html:
            pdf_url = _find_pdf_url(html, pattern_words=("ANUAIS",))
            if pdf_url:
                pdf_bytes = _download_pdf(pdf_url)
                if pdf_bytes:
                    parsed = _parse_anual_pdf(pdf_bytes)
                    ytd_map = parsed.get("ytd", {})
                    from datetime import date
                    curr_yr = date.today().year
                    prev_yr = curr_yr - 1
                    ytd_c   = ytd_map.get(curr_yr, {})
                    ytd_p   = ytd_map.get(prev_yr, {})
                    if ytd_c and ytd_p:
                        tc = ytd_c.get("tonnes", 0)
                        tp = ytd_p.get("tonnes", 0)
                        if tp > 0:
                            yoy           = round((tc - tp) / tp * 100, 2)
                            ytd_curr_t    = tc
                            ytd_prev_t    = tp
                            latest_period = ytd_c.get("period") or parsed.get("latest_period")
    except Exception as e:
        logger.warning("comex_signal live PDF: %s", e)

    if yoy is None:
        return base

    # ── Señal ─────────────────────────────────────────────────────────────────
    period_s = latest_period or "YTD"

    if yoy < YOY_BULLISH_THRESHOLD:
        signal = 1; bias = "LONG"
        desc = (f"Exports Brasil {yoy:+.1f}% YoY ({period_s}) — "
                f"ritmo < año anterior → menos oferta → alcista azúcar")
    elif yoy > YOY_BEARISH_THRESHOLD:
        signal = -1; bias = "SHORT"
        desc = (f"Exports Brasil {yoy:+.1f}% YoY ({period_s}) — "
                f"ritmo > año anterior → más oferta en mercado → bajista azúcar")
    else:
        signal = 0; bias = "NEUTRAL"
        desc = (f"Exports Brasil {yoy:+.1f}% YoY ({period_s}) — "
                f"dentro de rango neutral ±5%")

    return {
        "signal":         signal,
        "bias":           bias,
        "yoy_change_pct": yoy,
        "ytd_curr_t":     ytd_curr_t,
        "ytd_prev_t":     ytd_prev_t,
        "latest_period":  latest_period,
        "description":    desc,
    }
