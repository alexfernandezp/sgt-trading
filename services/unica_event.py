"""
UNICA Event Monitor.

Tres capas:
  1. NUEVO REPORTE   — idM actual > idM guardado -> publicacion detectada
  2. WATCHMODE       — >12 dias desde ultimo reporte -> proximo esta semana
  3. LEAKAGE         — ventana 14:00-16:30 BRT, SB mueve >1.5% sin Brent

Estado persistido en data/unica_state.json (idM, fecha, YoY, mix).
"""
import json
import logging
from datetime import datetime, date, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_STATE_FILE    = Path(__file__).parent.parent / "data" / "unica_state.json"
_BRT           = timezone(timedelta(hours=-3))

# UNICA publica ~26 dias despues del corte de datos (position_date).
# Ej: datos hasta 01-may → publicado 27-may (26d despues).
# Siguiente ciclo: corte 16-may → publicacion estimada 11-jun.
_PUB_DELAY_DAYS    = 26    # dias entre position_date y publicacion real
_DATA_PERIOD_DAYS  = 15    # cada quincena = 15 dias
_WATCHMODE_DAYS    = 10    # alertar cuando faltan <=5d para la proxima pub
_LEAKAGE_SB_PCT    = 1.5   # SB move 1h > 1.5% = posible leak
_LEAKAGE_BRENT_MAX = 0.5   # Brent 1h < 0.5% = no es macro


