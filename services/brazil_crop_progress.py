"""
Brazil Crop Progress — modelo fundamental ALL-IN para safra Centro-Sul.

Fuentes:
  unica_biweekly  — serie per-quincena (Excel backfill 2010-2025 + PDF fresco de-acumulado)
  brazil_production — levantamentos MAPA/CONAB

Señales computadas sobre acumulado-a-fecha via cumsum del neto:
  A. Ritmo molienda acumulado (modified_z, percentile_rank, conviction, yoy)
  B. Ritmo azúcar acumulado  (idem)
  C. Mix azúcar/etanol + descomposición YoY (efecto volumen + ATR + mix)
  D. ATR delta vs histórico
  E. Etanol share hidratado/anidro
  F. Proyección full-year analógica (mediana de ratios, banda p25-p75)
  G. Predicción próxima quincena (mediana baseline × pace_ratio)
  H. Bias direccional ICE No.11 (−1..+1, solo output)

Uso:
    from services.brazil_crop_progress import compute_crop_progress
    result = compute_crop_progress(session)
"""
import logging
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

_BASELINE_SAFRAS_MIN = 2010
_BASELINE_SAFRAS_MAX = 2024   # última safra completa en Excel


def _get_unica_biweekly(session, region: str = "CS") -> list[dict]:
    from sqlalchemy import text
    rows = session.execute(
        text("""
            SELECT safra, quinzena_date, cane_crushed_t, sugar_t,
                   ethanol_hidratado_m3, ethanol_anidro_m3, ethanol_total_m3,
                   atr_kg_ton, sugar_mix_pct, eth_mix_pct
            FROM unica_biweekly
            WHERE region = :reg
            ORDER BY quinzena_date
        """),
        {"reg": region},
    ).fetchall()

    _numeric = (
        "cane_crushed_t", "sugar_t", "ethanol_hidratado_m3", "ethanol_anidro_m3",
        "ethanol_total_m3", "atr_kg_ton", "sugar_mix_pct", "eth_mix_pct",
    )
    out = []
    for r in rows:
        d = dict(r._mapping)
        for f in _numeric:
            if d.get(f) is not None:
                d[f] = float(d[f])
        # Normalizar mix on-read: si ≤1 asumir fracción → ×100
        for mf in ("sugar_mix_pct", "eth_mix_pct"):
            v = d.get(mf)
            if v is not None and 0 < v <= 1.0:
                d[mf] = round(v * 100, 4)
        out.append(d)
    return out


def _get_mapa_latest(session) -> Optional[dict]:
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


def _cumsum_by_safra(rows: list[dict], field: str) -> dict:
    """
    Calcula acumulado-a-fecha por safra.
    Retorna {(safra, seq): cum_value} usando cumsum del neto ordenado por seq.
    """
    from ingestion.unica import season_fortnight_seq
    from collections import defaultdict

    by_safra = defaultdict(list)
    for r in rows:
        seq = season_fortnight_seq(r["quinzena_date"])
        if seq is None:
            continue
        val = r.get(field)
        if val is None:
            continue
        by_safra[r["safra"]].append((seq, val))

    result = {}
    for safra, items in by_safra.items():
        items.sort(key=lambda x: x[0])
        cum = 0.0
        for seq, net in items:
            cum += net
            result[(safra, seq)] = cum
    return result


def _median(values: list) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return (s[mid - 1] + s[mid]) / 2.0 if n % 2 == 0 else s[mid]


