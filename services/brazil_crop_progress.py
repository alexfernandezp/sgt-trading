"""
Brazil Crop Progress — modelo fundamental independiente para monitoreo CS.

Combina tres fuentes:
  1. unica_biweekly    — serie quinzenal UNICA 2010→presente (Excel backfill + PDF fresco)
  2. brazil_production — levantamentos MAPA/CONAB acumulados 2026/27
  3. brazil_signal.py  — señales A4a (YoY caña) + A4b (mix% azúcar)

Señales computadas:
  crushing_pace_z     — z-score moagem CS acumulada vs media histórica misma quincena
  sugar_mix_pct_z     — z-score mix% azúcar vs media histórica misma quincena
  atr_delta           — ATR actual - media histórica misma quincena (kg/ton)
  eth_hid_pace_z      — z-score etanol hidratado acumulado vs media histórica
  yoy_cane_pct        — variación % YoY moagem CS (vs safra anterior misma quincena)
  projected_sugar_mt  — proyección full-year (de unica.py, si disponible)
  mapa_levantamento   — último valor MAPA caña/azúcar/etanol
  season_progress_pct — % temporada completado según UNICA

Uso:
    from services.brazil_crop_progress import compute_crop_progress
    signals = compute_crop_progress(session)
    # signals = {'crushing_pace_z': 0.42, 'sugar_mix_pct_z': -1.1, ...}
"""
import logging
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

# Años a incluir en baseline histórico (se excluyen años de eventos extremos si se desea)
_BASELINE_MIN_YEAR = 2012
_BASELINE_MAX_YEAR = 2024   # última safra completa disponible en Excel

# Número mínimo de años en baseline para calcular z-score con confianza
_MIN_BASELINE_OBS = 5


def _zscore(value: float, hist_values: list[float]) -> Optional[float]:
    """Z-score de value respecto a hist_values. Retorna None si insuficiente muestra."""
    n = len(hist_values)
    if n < _MIN_BASELINE_OBS or value is None:
        return None
    mean = sum(hist_values) / n
    variance = sum((x - mean) ** 2 for x in hist_values) / n
    std = variance ** 0.5
    if std < 1e-9:
        return 0.0
    return round((value - mean) / std, 2)


def _pct_rank(value: float, hist_values: list[float]) -> Optional[float]:
    """Percentil (0-100) de value dentro de hist_values."""
    if not hist_values or value is None:
        return None
    below = sum(1 for x in hist_values if x <= value)
    return round(100 * below / len(hist_values), 1)