# ── Estado persistente ────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(state: dict):
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(
            json.dumps(state, default=str, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning("unica_event: no se pudo guardar estado: %s", exc)


# ── Deteccion de reporte nuevo ────────────────────────────────────────────────

def check_unica_event() -> dict:
    """
    Compara max(idM) de la web con el estado guardado.

    Returns:
      status           : "NEW" | "WATCHMODE" | "RECENT" | "NONE"
      new_report       : dict con datos parseados (solo si NEW)
      days_since_last  : int
      last_position_date: date | None
      yoy_direction    : "BEARISH" | "BULLISH" | "NEUTRAL" | None
      alert_lines      : list[str] para imprimir
    """
    from ingestion.unica import scrape_latest_idm, download_pdf, parse_unica_pdf

    state = _load_state()
    result = {
        "status":             "NONE",
        "new_report":         None,
        "days_since_last":    None,
        "last_position_date": None,
        "yoy_direction":      None,
        "alert_lines":        [],
    }

    # Dias desde ultimo reporte conocido
    last_pos_str = state.get("last_position_date")
    if last_pos_str:
        try:
            last_pos = date.fromisoformat(last_pos_str)
            result["last_position_date"] = last_pos
            result["days_since_last"]    = (date.today() - last_pos).days
        except Exception:
            pass

    # Obtener idM actual de la web
    try:
        current_idm = scrape_latest_idm()
    except Exception as exc:
        logger.warning("unica_event: scrape fallido: %s", exc)
        return _apply_watchmode(result, state)

    if current_idm is None:
        return _apply_watchmode(result, state)

    stored_idm = state.get("last_idm")

    # ── NUEVO REPORTE ─────────────────────────────────────────────────────────
    if stored_idm is None or current_idm > int(stored_idm):
        try:
            pdf  = download_pdf(current_idm)
            data = parse_unica_pdf(pdf) if pdf else None
        except Exception as exc:
            logger.warning("unica_event: error parseando reporte: %s", exc)
            data = None

        if data:
            new_state = {
                "last_idm":               current_idm,
                "last_detected_at":       datetime.now(timezone.utc).isoformat(),
                "last_position_date":     str(data.get("position_date", date.today())),
                "last_yoy_sugar_pct":     data.get("yoy_sugar_pct"),
                "last_yoy_quint_pct":     None,
                "last_sugar_cumul_mt":    data.get("sugar_cumulative_mt"),
                "last_cane_cumul_mt":     data.get("cane_cumulative_mt"),
                "last_mix_ethanol_pct":   data.get("mix_ethanol_pct"),       # acumulado Tabla 1
                "last_mix_ethanol_pct_q": data.get("mix_ethanol_pct_q"),     # quinzenal Tabla 2
                "last_atr_kg_t_q":        data.get("atr_kg_t_q"),            # quinzenal Tabla 2
                "last_safra":             data.get("safra"),
                "last_quinzena":          data.get("quinzena_num"),
                "last_ref_month":         data.get("ref_month"),
            }
            _save_state(new_state)

            yoy  = data.get("yoy_sugar_pct") or 0
            # Usar mix quinzenal (Tabla 2) si disponible; fallback a acumulado (Tabla 1)
            emix_display = data.get("mix_ethanol_pct_q") or data.get("mix_ethanol_pct")
            mix_sugar = round(100 - emix_display, 1) if emix_display else None
            eth_ml    = data.get("ethanol_total_ml") or 0
            cum_mt    = data.get("sugar_cumulative_mt") or 0
            q_mt      = data.get("sugar_quinzenal_mt") or 0
            pos_date  = data.get("position_date", date.today())
            safra     = data.get("safra", "?")
            qn        = data.get("quinzena_num", "?")
            month_n   = data.get("ref_month", "?")

            yoy_dir = "BEARISH" if yoy > 5 else ("BULLISH" if yoy < -5 else "NEUTRAL")
            impact  = ("-> Presion BAJISTA esperada (-2% a -4% en sesion)" if yoy_dir == "BEARISH"
                       else ("-> Presion ALCISTA esperada (+1% a +2% en sesion)" if yoy_dir == "BULLISH"
                             else "-> Impacto neutro"))

            lines = [
                "=" * 72,
                "  *** UNICA PUBLICADO [%s] — %s ***" % (pos_date, yoy_dir),
                "  Safra %s  |  %sa quinzena mes %s" % (safra, qn, month_n),
                "  Azucar acumulada : %+.1f%% YoY  (%.3f Mt)" % (yoy, cum_mt),
                "  Azucar quinzenal : %.3f Mt" % q_mt,
                "  Mix azucar       : %.1f%%  |  Etanol total: %.0f Ml" % (
                    mix_sugar or 0, eth_ml),
                "  %s" % impact,
                "=" * 72,
            ]
            result.update({
                "status":         "NEW",
                "new_report":     data,
                "days_since_last": 0,
                "yoy_direction":  yoy_dir,
                "alert_lines":    lines,
            })
            return result

    return _apply_watchmode(result, state)


def _apply_watchmode(result: dict, state: dict) -> dict:
    """
    Aplica logica WATCHMODE/RECENT usando la fecha de PUBLICACION estimada,
    no el position_date (corte de datos).

    Logica de fechas:
      pub_date_est      = position_date + PUB_DELAY_DAYS (~26d)
      next_position     = position_date + DATA_PERIOD_DAYS (15d)
      next_pub_est      = next_position + PUB_DELAY_DAYS
      days_since_pub    = hoy - pub_date_est
      days_to_next_pub  = next_pub_est - hoy  (negativo = overdue)
    """
    pos_str = state.get("last_position_date")
    yoy     = state.get("last_yoy_sugar_pct")
    yoy_s   = ("%+.1f%% YoY" % yoy) if yoy is not None else "sin dato"

    if not pos_str:
        return result

    try:
        pos_date      = date.fromisoformat(pos_str)
        pub_date_est  = pos_date + timedelta(days=_PUB_DELAY_DAYS)
        next_pos      = pos_date + timedelta(days=_DATA_PERIOD_DAYS)
        next_pub_est  = next_pos + timedelta(days=_PUB_DELAY_DAYS)

        days_since_pub   = (date.today() - pub_date_est).days
        days_to_next_pub = (next_pub_est - date.today()).days

        result["days_since_last"]    = days_since_pub
        result["last_position_date"] = pos_date

        overdue = days_to_next_pub < 0

        if days_to_next_pub <= _WATCHMODE_DAYS:
            result["status"] = "WATCHMODE"
            if overdue:
                timing = "OVERDUE %dd" % abs(days_to_next_pub)
            else:
                timing = "en ~%dd" % days_to_next_pub
            result["alert_lines"] = [
                "  [!] UNICA DUE [%s] — pub. estimada: ~%s  [%s]" % (
                    timing, next_pub_est.strftime("%d-%b"), yoy_s),
                "  Datos: hasta ~%s | Pub anterior: ~%s (%dd)" % (
                    next_pos.strftime("%d-%b"),
                    pub_date_est.strftime("%d-%b"),
                    days_since_pub),
                "  Vigilar 14:00-16:30 BRT — SB cae >1.5%% sin Brent = posible leakage.",
            ]
        else:
            result["status"] = "RECENT"
            result["alert_lines"] = [
                "  UNICA: pub ~%s (%dd)  datos hasta %s  [%s]  |  Proximo: ~%s" % (
                    pub_date_est.strftime("%d-%b"),
                    days_since_pub,
                    pos_date.strftime("%d-%b"),
                    yoy_s,
                    next_pub_est.strftime("%d-%b")),
            ]
    except Exception as exc:
        logger.warning("unica_event _apply_watchmode: %s", exc)

    return result


# ── Deteccion de leakage intraday ─────────────────────────────────────────────

def check_leakage_window() -> dict:
    """
    Detecta posible leakage de datos UNICA en la ventana 14:00-16:30 BRT.

    Criterio: SB mueve >1.5% en ultima hora Y Brent <0.5% en misma ventana.
    Si se cumple -> posible informacion filtrada antes de publicacion oficial (16:00 BRT).

    Returns:
      in_window        : bool — estamos en la ventana de publicacion
      leakage_detected : bool
      sb_move_pct      : float | None
      brent_move_pct   : float | None
      alert_lines      : list[str]
    """
    result = {
        "in_window":        False,
        "leakage_detected": False,
        "sb_move_pct":      None,
        "brent_move_pct":   None,
        "alert_lines":      [],
    }

    now_brt = datetime.now(_BRT)
    # Ventana activa: 14:00 a 19:00 BRT (2h antes pub + 3h digestion)
    in_window = 14 <= now_brt.hour < 19
    result["in_window"] = in_window
    if not in_window:
        return result

    # Fase de la ventana
    if now_brt.hour < 16:
        phase = "PRE-PUBLICACION"
    elif now_brt.hour < 17:
        phase = "PUBLICACION"
    else:
        phase = "DIGESTION"

    try:
        import yfinance as yf

        def _move_1h(ticker: str) -> Optional[float]:
            df = yf.download(ticker, period="1d", interval="5m",
                             progress=False, auto_adjust=True)
            if df is None or len(df) < 12:
                return None
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
            closes = df["Close"].dropna()
            if len(closes) < 12:
                return None
            return float((closes.iloc[-1] / closes.iloc[-12] - 1) * 100)

        sb_move    = _move_1h("SB=F")
        brent_move = _move_1h("BZ=F")

        result["sb_move_pct"]    = round(sb_move,    2) if sb_move    is not None else None
        result["brent_move_pct"] = round(brent_move, 2) if brent_move is not None else None

        sb_s  = ("%+.2f%%" % sb_move)    if sb_move    is not None else "N/D"
        bz_s  = ("%+.2f%%" % brent_move) if brent_move is not None else "N/D"
        t_s   = now_brt.strftime("%H:%M")

        if (sb_move is not None and brent_move is not None
                and abs(sb_move) >= _LEAKAGE_SB_PCT
                and abs(brent_move) <= _LEAKAGE_BRENT_MAX):
            result["leakage_detected"] = True
            direction = "BAJISTA" if sb_move < 0 else "ALCISTA"
            result["alert_lines"] = [
                "  [!!!] POSIBLE LEAKAGE UNICA [%s BRT — %s]" % (t_s, phase),
                "  SB 1h: %s  |  Brent 1h: %s  -> SB mueve sin Brent" % (sb_s, bz_s),
                "  Direccion: %s  — considerar posicion antes de 16:00 BRT" % direction,
            ]
        else:
            result["alert_lines"] = [
                "  Ventana UNICA [%s BRT — %s]  SB_1h=%s  Brent_1h=%s" % (
                    t_s, phase, sb_s, bz_s),
            ]

    except Exception as exc:
        logger.warning("check_leakage_window: %s", exc)

    return result