def _percentile(values: list, p: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    idx = p / 100 * (len(s) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (idx - lo) * (s[hi] - s[lo])


def compute_crop_progress(session, region: str = "CS") -> dict:
    """
    Calcula todas las señales Brazil Crop Progress ALL-IN para la safra vigente.
    """
    from services.stats_utils import robust_stats
    from ingestion.unica import season_fortnight_seq

    rows = _get_unica_biweekly(session, region)
    if not rows:
        logger.warning("brazil_crop_progress: sin datos en unica_biweekly region=%s", region)
        return {"error": "no_data"}

    # ── Identificar safra vigente y última quincena con datos ─────────────────
    latest = next(
        (r for r in reversed(rows) if r.get("cane_crushed_t") is not None),
        rows[-1],
    )
    latest_date: date = latest["quinzena_date"]
    latest_safra: str = latest["safra"]
    latest_seq = season_fortnight_seq(latest_date)

    if latest_seq is None:
        logger.warning("brazil_crop_progress: seq no reconocido para %s", latest_date)
        return {"error": "bad_date"}

    data_age_days = (date.today() - latest_date).days

    # ── Cumsum por safra para caña y azúcar ──────────────────────────────────
    cum_cane_map  = _cumsum_by_safra(rows, "cane_crushed_t")
    cum_sugar_map = _cumsum_by_safra(rows, "sugar_t")
    cum_eth_t_map = _cumsum_by_safra(rows, "ethanol_total_m3")
    cum_eth_h_map = _cumsum_by_safra(rows, "ethanol_hidratado_m3")
    cum_eth_a_map = _cumsum_by_safra(rows, "ethanol_anidro_m3")

    cum_cane_cur  = cum_cane_map.get((latest_safra, latest_seq))
    cum_sugar_cur = cum_sugar_map.get((latest_safra, latest_seq))
    cum_eth_t_cur = cum_eth_t_map.get((latest_safra, latest_seq))
    cum_eth_h_cur = cum_eth_h_map.get((latest_safra, latest_seq))

    # ── Baseline: mismo seq en safras históricas ──────────────────────────────
    def _baseline_cum(cum_map: dict, seq: int) -> list[float]:
        vals = []
        for safra, s in cum_map:
            if s != seq:
                continue
            try:
                y = int(safra.split("-")[0])
            except Exception:
                continue
            if _BASELINE_SAFRAS_MIN <= y <= _BASELINE_SAFRAS_MAX:
                vals.append(cum_map[(safra, s)])
        return vals

    def _baseline_net(rows_all: list[dict], seq: int, field: str) -> list[float]:
        from ingestion.unica import season_fortnight_seq as sfseq
        vals = []
        for r in rows_all:
            if r.get(field) is None:
                continue
            if sfseq(r["quinzena_date"]) != seq:
                continue
            try:
                y = int(r["safra"].split("-")[0])
            except Exception:
                continue
            if _BASELINE_SAFRAS_MIN <= y <= _BASELINE_SAFRAS_MAX:
                vals.append(r[field])
        return vals

    hist_cum_cane  = _baseline_cum(cum_cane_map, latest_seq)
    hist_cum_sugar = _baseline_cum(cum_sugar_map, latest_seq)
    hist_cum_eth_h = _baseline_cum(cum_eth_h_map, latest_seq)

    baseline_years = len(hist_cum_cane)

    # ── Señal A: Ritmo molienda acumulado ─────────────────────────────────────
    sig_a = {}
    if cum_cane_cur is not None and hist_cum_cane:
        sig_a = robust_stats(hist_cum_cane, cum_cane_cur)

    yoy_cane_pct = None
    try:
        y1, y2 = (int(p) for p in latest_safra.split("-"))
        prev_safra = f"{y1-1}-{y2-1}"
        prev_cane_cum = cum_cane_map.get((prev_safra, latest_seq))
        if prev_cane_cum and cum_cane_cur:
            yoy_cane_pct = round((cum_cane_cur / prev_cane_cum - 1) * 100, 1)
    except Exception:
        pass

    # ── Señal B: Ritmo azúcar acumulado ───────────────────────────────────────
    sig_b = {}
    if cum_sugar_cur is not None and hist_cum_sugar:
        sig_b = robust_stats(hist_cum_sugar, cum_sugar_cur)

    yoy_sugar_pct = None
    try:
        prev_sugar_cum = cum_sugar_map.get((prev_safra, latest_seq))
        if prev_sugar_cum and cum_sugar_cur:
            yoy_sugar_pct = round((cum_sugar_cur / prev_sugar_cum - 1) * 100, 1)
    except Exception:
        pass

    # ── Señal C: Mix + descomposición YoY azúcar ──────────────────────────────
    smix_cur = latest.get("sugar_mix_pct")
    hist_smix = [r["sugar_mix_pct"] for r in rows
                 if r.get("sugar_mix_pct") is not None
                 and season_fortnight_seq(r["quinzena_date"]) == latest_seq
                 and r["safra"] != latest_safra
                 and _BASELINE_SAFRAS_MIN <= int(r["safra"].split("-")[0]) <= _BASELINE_SAFRAS_MAX]

    sig_c = {}
    if smix_cur is not None and hist_smix:
        sig_c = robust_stats(hist_smix, smix_cur)

    # Descomposición del YoY de azúcar: Sugar ≈ Cane × ATR × mix_share × stoich
    # Δsugar_yoy = efecto_volumen + efecto_atr + efecto_mix
    mix_decomp = {}
    try:
        atr_cur  = latest.get("atr_kg_ton")
        prev_rows_same = [r for r in rows
                          if r["safra"] == prev_safra
                          and season_fortnight_seq(r["quinzena_date"]) == latest_seq]
        if prev_rows_same and cum_cane_cur and cum_sugar_cur and atr_cur and smix_cur:
            p = prev_rows_same[0]
            prev_cane_c = cum_cane_map.get((prev_safra, latest_seq)) or 0
            prev_sugar_c = cum_sugar_map.get((prev_safra, latest_seq)) or 0
            prev_atr   = p.get("atr_kg_ton") or atr_cur
            prev_smix  = p.get("sugar_mix_pct") or smix_cur

            # Aproximación lineal: dS ≈ (∂S/∂C)dC + (∂S/∂ATR)dATR + (∂S/∂mix)dmix
            # S ≈ C × ATR × (mix/100) × stoich  → stoich cancela en deltas relativos
            s_base = prev_cane_c * prev_atr * (prev_smix / 100)
            if s_base > 0:
                delta_c   = (cum_cane_cur  - prev_cane_c) * prev_atr * (prev_smix / 100)
                delta_atr = prev_cane_c * (atr_cur - prev_atr) * (prev_smix / 100)
                delta_mix = prev_cane_c * prev_atr * ((smix_cur - prev_smix) / 100)
                total_delta = delta_c + delta_atr + delta_mix
                if abs(total_delta) > 0:
                    mix_decomp = {
                        "yoy_sugar_pct":       yoy_sugar_pct,
                        "effect_volume_pct":   round(delta_c   / abs(total_delta) * 100, 1),
                        "effect_atr_pct":      round(delta_atr / abs(total_delta) * 100, 1),
                        "effect_mix_pct":      round(delta_mix / abs(total_delta) * 100, 1),
                        "delta_total_kt":      round(total_delta / 1000, 0),
                    }
    except Exception as e:
        logger.debug("mix_decomp error: %s", e)

    # ── Señal D: ATR ──────────────────────────────────────────────────────────
    atr_cur = latest.get("atr_kg_ton")
    hist_atr = [r["atr_kg_ton"] for r in rows
                if r.get("atr_kg_ton") is not None
                and season_fortnight_seq(r["quinzena_date"]) == latest_seq
                and r["safra"] != latest_safra
                and _BASELINE_SAFRAS_MIN <= int(r["safra"].split("-")[0]) <= _BASELINE_SAFRAS_MAX]
    sig_d = {}
    atr_delta = None
    if atr_cur and hist_atr:
        sig_d = robust_stats(hist_atr, atr_cur)
        atr_delta = round(atr_cur - sig_d["median"], 2)

    # ── Señal E: Etanol share hidratado ───────────────────────────────────────
    eth_mix_cur = latest.get("eth_mix_pct")
    hist_eth_mix = [r["eth_mix_pct"] for r in rows
                    if r.get("eth_mix_pct") is not None
                    and season_fortnight_seq(r["quinzena_date"]) == latest_seq
                    and r["safra"] != latest_safra
                    and _BASELINE_SAFRAS_MIN <= int(r["safra"].split("-")[0]) <= _BASELINE_SAFRAS_MAX]
    sig_e = {}
    if eth_mix_cur and hist_eth_mix:
        sig_e = robust_stats(hist_eth_mix, eth_mix_cur)

    # Share hidratado/total actual
    eth_hid_share = None
    if cum_eth_h_cur is not None and cum_eth_t_cur and cum_eth_t_cur > 0:
        eth_hid_share = round(cum_eth_h_cur / cum_eth_t_cur * 100, 1)

    # ── Señal F: Proyección full-year analógica ────────────────────────────────
    proj = {}
    for label, field, cum_map, cum_cur in [
        ("cane",  "cane_crushed_t",  cum_cane_map,  cum_cane_cur),
        ("sugar", "sugar_t",         cum_sugar_map, cum_sugar_cur),
        ("eth_t", "ethanol_total_m3",cum_eth_t_map, cum_eth_t_cur),
    ]:
        if cum_cur is None or cum_cur == 0:
            proj[label] = None
            continue
        # Para cada safra baseline, ratio = full_year / cum[latest_seq]
        ratios = []
        for safra in {s for s, _ in cum_map}:
            try:
                y = int(safra.split("-")[0])
            except Exception:
                continue
            if not (_BASELINE_SAFRAS_MIN <= y <= _BASELINE_SAFRAS_MAX):
                continue
            cum_at_seq = cum_map.get((safra, latest_seq))
            if cum_at_seq is None or cum_at_seq == 0:
                continue
            # full_year = max cumsum para esa safra (última quincena)
            safra_seqs = [(s, v) for (sf, s), v in cum_map.items() if sf == safra]
            if not safra_seqs:
                continue
            full_year = max(v for _, v in safra_seqs)
            ratios.append(full_year / cum_at_seq)

        if len(ratios) >= 5:
            med_ratio = _median(ratios)
            p25 = _percentile(ratios, 25)
            p75 = _percentile(ratios, 75)
            unit = 1e6  # t → Mt o m³ → Mm³
            proj[label] = {
                "point_mt":  round(cum_cur * med_ratio / unit, 3),
                "low_mt":    round(cum_cur * p25 / unit, 3) if p25 else None,
                "high_mt":   round(cum_cur * p75 / unit, 3) if p75 else None,
                "n_ratios":  len(ratios),
                "med_ratio": round(med_ratio, 3),
            }
        else:
            # Fallback: _SEASON_PROGRESS_PCT
            try:
                from ingestion.unica import _SEASON_PROGRESS_PCT
                ref_month = latest_date.month
                quinzena_half = 1 if latest_date.day <= 16 else 2
                prog = _SEASON_PROGRESS_PCT.get((ref_month, quinzena_half))
                if prog and prog > 5:
                    point = cum_cur / (prog / 100)
                    proj[label] = {"point_mt": round(point / unit, 3), "low_mt": None, "high_mt": None, "n_ratios": 0}
                else:
                    proj[label] = None
            except Exception:
                proj[label] = None

    # ── Señal G: Predicción próxima quincena ──────────────────────────────────
    from ingestion.unica import _SEASON_FORTNIGHTS
    next_seq = latest_seq + 1 if latest_seq < 24 else None
    pred_next = {}
    if next_seq is not None:
        pace_ratio_cane  = None
        pace_ratio_sugar = None
        med_cum_cane_cur  = _median(hist_cum_cane)  if hist_cum_cane  else None
        med_cum_sugar_cur = _median(hist_cum_sugar) if hist_cum_sugar else None

        if cum_cane_cur and med_cum_cane_cur and med_cum_cane_cur > 0:
            pace_ratio_cane = cum_cane_cur / med_cum_cane_cur
        if cum_sugar_cur and med_cum_sugar_cur and med_cum_sugar_cur > 0:
            pace_ratio_sugar = cum_sugar_cur / med_cum_sugar_cur

        for label, field, pace_ratio in [
            ("cane",  "cane_crushed_t", pace_ratio_cane),
            ("sugar", "sugar_t",        pace_ratio_sugar),
        ]:
            hist_net_next = _baseline_net(rows, next_seq, field)
            if hist_net_next and pace_ratio is not None:
                med_net = _median(hist_net_next)
                p25 = _percentile(hist_net_next, 25)
                p75 = _percentile(hist_net_next, 75)
                unit = 1e6
                pred_next[label] = {
                    "point_mt": round(med_net * pace_ratio / unit, 3) if med_net else None,
                    "low_mt":   round(p25 * pace_ratio / unit, 3) if p25 else None,
                    "high_mt":  round(p75 * pace_ratio / unit, 3) if p75 else None,
                    "next_seq": next_seq,
                }

    # ── Señal H: Bias direccional ICE No.11 ───────────────────────────────────
    # Score normalizado −1..+1; bearish (más oferta) = positivo = presión bajista precio
    # Más azúcar/proyección al alza/mix a azúcar → bearish; ritmo atrás → bullish
    bias_score = 0.0
    bias_drivers = []
    bias_weights = 0.0

    def _add_bias(z_or_pct, weight, label, sign=1.0):
        nonlocal bias_score, bias_weights
        if z_or_pct is None:
            return
        norm = max(-3.0, min(3.0, z_or_pct)) / 3.0
        bias_score  += sign * norm * weight
        bias_weights += weight
        bias_drivers.append(f"{label}:{sign*norm*weight:+.2f}")

    if sig_b.get("modified_z") is not None:
        _add_bias(sig_b["modified_z"], 0.35, "sugar_pace_z", sign=-1.0)  # más azúcar → bearish
    if sig_a.get("modified_z") is not None:
        _add_bias(sig_a["modified_z"], 0.20, "cane_pace_z",  sign=-1.0)
    if sig_c.get("modified_z") is not None:
        _add_bias(sig_c["modified_z"], 0.20, "sugar_mix_z",  sign=-1.0)  # más mix azúcar → bearish
    if sig_d.get("modified_z") is not None:
        _add_bias(sig_d["modified_z"], 0.10, "atr_z",        sign=-1.0)
    proj_sugar = proj.get("sugar") or {}
    if proj_sugar.get("point_mt") is not None and cum_sugar_cur:
        hist_med_proj = _median([r / 1e6 for r in hist_cum_sugar]) if hist_cum_sugar else None
        if hist_med_proj and hist_med_proj > 0:
            proj_z = (proj_sugar["point_mt"] - hist_med_proj * (proj_sugar.get("med_ratio", 1.0))) / (hist_med_proj * 0.05)
            _add_bias(proj_z, 0.15, "proj_sugar_z", sign=-1.0)

    if bias_weights > 0:
        bias_score = round(bias_score / bias_weights, 3)
    else:
        bias_score = None

    # ── Season progress ────────────────────────────────────────────────────────
    season_progress_pct = None
    try:
        from ingestion.unica import _SEASON_PROGRESS_PCT
        ref_month = latest_date.month
        quinzena_half = 1 if latest_date.day <= 16 else 2
        season_progress_pct = _SEASON_PROGRESS_PCT.get((ref_month, quinzena_half))
    except Exception:
        pass

    # ── MAPA ──────────────────────────────────────────────────────────────────
    mapa = _get_mapa_latest(session)

    return {
        "latest_safra":         latest_safra,
        "latest_quinzena_date": str(latest_date),
        "latest_seq":           latest_seq,
        "season_progress_pct":  season_progress_pct,
        "data_age_days":        data_age_days,
        "baseline_years":       baseline_years,
        # cumsum actuales (Mt)
        "cum_cane_mt":          round(cum_cane_cur  / 1e6, 3) if cum_cane_cur  else None,
        "cum_sugar_mt":         round(cum_sugar_cur / 1e6, 3) if cum_sugar_cur else None,
        # Señal A
        "A_cane_pace":          sig_a,
        "yoy_cane_pct":         yoy_cane_pct,
        # Señal B
        "B_sugar_pace":         sig_b,
        "yoy_sugar_pct":        yoy_sugar_pct,
        # Señal C
        "C_sugar_mix":          sig_c,
        "sugar_mix_pct_cur":    smix_cur,
        "mix_decomp":           mix_decomp,
        # Señal D
        "D_atr":                sig_d,
        "atr_cur":              atr_cur,
        "atr_delta":            atr_delta,
        # Señal E
        "E_eth_mix":            sig_e,
        "eth_hid_share_pct":    eth_hid_share,
        "eth_mix_pct_cur":      eth_mix_cur,
        # Señal F
        "F_proj":               proj,
        # Señal G
        "G_pred_next":          pred_next,
        # Señal H
        "H_bias_ice11":         bias_score,
        "H_bias_drivers":       bias_drivers,
        # MAPA
        "mapa_latest":          mapa,
    }


def format_crop_progress_report(signals: dict) -> str:
    """Formatea las señales ALL-IN como reporte legible."""
    if signals.get("error"):
        return f"Brazil Crop Progress: sin datos ({signals['error']})"

    def _v(key, default="N/D"):
        v = signals.get(key)
        return default if v is None else v

    def _rs(sig: dict, label: str) -> str:
        if not sig or sig.get("conviction") == "INSUFFICIENT_DATA":
            return f"  {label}: insuficiente"
        mz  = sig.get("modified_z")
        pct = sig.get("percentile_rank")
        con = sig.get("conviction", "?")
        return (f"  {label}: mZ={mz:+.2f}  pct={pct:.0f}  [{con}]"
                if mz is not None and pct is not None else f"  {label}: N/D")

    def _proj(p: Optional[dict], label: str) -> str:
        if not p:
            return f"  Proj {label}: N/D"
        pt = p.get("point_mt")
        lo = p.get("low_mt")
        hi = p.get("high_mt")
        n  = p.get("n_ratios", 0)
        band = f"  [{lo:.2f}–{hi:.2f}]" if lo and hi else ""
        return f"  Proj {label}: {pt:.2f} Mt{band}  (n={n})"

    lines = [
        "=" * 60,
        f"Brazil Crop Progress — Safra {_v('latest_safra')}",
        f"Quincena: {_v('latest_quinzena_date')} (seq={_v('latest_seq')})",
        f"Datos con {_v('data_age_days')}d de antigüedad | "
        f"baseline {signals.get('baseline_years', 0)} años | "
        f"progreso safra ~{_v('season_progress_pct')}%",
        "",
        f"ACUMULADO A FECHA:",
        f"  Caña:   {_v('cum_cane_mt')} Mt",
        f"  Azúcar: {_v('cum_sugar_mt')} Mt",
        f"  YoY caña: {_v('yoy_cane_pct')}%  |  YoY azúcar: {_v('yoy_sugar_pct')}%",
        "",
        "SEÑALES ROBUST (modified_z / percentile / conviction):",
        _rs(signals.get("A_cane_pace", {}), "A. Caña acumulada"),
        _rs(signals.get("B_sugar_pace", {}), "B. Azúcar acumulada"),
        _rs(signals.get("C_sugar_mix", {}),  "C. Mix azúcar %"),
        _rs(signals.get("D_atr", {}),        "D. ATR"),
        _rs(signals.get("E_eth_mix", {}),    "E. Mix etanol %"),
        f"  ATR actual: {_v('atr_cur')} kg/t  delta vs hist: {_v('atr_delta')} kg/t",
        f"  Mix azúcar: {_v('sugar_mix_pct_cur')}%  |  "
        f"Etanol hidratado share: {_v('eth_hid_share_pct')}%",
    ]

    # Descomposición mix
    decomp = signals.get("mix_decomp") or {}
    if decomp:
        lines += [
            "",
            f"DESCOMPOSICIÓN YoY AZÚCAR ({decomp.get('yoy_sugar_pct')}%  Δ≈{decomp.get('delta_total_kt')}kt):",
            f"  Efecto volumen (Δcaña): {decomp.get('effect_volume_pct')}%",
            f"  Efecto ATR:             {decomp.get('effect_atr_pct')}%",
            f"  Efecto mix:             {decomp.get('effect_mix_pct')}%",
        ]

    # Proyecciones
    proj = signals.get("F_proj") or {}
    lines += ["", "PROYECCIÓN FULL-YEAR (analógica, mediana ratios):"]
    lines.append(_proj(proj.get("cane"),  "caña"))
    lines.append(_proj(proj.get("sugar"), "azúcar"))
    lines.append(_proj(proj.get("eth_t"), "etanol total"))

    # Predicción próxima quincena
    pred = signals.get("G_pred_next") or {}
    if pred:
        cane_p  = pred.get("cane")  or {}
        sugar_p = pred.get("sugar") or {}
        next_seq = cane_p.get("next_seq") or sugar_p.get("next_seq")
        lines += ["", f"PREDICCIÓN PRÓXIMA QUINCENA (seq={next_seq}):"]
        if cane_p.get("point_mt"):
            lo = cane_p.get("low_mt"); hi = cane_p.get("high_mt")
            band = f" [{lo:.2f}–{hi:.2f}]" if lo and hi else ""
            lines.append(f"  Caña neta:   {cane_p['point_mt']:.2f} Mt{band}")
        if sugar_p.get("point_mt"):
            lo = sugar_p.get("low_mt"); hi = sugar_p.get("high_mt")
            band = f" [{lo:.2f}–{hi:.2f}]" if lo and hi else ""
            lines.append(f"  Azúcar neta: {sugar_p['point_mt']:.2f} Mt{band}")

    # Bias
    bias = signals.get("H_bias_ice11")
    drivers = signals.get("H_bias_drivers") or []
    if bias is not None:
        direction = "BEARISH (más oferta)" if bias < -0.15 else "BULLISH (menos oferta)" if bias > 0.15 else "NEUTRAL"
        lines += [
            "",
            f"BIAS ICE No.11: {bias:+.3f}  → {direction}",
            f"  Drivers: {' | '.join(drivers[:5]) if drivers else 'N/D'}",
        ]

    # MAPA
    mapa = signals.get("mapa_latest")
    if mapa:
        lines += [
            "",
            f"MAPA ({mapa.get('ref_date')} rev{mapa.get('revision_num', 0)}):",
            f"  Caña: {mapa.get('total_cane_mt')} Mt  "
            f"Azúcar: {mapa.get('sugar_mt')} Mt  "
            f"Etanol: {mapa.get('ethanol_total_mm3')} Mm³",
        ]

    lines.append("=" * 60)
    return "\n".join(str(l) for l in lines)
