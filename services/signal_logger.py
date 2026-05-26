"""
Signal Daily Log — extrae y persiste el estado de todas las señales del modelo.

Flujo:
  score_today.py llama a log_signals() después de calcular señales.
  daily_pipeline.py llama a fill_forward_returns() para rellenar fwd_ret_Nd
  retroactivamente cuando el precio futuro ya está disponible.
  ic_weighting.py usa esta tabla para calcular IC rolling por señal.

Convención de dirección:
  +1 = señal BULLISH (sugiere LONG)
  -1 = señal BEARISH (sugiere SHORT)
   0 = NEUTRAL / sin señal
"""
import logging
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ── Helpers ────────────────────────────────────────────────────────────────────

def _bias(b: Optional[str]) -> Optional[int]:
    """Convierte string bias a dirección numérica."""
    if b is None:
        return None
    b = b.upper()
    if "LONG" in b or "BULL" in b:
        return +1
    if "SHORT" in b or "BEAR" in b or "CONTRA" in b:
        return -1
    if "NEUTRAL" in b:
        return 0
    return None


def _clamp(v, lo=-1, hi=1):
    if v is None:
        return None
    return max(lo, min(hi, float(v)))


def _safe(v):
    """Convierte Decimal/float a float o None."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── Signal extraction ──────────────────────────────────────────────────────────

def extract_signals(inputs: dict, brazil: Optional[dict],
                    macro: Optional[dict], santos: Optional[dict]) -> list[tuple]:
    """
    Extrae todas las señales del modelo en formato (name, group, raw_value, direction).

    raw_value: valor continuo original (para IC regression)
    direction: +1 / 0 / -1 (para weighted score)

    Returns list of (signal_name, group, raw_value, direction).
    """
    rows = []

    def add(name, group, raw, direction):
        r = _safe(raw)
        d = direction if isinstance(direction, int) else _safe(direction)
        if d is not None:
            d = int(round(d))
            d = max(-1, min(1, d))
        rows.append((name, group, r, d))

    # ── COT Layer 1 ───────────────────────────────────────────────────────────
    inp = inputs or {}

    # A1: spec net position percentile vs all-time history
    # Alto percentil (>75) = crowded long = señal SHORT = direction -1
    spec_pct = _safe(inp.get("spec_alltime_pct"))
    add("cot_spec_pct_alltime", "cot", spec_pct,
        -1 if spec_pct is not None and spec_pct > 75
        else (+1 if spec_pct is not None and spec_pct < 25 else 0))

    spec_3m = _safe(inp.get("spec_3m_pct"))
    add("cot_spec_pct_3m", "cot", spec_3m,
        -1 if spec_3m is not None and spec_3m > 75
        else (+1 if spec_3m is not None and spec_3m < 25 else 0))

    # A1: spec net absolute (contratos)
    add("cot_spec_net", "cot", inp.get("spec_net"), None)

    # A2: cambio semanal de specs (positivo = specs subiendo = más largo = SHORT)
    chg_1w = _safe(inp.get("spec_change_wk"))
    add("cot_spec_change_1w", "cot", chg_1w,
        -1 if chg_1w is not None and chg_1w > 0 else (+1 if chg_1w is not None and chg_1w < 0 else 0))

    chg_4w = _safe(inp.get("spec_change_4wk"))
    add("cot_spec_change_4w", "cot", chg_4w,
        -1 if chg_4w is not None and chg_4w > 0 else (+1 if chg_4w is not None and chg_4w < 0 else 0))

    # A3: comerciales vs media 13w (más hedgeado = más negativo = señal SHORT)
    comm_net  = _safe(inp.get("comm_net"))
    comm_mean = _safe(inp.get("comm_mean_13w"))
    comm_diff = (comm_net - comm_mean) if (comm_net is not None and comm_mean is not None) else None
    add("cot_comm_vs_13w_mean", "cot", comm_diff,
        -1 if comm_diff is not None and comm_diff < 0 else (+1 if comm_diff is not None and comm_diff > 0 else 0))

    # B1: spread SBN-SBV (negativo = contango = SHORT; positivo = backwardation = LONG)
    spread_b1 = _safe(inp.get("spread_val"))
    add("spread_b1_sbn_sbv", "spread",  spread_b1,
        +1 if spread_b1 is not None and spread_b1 > 0
        else (-1 if spread_b1 is not None and spread_b1 < -0.05 else 0))

    # B2: z-score precio vs MA26w
    b2_z = _safe(inp.get("b2_z26"))
    add("price_b2_z26w", "spread", b2_z,
        -1 if b2_z is not None and b2_z > 1.5
        else (+1 if b2_z is not None and b2_z < -1.5 else 0))

    # ── Fundamental A4 Brasil (MAPA) ──────────────────────────────────────────
    bra = brazil or {}
    add("fundamental_a4_composite", "fundamental", bra.get("signal_a4"), _bias(bra.get("bias")))
    add("fundamental_a4_yoy_cane",  "fundamental", bra.get("signal_a4a"), None)
    add("fundamental_a4_mix",       "fundamental", bra.get("signal_a4b"), None)
    add("fundamental_a4_yoy_pct",   "fundamental", bra.get("yoy_pct"),    None)

    # ── Santos A5 ─────────────────────────────────────────────────────────────
    san = santos or {}
    add("fundamental_a5_composite", "fundamental", san.get("signal_a5"), _bias(san.get("bias")))
    add("fundamental_a5_z_combined","fundamental", san.get("z_combined"), None)
    add("fundamental_a5_z_level",   "fundamental", san.get("z_level"),    None)
    add("fundamental_a5_n_ships",   "fundamental", san.get("n_ships"),    None)

    # ── Macro ─────────────────────────────────────────────────────────────────
    mac = macro or {}

    brl   = mac.get("brl",     {}) or {}
    brent = mac.get("brent",   {}) or {}
    corr  = mac.get("corr",    {}) or {}
    parity= mac.get("parity",  {}) or {}
    enso  = mac.get("enso",    {}) or {}
    clim  = mac.get("climate", {}) or {}
    carry = mac.get("carry",   {}) or {}
    comex = mac.get("comex",   {}) or {}
    fire  = mac.get("fire",    {}) or {}
    conab = mac.get("conab",   {}) or {}
    hp    = mac.get("harvest_pace", {}) or {}
    cs    = mac.get("crop_stress",  {}) or {}
    rf    = mac.get("rainfall",     {}) or {}

    add("macro_brl_vs_ma20",     "macro", brl.get("vs_ma20_pct"),    _bias(brl.get("bias")))
    add("macro_brl_1d_chg",      "macro", brl.get("change_1d_pct"),  None)
    add("macro_brent_1d_chg",    "macro", brent.get("change_1d_pct"),_bias(brent.get("bias")))
    add("macro_brent_5d_chg",    "macro", brent.get("change_5d_pct"),None)
    add("macro_corr_brent_sb",   "macro", corr.get("corr_brent_sugar"), _bias(corr.get("bias")))
    add("macro_corr_brl_sb",     "macro", corr.get("corr_brl_sugar"),   None)
    add("macro_parity_ratio",    "macro", parity.get("parity_ratio"),    _bias(parity.get("bias")))
    add("macro_parity_spread_clb","macro", parity.get("spread_c_lb"),    None)
    add("macro_enso_oni",        "macro", enso.get("oni_value"),          _bias(enso.get("bias")))
    add("macro_climate_deficit90","macro", clim.get("deficit_90d"),      _bias(clim.get("bias")))
    add("macro_climate_ndvi",    "macro", clim.get("ndvi"),               None)
    add("macro_carry_ratio",     "macro", carry.get("carry_ratio"),       _bias(carry.get("bias")))
    add("macro_comex_yoy",       "macro", comex.get("yoy_change_pct"),   _bias(comex.get("bias")))
    add("macro_fires_signal",    "macro", fire.get("signal"),             _bias(fire.get("bias")))
    add("macro_conab_revision",  "macro", _safe(conab.get("revision_sugar_pct")), _bias(conab.get("bias")))
    add("macro_conab_yoy_sugar", "macro", _safe(conab.get("yoy_sugar_pct")),      None)
    add("macro_harvest_pace",    "macro", hp.get("score_weighted"),       _bias(hp.get("bias")))
    add("macro_crop_stress",     "macro", cs.get("score_weighted"),       _bias(cs.get("bias")))
    add("macro_rainfall_spi",    "macro", rf.get("score_weighted"),       _bias(rf.get("bias")))

    return rows


# ── DB write ───────────────────────────────────────────────────────────────────

def log_signals(session, signals_date: date,
                inputs: dict, brazil: Optional[dict],
                macro: Optional[dict], santos: Optional[dict]) -> int:
    """
    Extrae señales y las persiste en signal_daily_log.
    Usa upsert (INSERT OR IGNORE) para ser idempotente.
    Retorna número de filas escritas.
    """
    from models.market_data import SignalDailyLog
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    rows = extract_signals(inputs, brazil, macro, santos)
    written = 0
    for name, group, raw, direction in rows:
        if raw is None and direction is None:
            continue
        stmt = pg_insert(SignalDailyLog).values(
            date=signals_date,
            signal_name=name,
            signal_group=group,
            raw_value=raw,
            direction=direction,
        ).on_conflict_do_update(
            index_elements=["date", "signal_name"],
            set_={"raw_value": raw, "direction": direction},
        )
        session.execute(stmt)
        written += 1
    try:
        session.commit()
    except Exception as e:
        session.rollback()
        logger.warning("signal_logger: commit error: %s", e)
        return 0
    logger.info("signal_logger: %d señales loggeadas para %s", written, signals_date)
    return written


# ── Forward return filler ──────────────────────────────────────────────────────

def fill_forward_returns(session, instrument: str = "SBN26") -> dict:
    """
    Rellena fwd_ret_Nd en signal_daily_log para registros donde ya se conoce
    el precio futuro (date + N días <= hoy).

    Usa PriceHistory (daily close) para calcular el retorno porcentual.
    Se ejecuta en daily_pipeline.py.
    """
    from models.market_data import SignalDailyLog, PriceHistory
    from sqlalchemy import select, and_

    today = date.today()
    filled = {5: 0, 10: 0, 20: 0}

    # Obtener precios diarios disponibles para el instrumento
    price_rows = session.execute(
        select(PriceHistory.date, PriceHistory.close)
        .where(PriceHistory.instrument == instrument)
        .order_by(PriceHistory.date)
    ).fetchall()

    if not price_rows:
        logger.warning("fill_forward_returns: sin precios para %s", instrument)
        return filled

    price_map = {r.date: float(r.close) for r in price_rows}
    price_dates = sorted(price_map.keys())

    def _nth_close(signal_date, n_days):
        """Retorna el close N días hábiles después de signal_date."""
        idx = None
        for i, d in enumerate(price_dates):
            if d >= signal_date:
                idx = i
                break
        if idx is None:
            return None
        target_idx = idx + n_days
        if target_idx >= len(price_dates):
            return None
        return price_map[price_dates[target_idx]]

    def _ret_pct(signal_date, n_days):
        """Retorno porcentual de signal_date a signal_date + N días hábiles."""
        base_close = None
        for d in price_dates:
            if d >= signal_date:
                base_close = price_map[d]
                break
        fwd_close = _nth_close(signal_date, n_days)
        if base_close is None or fwd_close is None or base_close == 0:
            return None
        return round((fwd_close / base_close - 1) * 100, 4)

    for n, col_attr in [(5, "fwd_ret_5d"), (10, "fwd_ret_10d"), (20, "fwd_ret_20d")]:
        cutoff = today - timedelta(days=n * 2)  # margen calendario
        # Registros sin retorno aún donde date es suficientemente antigua
        rows = session.execute(
            select(SignalDailyLog)
            .where(and_(
                getattr(SignalDailyLog, col_attr).is_(None),
                SignalDailyLog.date <= cutoff,
            ))
            .limit(500)
        ).scalars().all()

        for row in rows:
            ret = _ret_pct(row.date, n)
            if ret is not None:
                setattr(row, col_attr, ret)
                filled[n] += 1

    try:
        session.commit()
    except Exception as e:
        session.rollback()
        logger.warning("fill_forward_returns: commit error: %s", e)
        return {5: 0, 10: 0, 20: 0}

    if any(filled.values()):
        logger.info("fill_forward_returns: retornos rellenados — 5d:%d 10d:%d 20d:%d",
                    filled[5], filled[10], filled[20])
    return filled