def _get_unica_biweekly(session, region: str = "CS") -> list[dict]:
    """Lee toda la serie quinzenal de unica_biweekly para la región dada."""
    from sqlalchemy import text
    rows = session.execute(
        text("""
            SELECT safra, quinzena_date, cane_crushed_t, sugar_t,
                   ethanol_hidratado_m3, ethanol_total_m3,
                   atr_kg_ton, sugar_mix_pct, eth_mix_pct
            FROM unica_biweekly
            WHERE region = :reg
            ORDER BY quinzena_date
        """),
        {"reg": region},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def _get_mapa_latest(session) -> Optional[dict]:
    """Retorna el levantamiento MAPA más reciente disponible."""
    try:
        from sqlalchemy import text
        row = session.execute(
            text("""
                SELECT report_date, harvest_year, fortnight_seq,
                       cane_crushed_t_cumulative, sugar_t_cumulative,
                       ethanol_total_m3_cumulative, sugar_mix_pct,
                       report_issue_date, report_revision_seq
                FROM brazil_production
                ORDER BY report_date DESC, report_issue_date DESC
                LIMIT 1
            """),
        ).fetchone()
        if row:
            m = row._mapping

            def _to_mt(v):
                return round(float(v) / 1e6, 2) if v is not None else None

            return {
                "ref_date":          m["report_date"],
                "harvest_year":      m["harvest_year"],
                "fortnight_seq":     m["fortnight_seq"],
                "total_cane_mt":     _to_mt(m["cane_crushed_t_cumulative"]),
                "sugar_mt":          _to_mt(m["sugar_t_cumulative"]),
                "ethanol_total_mm3": _to_mt(m["ethanol_total_m3_cumulative"]),
                "sugar_mix_pct":     float(m["sugar_mix_pct"]) if m["sugar_mix_pct"] is not None else None,
                "revision_num":      m["report_revision_seq"],
                "issue_date":        m["report_issue_date"],
            }
    except Exception as e:
        logger.warning("brazil_crop_progress: MAPA query fallida: %s", e)
    return None


def compute_crop_progress(session, region: str = "CS") -> dict:
    """
    Calcula todas las señales Brazil Crop Progress para la quincena más reciente.

    Returns dict:
      latest_safra, latest_quinzena_date, season_progress_pct,
      crushing_pace_z, crushing_pace_pct_rank,
      sugar_mix_pct_z, sugar_mix_pct_rank,
      atr_delta, atr_current, atr_hist_mean,
      eth_hid_pace_z, eth_hid_pace_pct_rank,
      yoy_cane_pct,
      projected_sugar_mt,
      mapa_latest,
      data_age_days, baseline_years
    """
    rows = _get_unica_biweekly(session, region)
    if not rows:
        logger.warning("brazil_crop_progress: sin datos en unica_biweekly region=%s", region)
        return {"error": "no_data"}

    # Última fila (quincena más reciente)
    latest = rows[-1]
    latest_date: date = latest["quinzena_date"]
    latest_safra: str = latest["safra"]

    # Identificar (mes, día) de la quincena para comparar vs histórico
    ref_month = latest_date.month
    ref_day   = latest_date.day
    # Quincena 1 si día <= 16, quincena 2 si día > 16
    quinzena_half = 1 if ref_day <= 16 else 2

    data_age_days = (date.today() - latest_date).days

    # ---------------------------------------------------------------------------
    # Baseline: misma quincena (mismo mes + mitad) en años anteriores
    # ---------------------------------------------------------------------------
    baseline_rows = []
    for r in rows:
        if r["safra"] == latest_safra:
            continue   # excluir año actual del baseline
        d: date = r["quinzena_date"]
        if d.month != ref_month:
            continue
        half = 1 if d.day <= 16 else 2
        if half != quinzena_half:
            continue
        # Filtro por rango de años baseline
        try:
            year_start = int(r["safra"].split("-")[0])
        except Exception:
            continue
        if not (_BASELINE_MIN_YEAR <= year_start <= _BASELINE_MAX_YEAR):
            continue
        baseline_rows.append(r)

    baseline_years = sorted({r["safra"] for r in baseline_rows})

    def _hist(field):
        return [r[field] for r in baseline_rows if r.get(field) is not None]

    # ---------------------------------------------------------------------------
    # Señales
    # ---------------------------------------------------------------------------
    result: dict = {
        "latest_safra":       latest_safra,
        "latest_quinzena_date": str(latest_date),
        "season_progress_pct":  None,
        "crushing_pace_z":      None,
        "crushing_pace_pct_rank": None,
        "sugar_mix_pct_z":      None,
        "sugar_mix_pct_rank":   None,
        "atr_delta":            None,
        "atr_current":          None,
        "atr_hist_mean":        None,
        "eth_hid_pace_z":       None,
        "eth_hid_pace_pct_rank": None,
        "yoy_cane_pct":         None,
        "projected_sugar_mt":   None,
        "mapa_latest":          None,
        "data_age_days":        data_age_days,
        "baseline_years":       len(baseline_years),
    }

    # Crushing pace z-score
    cane_cur = latest.get("cane_crushed_t")
    hist_cane = _hist("cane_crushed_t")
    if cane_cur and hist_cane:
        result["crushing_pace_z"]        = _zscore(cane_cur, hist_cane)
        result["crushing_pace_pct_rank"] = _pct_rank(cane_cur, hist_cane)

    # Sugar mix %
    smix_cur = latest.get("sugar_mix_pct")
    hist_smix = _hist("sugar_mix_pct")
    if smix_cur and hist_smix:
        result["sugar_mix_pct_z"]    = _zscore(smix_cur, hist_smix)
        result["sugar_mix_pct_rank"] = _pct_rank(smix_cur, hist_smix)

    # ATR delta
    atr_cur = latest.get("atr_kg_ton")
    hist_atr = _hist("atr_kg_ton")
    if atr_cur and hist_atr:
        atr_mean = sum(hist_atr) / len(hist_atr)
        result["atr_current"]   = round(float(atr_cur), 2)
        result["atr_hist_mean"] = round(atr_mean, 2)
        result["atr_delta"]     = round(float(atr_cur) - atr_mean, 2)

    # Ethanol hidratado pace z-score
    eth_h_cur = latest.get("ethanol_hidratado_m3")
    hist_eth_h = _hist("ethanol_hidratado_m3")
    if eth_h_cur and hist_eth_h:
        result["eth_hid_pace_z"]        = _zscore(eth_h_cur, hist_eth_h)
        result["eth_hid_pace_pct_rank"] = _pct_rank(eth_h_cur, hist_eth_h)

    # YoY caña: safra anterior, misma quincena
    try:
        y1, y2 = (int(p) for p in latest_safra.split("-"))
        prev_safra = f"{y1-1}-{y2-1}"
        prev_rows = [
            r for r in rows
            if r["safra"] == prev_safra
            and r["quinzena_date"].month == ref_month
            and (1 if r["quinzena_date"].day <= 16 else 2) == quinzena_half
        ]
        if prev_rows and cane_cur:
            prev_cane = prev_rows[0].get("cane_crushed_t")
            if prev_cane and prev_cane > 0:
                result["yoy_cane_pct"] = round((cane_cur / prev_cane - 1) * 100, 1)
    except Exception:
        pass

    # Season progress (usar _SEASON_PROGRESS_PCT de unica.py)
    try:
        from ingestion.unica import _SEASON_PROGRESS_PCT
        prog = _SEASON_PROGRESS_PCT.get((ref_month, quinzena_half))
        result["season_progress_pct"] = prog
    except Exception:
        pass

    # Proyección full-year (requiere UNICA fresco)
    try:
        from ingestion.unica import get_latest_unica
        unica_data = get_latest_unica()
        if unica_data:
            result["projected_sugar_mt"] = unica_data.get("projected_full_year_mt")
    except Exception:
        pass

    # MAPA último levantamiento
    result["mapa_latest"] = _get_mapa_latest(session)

    return result


def format_crop_progress_report(signals: dict) -> str:
    """Formatea las señales como reporte de texto para log/dashboard."""
    if signals.get("error"):
        return f"Brazil Crop Progress: sin datos ({signals['error']})"

    def _z(key, width=6):
        """Formatea un z-score que puede ser None sin reventar."""
        v = signals.get(key)
        return f"{v:>{width}.2f}" if isinstance(v, (int, float)) else f"{'N/D':>{width}}"

    def _v(key, default="N/D"):
        v = signals.get(key)
        return default if v is None else v

    lines = [
        f"=== Brazil Crop Progress ===",
        f"Safra: {_v('latest_safra', '?')}  "
        f"Quincena: {_v('latest_quinzena_date', '?')}  "
        f"({_v('data_age_days', '?')}d old)",
        f"Season progress: {_v('season_progress_pct', '?')}%  "
        f"(baseline {signals.get('baseline_years', 0)} años)",
        "",
        f"Crushing pace z: {_z('crushing_pace_z')}  "
        f"pct_rank: {_v('crushing_pace_pct_rank')}",
        f"Sugar mix    z: {_z('sugar_mix_pct_z')}  "
        f"pct_rank: {_v('sugar_mix_pct_rank')}",
        f"ATR delta (kg/t): {_v('atr_delta')}  "
        f"(curr={_v('atr_current')}  hist={_v('atr_hist_mean')})",
        f"Eth.hid  pace z: {_z('eth_hid_pace_z')}  "
        f"pct_rank: {_v('eth_hid_pace_pct_rank')}",
        f"YoY caña: {_v('yoy_cane_pct')}%",
        f"Projected sugar (CS full-year): {_v('projected_sugar_mt')} Mt",
    ]

    mapa = signals.get("mapa_latest")
    if mapa:
        lines += [
            "",
            f"MAPA último levantamiento ({mapa.get('ref_date', '?')} rev{mapa.get('revision_num', 0)}):",
            f"  Caña: {mapa.get('total_cane_mt', 'N/D')} Mt  "
            f"Azúcar: {mapa.get('sugar_mt', 'N/D')} Mt  "
            f"Etanol: {mapa.get('ethanol_total_mm3', 'N/D')} Mm³",
        ]

    return "\n".join(str(l) for l in lines)
