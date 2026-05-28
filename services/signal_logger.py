"""
Signal Daily Log — extrae y persiste el estado de todas las señales del modelo.

Principios de diseño:
  1. El dato crudo es sagrado: raw_value = el número original, nunca normalizado.
     direction es la interpretación (+1/-1/0) derivable a posteriori.
  2. Fault-tolerance: cada fila usa un savepoint independiente. Un fallo en una
     señal no bloquea ni revierte las demás.
  3. Forward fill: señales infrecuentes (CONAB, GEE, MAPA) se marcan is_carry=True
     cuando se propaga el último valor conocido — el IC weighting distingue
     observaciones reales de carry.

Flujo diario:
  score_today.py → log_signals()         (escribe señales del día)
  daily_pipeline.py → fill_forward_returns()  (rellena fwd_ret_Nd retroactivamente)
  ic_weighting.py  → compute_ic_weights() (lee tabla para calibrar pesos)
"""
import logging
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Señales cuyo valor no cambia a diario — se propaga el último conocido (forward fill)
# Si la señal devuelve None hoy, buscamos el valor más reciente en la BD.
CARRY_FORWARD_SIGNALS = {
    "fundamental_a4_composite",  # MAPA cada 15 días
    "fundamental_a4_yoy_cane",
    "fundamental_a4_mix",
    "fundamental_a4_yoy_pct",
    "macro_conab_revision",       # CONAB 4-6x/año
    "macro_conab_yoy_sugar",
    "macro_harvest_pace",         # GEE — gaps por nubes
    "macro_crop_stress",
    "macro_rainfall_spi",
    "macro_enso_oni",             # ONI mensual
    "macro_climate_deficit90",    # Open-Meteo — rara vez falla, pero por si acaso
    "macro_climate_ndvi",
    "macro_carry_ratio",          # SOFR + spread: estable
    "macro_comex_yoy",            # COMEX: mensual
    "macro_fires_signal",         # INPE: puede fallar en fin de semana
    "macro_usda_stu_pct",         # USDA WASDE: mensual (publicación ~día 12 con WASDE)
    "macro_usda_prod_mt",
    "macro_usda_cons_mt",
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _bias(b: Optional[str]) -> Optional[int]:
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


def _safe(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        # Rechazar infinitos y NaN — no son valores útiles
        if f != f or abs(f) == float("inf"):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _dir_from_float(direction) -> Optional[int]:
    if direction is None:
        return None
    if isinstance(direction, int):
        return max(-1, min(1, direction))
    try:
        d = int(round(float(direction)))
        return max(-1, min(1, d))
    except (TypeError, ValueError):
        return None


# ── Signal extraction ──────────────────────────────────────────────────────────

def extract_signals(inputs: dict, brazil: Optional[dict],
                    macro: Optional[dict], santos: Optional[dict]) -> list[tuple]:
    """
    Extrae todas las señales del modelo.

    Returns list of (signal_name, group, raw_value, direction).

    raw_value: número continuo original — NUNCA normalizado.
                Si es None no hay dato disponible hoy (candidato a carry).
    direction: +1 LONG | -1 SHORT | 0 NEUTRAL | None = sin lógica de dirección
    """
    rows = []

    def add(name: str, group: str, raw, direction):
        rows.append((name, group, _safe(raw), _dir_from_float(direction)))

    inp = inputs or {}
    bra = brazil or {}
    mac = macro   or {}
    san = santos  or {}

    # ── COT Layer 1 ──────────────────────────────────────────────────────────

    # Dato crudo: posición neta absoluta (contratos) — el número de mercado real
    add("cot_spec_net_contracts", "cot", inp.get("spec_net"), None)
    add("cot_comm_net_contracts", "cot", inp.get("comm_net"), None)

    # Derivados: percentiles (para lógica de señal)
    spec_pct_at = _safe(inp.get("spec_alltime_pct"))
    spec_pct_3m = _safe(inp.get("spec_3m_pct"))
    add("cot_spec_pct_alltime", "cot", spec_pct_at,
        -1 if spec_pct_at is not None and spec_pct_at > 75
        else (+1 if spec_pct_at is not None and spec_pct_at < 25 else 0))
    add("cot_spec_pct_3m", "cot", spec_pct_3m,
        -1 if spec_pct_3m is not None and spec_pct_3m > 75
        else (+1 if spec_pct_3m is not None and spec_pct_3m < 25 else 0))

    # A2: cambio semanal (positivo = specs incrementando longs = presión SHORT)
    chg_1w = _safe(inp.get("spec_change_wk"))
    chg_4w = _safe(inp.get("spec_change_4wk"))
    add("cot_spec_change_1w", "cot", chg_1w,
        -1 if chg_1w is not None and chg_1w > 0 else (+1 if chg_1w is not None and chg_1w < 0 else 0))
    add("cot_spec_change_4w", "cot", chg_4w,
        -1 if chg_4w is not None and chg_4w > 0 else (+1 if chg_4w is not None and chg_4w < 0 else 0))

    # A3: exceso de hedging comercial vs media 13w
    comm_net  = _safe(inp.get("comm_net"))
    comm_mean = _safe(inp.get("comm_mean_13w"))
    comm_diff = (comm_net - comm_mean) if (comm_net is not None and comm_mean is not None) else None
    add("cot_comm_vs_13w_mean", "cot", comm_diff,
        -1 if comm_diff is not None and comm_diff < 0 else (+1 if comm_diff is not None and comm_diff > 0 else 0))

    # B1: spread SBN-SBV en c/lb (dato crudo + dirección)
    spread_b1 = _safe(inp.get("spread_val"))
    add("spread_b1_sbn_sbv", "spread", spread_b1,
        +1 if spread_b1 is not None and spread_b1 > 0
        else (-1 if spread_b1 is not None and spread_b1 < -0.05 else 0))

    # B2: z-score precio vs MA26w (dato crudo = z-score, ya es continuo)
    b2_z = _safe(inp.get("b2_z26"))
    add("price_b2_z26w", "spread", b2_z,
        -1 if b2_z is not None and b2_z > 1.5 else (+1 if b2_z is not None and b2_z < -1.5 else 0))

    # ── Fundamental A4 Brasil (MAPA) ─────────────────────────────────────────
    # Datos crudos: YoY% y mix% son los números de mercado reales
    add("fundamental_a4_composite",  "fundamental", bra.get("signal_a4"),   _bias(bra.get("bias")))
    add("fundamental_a4_yoy_cane",   "fundamental", bra.get("signal_a4a"),  None)
    add("fundamental_a4_mix",        "fundamental", bra.get("signal_a4b"),  None)
    add("fundamental_a4_yoy_pct",    "fundamental", bra.get("yoy_pct"),     None)
    add("fundamental_a4_mix_pct",    "fundamental", bra.get("mix_sugar_pct"), None)  # % azúcar/etanol real

    # ── Santos A5 ────────────────────────────────────────────────────────────
    # Datos crudos: número de barcos y tonelaje son los números físicos reales
    add("fundamental_a5_composite",  "fundamental", san.get("signal_a5"),   _bias(san.get("bias")))
    add("fundamental_a5_z_combined", "fundamental", san.get("z_combined"),  None)
    add("fundamental_a5_z_level",    "fundamental", san.get("z_level"),     None)
    add("fundamental_a5_n_ships",    "fundamental", san.get("n_ships"),     None)   # dato crudo: barcos
    add("fundamental_a5_tonnage_t",  "fundamental", san.get("tonnage"),     None)   # dato crudo: toneladas

    # ── Macro ─────────────────────────────────────────────────────────────────
    brl    = mac.get("brl",     {}) or {}
    brent  = mac.get("brent",   {}) or {}
    corr   = mac.get("corr",    {}) or {}
    parity = mac.get("parity",  {}) or {}
    enso   = mac.get("enso",    {}) or {}
    clim   = mac.get("climate", {}) or {}
    carry  = mac.get("carry",   {}) or {}
    comex  = mac.get("comex",   {}) or {}
    fire   = mac.get("fire",    {}) or {}
    conab  = mac.get("conab",   {}) or {}
    hp     = mac.get("harvest_pace", {}) or {}
    cs     = mac.get("crop_stress",  {}) or {}
    rf     = mac.get("rainfall",     {}) or {}
    usda   = mac.get("usda",    {}) or {}
    dxy    = mac.get("dxy",     {}) or {}

    # BRL: dato crudo = tipo de cambio real (USDBRL); derivado = % vs MA20
    add("macro_brl_per_usd",      "macro", brl.get("brl_per_usd"),    None)   # precio real BRL
    add("macro_brl_vs_ma20",      "macro", brl.get("vs_ma20_pct"),    _bias(brl.get("bias")))
    add("macro_brl_1d_chg",       "macro", brl.get("change_1d_pct"),  None)

    # Brent: dato crudo = precio en USD/bbl
    add("macro_brent_price_usd",  "macro", brent.get("brent_price"),  None)   # precio real Brent
    add("macro_brent_1d_chg",     "macro", brent.get("change_1d_pct"), _bias(brent.get("bias")))
    add("macro_brent_5d_chg",     "macro", brent.get("change_5d_pct"), None)

    # Correlaciones: datos crudos = ρ (Pearson rolling 5m)
    add("macro_corr_brent_sb",    "macro", corr.get("corr_brent_sugar"),  _bias(corr.get("bias")))
    add("macro_corr_brl_sb",      "macro", corr.get("corr_brl_sugar"),    None)

    # Paridad etanol-azúcar: dato crudo = precio etanol hidratado US$/m³ y spread c/lb
    add("macro_parity_ethanol_m3",  "macro", parity.get("hydrous_usd_m3"), None)  # precio real etanol
    add("macro_parity_spread_clb",  "macro", parity.get("spread_c_lb"),    None)  # spread real
    add("macro_parity_ratio",       "macro", parity.get("parity_ratio"),   _bias(parity.get("bias")))

    # ENSO/ONI: dato crudo = valor ONI (anomalía temperatura mar)
    add("macro_enso_oni",         "macro", enso.get("oni_value"),      _bias(enso.get("bias")))

    # Clima: datos crudos = déficit hídrico en mm y NDVI [0-1]
    add("macro_climate_deficit90","macro", clim.get("deficit_90d"),    _bias(clim.get("bias")))
    add("macro_climate_deficit30","macro", clim.get("deficit_30d"),    None)
    add("macro_climate_ndvi",     "macro", clim.get("ndvi"),           None)   # NDVI real [0-1]

    # Carry: dato crudo = carry ratio (spread/full_carry)
    add("macro_carry_ratio",      "macro", carry.get("carry_ratio"),   _bias(carry.get("bias")))
    add("macro_carry_spread_clb", "macro", carry.get("spread_c_lb"),   None)

    # Comex: dato crudo = YoY% real de exportaciones
    add("macro_comex_yoy",        "macro", comex.get("yoy_change_pct"), _bias(comex.get("bias")))

    # INPE fuegos: dato crudo = signal score [-1,0,+1]
    add("macro_fires_signal",     "macro", _safe(fire.get("signal")),  _bias(fire.get("bias")))

    # CONAB: datos crudos = revisión % real y YoY% real vs temporada anterior
    add("macro_conab_revision",   "macro", _safe(conab.get("revision_sugar_pct")), _bias(conab.get("bias")))
    add("macro_conab_yoy_sugar",  "macro", _safe(conab.get("yoy_sugar_pct")),      None)
    add("macro_conab_sugar_mt",   "macro", _safe(conab.get("sugar_total_mt")),     None)  # producción real Mt

    # GEE: datos crudos = z-scores compuestos por región
    add("macro_harvest_pace",     "macro", hp.get("score_weighted"),   _bias(hp.get("bias")))
    add("macro_crop_stress",      "macro", cs.get("score_weighted"),   _bias(cs.get("bias")))
    add("macro_rainfall_spi",     "macro", rf.get("score_weighted"),   _bias(rf.get("bias")))

    # USDA WASDE: dato crudo = STU% (Stocks-to-Use ratio global)
    add("macro_usda_stu_pct",    "macro", usda.get("stu_pct"),        _bias(usda.get("bias")))
    add("macro_usda_prod_mt",    "macro", usda.get("production_mt"),  None)   # producción global real Mt
    add("macro_usda_cons_mt",    "macro", usda.get("consumption_mt"), None)   # consumo global real Mt

    # DXY: dato crudo = nivel del índice dólar (≈104)
    add("macro_dxy_value",       "macro", dxy.get("dxy_value"),       None)
    add("macro_dxy_vs_ma20",     "macro", dxy.get("vs_ma20_pct"),     _bias(dxy.get("bias")))

    return rows


# ── Last-known-value lookup (para carry forward) ───────────────────────────────

def _get_last_known(session, signal_name: str, before_date: date) -> tuple:
    """Retorna (raw_value, direction) del registro más reciente antes de before_date."""
    from models.market_data import SignalDailyLog
    from sqlalchemy import select

    row = session.execute(
        select(SignalDailyLog.raw_value, SignalDailyLog.direction)
        .where(
            SignalDailyLog.signal_name == signal_name,
            SignalDailyLog.date < before_date,
        )
        .order_by(SignalDailyLog.date.desc())
        .limit(1)
    ).first()
    if row and (row.raw_value is not None or row.direction is not None):
        return (_safe(row.raw_value), row.direction)
    return (None, None)


# ── DB write — atomic per row via savepoints ───────────────────────────────────

def log_signals(session, signals_date: date,
                inputs: dict, brazil: Optional[dict],
                macro: Optional[dict], santos: Optional[dict]) -> dict:
    """
    Extrae y persiste señales en signal_daily_log.

    Garantías:
    - Cada fila usa un SAVEPOINT independiente: un fallo en una señal no
      afecta al resto (fault isolation).
    - Señales CARRY_FORWARD_SIGNALS sin dato hoy se propagan desde el último
      valor conocido en BD, marcadas con is_carry=True.
    - Idempotente: ON CONFLICT DO UPDATE sobreescribe solo si hay dato nuevo
      (is_carry=False) — no sobreescribe un dato real con carry.

    Returns dict: {"written": int, "carried": int, "skipped": int, "errors": int}
    """
    from models.market_data import SignalDailyLog
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    rows = extract_signals(inputs, brazil, macro, santos)
    stats = {"written": 0, "carried": 0, "skipped": 0, "errors": 0}

    for name, group, raw, direction in rows:
        is_carry = False

        # Si no hay dato nuevo, intentar carry forward para señales elegibles
        if raw is None and direction is None:
            if name not in CARRY_FORWARD_SIGNALS:
                stats["skipped"] += 1
                continue
            raw, direction = _get_last_known(session, name, signals_date)
            if raw is None and direction is None:
                stats["skipped"] += 1
                continue
            is_carry = True

        # Escribir con savepoint para aislar fallos por fila
        try:
            with session.begin_nested():
                stmt = pg_insert(SignalDailyLog).values(
                    date=signals_date,
                    signal_name=name,
                    signal_group=group,
                    raw_value=raw,
                    direction=direction,
                    is_carry=is_carry,
                ).on_conflict_do_update(
                    index_elements=["date", "signal_name"],
                    # Solo sobreescribir si el nuevo dato es real (no carry sobre real)
                    set_={
                        "raw_value":    pg_insert(SignalDailyLog).excluded.raw_value,
                        "direction":    pg_insert(SignalDailyLog).excluded.direction,
                        "is_carry":     pg_insert(SignalDailyLog).excluded.is_carry,
                    },
                )
                session.execute(stmt)

            if is_carry:
                stats["carried"] += 1
            else:
                stats["written"] += 1

        except Exception as exc:
            # Fallo en fila individual: loggear y continuar con las demás
            logger.warning("signal_logger: fila '%s' fallida: %s", name, exc)
            stats["errors"] += 1

    # Commit final — las filas individuales que fallaron ya están en rollback
    try:
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.error("signal_logger: commit final fallido: %s", exc)
        return {"written": 0, "carried": 0, "skipped": stats["skipped"], "errors": stats["errors"]}

    logger.info(
        "signal_logger %s: real=%d  carry=%d  skip=%d  err=%d",
        signals_date, stats["written"], stats["carried"], stats["skipped"], stats["errors"],
    )
    return stats


# ── Forward return filler ──────────────────────────────────────────────────────

def fill_forward_returns(session, instrument: str = "SBN26") -> dict:
    """
    Rellena fwd_ret_5d/10d/20d para registros donde ya se conoce el precio futuro.
    Solo actualiza registros con is_carry=False (datos reales) para mantener
    la integridad del IC: el carry forward ya tiene la dirección del dato real,
    pero su retorno forward debe medirse desde la fecha original del dato.
    Se ejecuta en daily_pipeline.py.
    """
    from models.market_data import SignalDailyLog, PriceHistory
    from sqlalchemy import select, and_

    today = date.today()
    filled = {5: 0, 10: 0, 20: 0}

    price_rows = session.execute(
        select(PriceHistory.date, PriceHistory.close)
        .where(PriceHistory.instrument == instrument)
        .order_by(PriceHistory.date)
    ).fetchall()

    if not price_rows:
        logger.warning("fill_forward_returns: sin precios para %s", instrument)
        return filled

    price_map   = {r.date: float(r.close) for r in price_rows}
    price_dates = sorted(price_map.keys())

    def _nth_close(signal_date, n_days):
        idx = next((i for i, d in enumerate(price_dates) if d >= signal_date), None)
        if idx is None:
            return None
        target = idx + n_days
        return price_map[price_dates[target]] if target < len(price_dates) else None

    def _ret_pct(signal_date, n_days):
        base = next((price_map[d] for d in price_dates if d >= signal_date), None)
        fwd  = _nth_close(signal_date, n_days)
        if base is None or fwd is None or base == 0:
            return None
        return round((fwd / base - 1) * 100, 4)

    for n, col_attr in [(5, "fwd_ret_5d"), (10, "fwd_ret_10d"), (20, "fwd_ret_20d")]:
        cutoff = today - timedelta(days=n * 2)
        rows = session.execute(
            select(SignalDailyLog)
            .where(and_(
                getattr(SignalDailyLog, col_attr).is_(None),
                SignalDailyLog.date <= cutoff,
            ))
            .limit(1000)
        ).scalars().all()

        for row in rows:
            ret = _ret_pct(row.date, n)
            if ret is not None:
                setattr(row, col_attr, ret)
                filled[n] += 1

    try:
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.warning("fill_forward_returns: commit error: %s", exc)
        return {5: 0, 10: 0, 20: 0}

    if any(filled.values()):
        logger.info("fill_forward_returns: 5d=%d  10d=%d  20d=%d", filled[5], filled[10], filled[20])
    return filled
