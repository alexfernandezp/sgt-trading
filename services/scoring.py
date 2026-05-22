import logging
from datetime import date
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import text
from models import DailyScoring
from ingestion.intraday import calc_session_vwap

logger = logging.getLogger(__name__)


def _score(condition: bool) -> int:
    return 1 if condition else 0


def get_current_price(session: Session, instrument: str = "SBN26") -> float | None:
    """Precio mas reciente: ultima barra 30m (Yahoo ~10min delay) o cierre diario."""
    row = session.execute(text("""
        SELECT close FROM price_bars
        WHERE instrument = :instr AND interval = '30m'
        ORDER BY datetime DESC LIMIT 1
    """), {"instr": instrument}).fetchone()
    if row:
        return float(row[0])
    row = session.execute(text("""
        SELECT close FROM price_history
        WHERE instrument = :instr ORDER BY date DESC LIMIT 1
    """), {"instr": instrument}).fetchone()
    return float(row[0]) if row else None


def _cot_regime(rows_full):
    """
    Clasifica el regimen de posicionamiento de especuladores (logica contrarian).

    En sugar, los specs son trend-followers que se equivocan en los extremos.
    Por tanto las señales son contrarian: extremo corto = señal larga, crowded largo = señal corta.

    Prioridad:
    EXTREMO_CORTO_ABSOLUTO : P_hist <= P5  → LONG  (rebote/squeeze inminente)
    EXTREMO_LARGO_ABSOLUTO : P_hist >= P95 → SHORT (liquidacion inminente)
    CONTRARIAN_SHORT       : P_hist >= P85 Y trend_4wk < 0  → SHORT (extremo + revirtiendo)
    CROWDED_SHORT          : pct_13w <= P40 Y trend_4wk < 0 → LONG  (specs apilandose cortos = contrarian long)
    CROWDED_LONG           : pct_13w >= P60 Y trend_4wk > 0 → SHORT (specs apilandose largos = contrarian short)
    NEUTRAL                : ningun criterio cumplido

    Retorna (regime_label, signal_long, signal_short, context_dict)
    """
    if len(rows_full) < 8:
        return "NEUTRAL", 0, 0, {}

    current = float(rows_full[0][0])

    # All-time percentile
    all_vals  = [float(r[0]) for r in rows_full]
    n_all     = len(all_vals)
    alltime_pct = sum(1 for v in all_vals if v <= current) / n_all * 100

    # 52-week percentile (last 52 data points ~= last year of weekly COT)
    recent_n  = min(52, n_all)
    recent_vals = [float(r[0]) for r in rows_full[:recent_n]]
    recent_pct  = sum(1 for v in recent_vals if v <= current) / len(recent_vals) * 100

    # 4-week trend: compare 4wk MA now vs 4wk MA one week ago
    if n_all >= 5:
        ma4_now  = sum(float(r[0]) for r in rows_full[:4]) / 4
        ma4_prev = sum(float(r[0]) for r in rows_full[1:5]) / 4
        trend_4wk = ma4_now - ma4_prev          # + = mejorando (menos cortos / mas largos)
    elif n_all >= 2:
        trend_4wk = float(rows_full[0][0]) - float(rows_full[1][0])
    else:
        trend_4wk = 0.0

    # 1-week change
    change_1wk = float(rows_full[0][0]) - float(rows_full[1][0]) if n_all >= 2 else 0.0

    # 2-week change (más granular que 4 semanas para detectar giro reciente)
    change_2wk = float(rows_full[0][0]) - float(rows_full[min(2, n_all-1)][0])

    # 4-week absolute change
    change_4wk = float(rows_full[0][0]) - float(rows_full[min(4, n_all-1)][0])

    # 13-week (3 meses) percentile — contexto reciente accionable
    n_13w     = min(13, n_all)
    vals_13w  = [float(r[0]) for r in rows_full[:n_13w]]
    pct_13w   = sum(1 for v in vals_13w if v <= current) / len(vals_13w) * 100

    # Historical extremes for context
    hist_min = min(all_vals)
    hist_max = max(all_vals)
    pct_from_min = round((current - hist_min) / (hist_max - hist_min) * 100, 1) if hist_max > hist_min else 50.0

    # Clasificacion contrarian: specs son trend-followers que se equivocan en extremos
    # Prioridad: extremos absolutos > contrarian extremo > crowded reciente
    if alltime_pct <= 5:
        regime       = "EXTREMO_CORTO_ABSOLUTO"   # rebote/squeeze inminente
        sig_l, sig_s = 1, 0
    elif alltime_pct >= 95:
        regime       = "EXTREMO_LARGO_ABSOLUTO"   # liquidacion inminente
        sig_l, sig_s = 0, 1
    elif alltime_pct >= 85 and trend_4wk < 0:
        regime       = "CONTRARIAN_SHORT"         # extremo largo + revirtiendo → SHORT
        sig_l, sig_s = 0, 1
    elif trend_4wk < 0 and pct_13w <= 40:
        regime       = "CROWDED_SHORT"            # specs apilandose cortos en zona baja → contrarian LONG
        sig_l, sig_s = 1, 0
    elif trend_4wk > 0 and pct_13w >= 60:
        regime       = "CROWDED_LONG"             # specs apilandose largos en zona alta → contrarian SHORT
        sig_l, sig_s = 0, 1
    else:
        regime       = "NEUTRAL"
        sig_l, sig_s = 0, 0

    ctx = {
        "spec_net":           int(current),
        "spec_mean_hist":     round(sum(all_vals) / n_all),
        "spec_alltime_pct":   round(alltime_pct, 1),
        "spec_recent_pct":    round(recent_pct, 1),    # 52-semanas (referencia anual)
        "spec_3m_pct":        round(pct_13w, 1),       # 13 semanas (3 meses, accionable)
        "spec_trend_4wk":     round(trend_4wk),
        "spec_change_wk":     round(change_1wk),
        "spec_change_2wk":    round(change_2wk),
        "spec_change_4wk":    round(change_4wk),
        "spec_hist_min":      int(hist_min),
        "spec_hist_max":      int(hist_max),
        "spec_pct_from_min":  pct_from_min,
        "cot_regime":         regime,
    }
    return regime, sig_l, sig_s, ctx


def _score_a1(session, direction):
    """
    COT Level — deteccion de regimen.
    Bullish contrarian solo cuando posicion es EXTREMO REAL y esta invirtiendo.
    Bearish trend-following cuando specs estan construyendo posicion bajista.
    """
    rows = session.execute(text(
        "SELECT speculator_net FROM cot_data ORDER BY report_date DESC"
    )).fetchall()
    if len(rows) < 4:
        return None, {}
    regime, sig_l, sig_s, ctx = _cot_regime(rows)
    score = sig_l if direction == "LONG" else sig_s
    return score, ctx


def _score_a2(session, direction):
    """
    COT Momentum semanal INVERTIDO (contrarian).
    Cuando specs REDUCEN posicion esta semana → señal LONG (se equivocan en el bottom).
    Cuando specs AUMENTAN posicion esta semana → señal SHORT (se equivocan en el top).
    Backtest 2017-2026: invertido da 53-57% WR vs 42-45% del original.
    """
    rows = session.execute(text(
        "SELECT speculator_net FROM cot_data ORDER BY report_date DESC LIMIT 5"
    )).fetchall()
    if len(rows) < 2:
        return None, {}
    change_1wk = float(rows[0][0]) - float(rows[1][0])
    change_4wk = float(rows[0][0]) - float(rows[min(4, len(rows)-1)][0])
    # Invertido: reduccion spec → LONG, aumento spec → SHORT
    score = _score(change_1wk < 0) if direction == "LONG" else _score(change_1wk > 0)
    return score, {"spec_change_wk": round(change_1wk), "spec_change_4wk": round(change_4wk)}


def _score_a3(session, direction):
    """
    Comerciales vs media 13 semanas (no all-time).
    comm > 13w mean → menos hedgeados que reciente → LONG (52% WR)
    comm < 13w mean → más hedgeados que reciente   → SHORT (57% WR)
    All-time mean da 46% — la ventana de 13s captura el contexto accionable.
    """
    rows = session.execute(text(
        "SELECT comm_net FROM cot_data ORDER BY report_date DESC LIMIT 13"
    )).fetchall()
    if len(rows) < 4:
        return None, {}
    vals   = [r[0] for r in rows]
    latest = vals[0]
    mean_13w = sum(vals) / len(vals)
    score  = _score(latest > mean_13w) if direction == "LONG" else _score(latest < mean_13w)
    return score, {"comm_net": latest, "comm_mean_13w": round(mean_13w)}


def _score_b1(session, direction):
    row = session.execute(text("""
        SELECT MAX(CASE WHEN instrument='SBN26' THEN close END),
               MAX(CASE WHEN instrument='SBV26' THEN close END)
        FROM price_history
        WHERE instrument IN ('SBN26','SBV26')
          AND date = (SELECT MAX(date) FROM price_history WHERE instrument='SBN26')
    """)).fetchone()
    if not row or row[0] is None or row[1] is None:
        return None, {}
    sbn, sbv = float(row[0]), float(row[1])
    spread   = round(sbn - sbv, 4)
    score    = _score(spread > 0) if direction == "LONG" else _score(spread < 0)
    return score, {"sbn26": sbn, "sbv26": sbv, "spread_sbn_sbv": spread}


def _score_b2(session, direction):
    """
    Z-score precio vs media 26w (130 sesiones de trading).
    z < -1.5 → LONG 57.4% WR (N=68 en backtest)
    z > +1.5 → SHORT 59.0% WR (N=61 en backtest)
    Más riguroso que extensión MA20 (46% WR) o simple above/below.
    """
    rows = session.execute(text("""
        SELECT close FROM price_history WHERE instrument='SB_CONT'
        ORDER BY date DESC LIMIT 200
    """)).fetchall()
    if len(rows) < 60:
        return None, {}

    closes = [float(r[0]) for r in reversed(rows)]   # oldest first
    price  = closes[-1]
    window = closes[-130:] if len(closes) >= 130 else closes

    mean26w = sum(window) / len(window)
    variance = sum((x - mean26w) ** 2 for x in window) / len(window)
    std26w  = variance ** 0.5

    if std26w < 0.001:
        return None, {}

    z = (price - mean26w) / std26w

    if direction == "LONG":
        score = _score(z < -1.5)
    else:
        score = _score(z > +1.5)

    return score, {
        "price":      round(price, 4),
        "mean_26w":   round(mean26w, 4),
        "std_26w":    round(std26w, 4),
        "b2_z26":     round(z, 3),
        "b2_zone":    "OVERSOLD" if z < -1.5 else ("OVERBOUGHT" if z > 1.5 else "NEUTRAL"),
    }


def _score_b3(session, direction, instrument):
    vwap  = calc_session_vwap(session, instrument)
    price = get_current_price(session, instrument)
    if vwap is None or price is None:
        return None, {}
    score = _score(price > vwap) if direction == "LONG" else _score(price < vwap)
    return score, {"price": price, "vwap": vwap}


def _score_c2(session, direction):
    """
    Analisis precio-volumen direccional:
      Subida + volumen BAJO  = sin conviccion, distribucion   -> SHORT
      Bajada + volumen BAJO  = venta agotada, sin presion     -> LONG
      Subida + volumen ALTO  = conviction alcista             -> LONG
      Bajada + volumen ALTO  = conviction bajista             -> SHORT

    Tambien calcula ritmo acumulado de volumen vs media historica
    a la misma hora del dia (pace).
    """
    # Primer 30m de cada sesion (apertura)
    rows_open = session.execute(text("""
        WITH first_bar AS (
            SELECT DATE(datetime) AS day, MIN(datetime) AS first_dt
            FROM price_bars WHERE instrument='SBN26' AND interval='30m'
            GROUP BY DATE(datetime) ORDER BY day DESC LIMIT 61
        )
        SELECT pb.volume FROM price_bars pb
        JOIN first_bar fb ON pb.datetime = fb.first_dt
        WHERE pb.instrument='SBN26' AND pb.interval='30m'
        ORDER BY fb.day DESC
    """)).fetchall()
    if len(rows_open) < 5:
        return None, {}
    open_vols  = [r[0] for r in rows_open if r[0] is not None]
    today_open = open_vols[0]
    avg_open   = sum(open_vols[1:]) / len(open_vols[1:]) if len(open_vols) > 1 else 1
    open_ratio = round(today_open / avg_open, 2) if avg_open else None

    # Ultimo 30m de la sesion anterior (cierre ayer)
    rows_close = session.execute(text("""
        WITH last_bar AS (
            SELECT DATE(datetime) AS day, MAX(datetime) AS last_dt
            FROM price_bars WHERE instrument='SBN26' AND interval='30m'
              AND DATE(datetime) < CURRENT_DATE
            GROUP BY DATE(datetime) ORDER BY day DESC LIMIT 61
        )
        SELECT pb.volume FROM price_bars pb
        JOIN last_bar lb ON pb.datetime = lb.last_dt
        WHERE pb.instrument='SBN26' AND pb.interval='30m'
        ORDER BY lb.day DESC
    """)).fetchall()
    prev_close_vol = prev_close_ratio = None
    if len(rows_close) >= 2:
        close_vols       = [r[0] for r in rows_close if r[0] is not None]
        prev_close_vol   = close_vols[0]
        avg_close        = sum(close_vols[1:]) / len(close_vols[1:]) if len(close_vols) > 1 else 1
        prev_close_ratio = round(prev_close_vol / avg_close, 2) if avg_close else None

    # Direccion del precio desde la apertura de hoy
    row_open_bar = session.execute(text("""
        SELECT open FROM price_bars
        WHERE instrument='SBN26' AND interval='30m'
          AND DATE(datetime) = CURRENT_DATE
        ORDER BY datetime ASC LIMIT 1
    """)).fetchone()
    current_price = get_current_price(session, "SBN26")
    price_up = price_move = None
    if row_open_bar and current_price:
        open_px   = float(row_open_bar[0])
        price_up  = current_price > open_px
        price_move = round(current_price - open_px, 4)

    # Ritmo acumulado: volumen total hoy vs media historica a la misma hora
    rows_today = session.execute(text("""
        SELECT volume FROM price_bars
        WHERE instrument='SBN26' AND interval='30m'
          AND DATE(datetime) = CURRENT_DATE
        ORDER BY datetime ASC
    """)).fetchall()
    n_bars_today = len(rows_today)
    today_cumvol = sum(float(r[0]) for r in rows_today if r[0] is not None)

    pace_ratio = avg_cumvol = None
    if n_bars_today > 0:
        row_pace = session.execute(text("""
            WITH ranked AS (
                SELECT DATE(datetime) AS day,
                       volume,
                       ROW_NUMBER() OVER (PARTITION BY DATE(datetime) ORDER BY datetime) AS bar_num
                FROM price_bars
                WHERE instrument='SBN26' AND interval='30m'
                  AND DATE(datetime) < CURRENT_DATE
            ),
            daily_cv AS (
                SELECT day, SUM(volume) AS cumvol
                FROM ranked WHERE bar_num <= :n
                GROUP BY day ORDER BY day DESC LIMIT 20
            )
            SELECT AVG(cumvol) FROM daily_cv
        """), {"n": n_bars_today}).fetchone()
        if row_pace and row_pace[0]:
            avg_cumvol = float(row_pace[0])
            pace_ratio = round(today_cumvol / avg_cumvol, 2) if avg_cumvol > 0 else None

    # Clasificacion de volumen
    high_vol  = open_ratio is not None and open_ratio >= 1.30
    low_vol   = open_ratio is not None and open_ratio < 0.80
    low_pace  = pace_ratio is not None and pace_ratio < 0.70
    vol_class = "ALTO" if high_vol else ("BAJO" if low_vol else "NORMAL")

    # Señal precio-volumen direccional
    signal = False
    if price_up is not None and open_ratio is not None:
        if direction == "LONG":
            # Conviction alcista: subida con volumen alto
            # Agotamiento bajista: bajada con volumen bajo (sin presion vendedora)
            signal = (price_up and high_vol) or (not price_up and (low_vol or low_pace))
        else:  # SHORT
            # Distribucion: subida con volumen bajo (manos debiles)
            # Conviction bajista: bajada con volumen alto
            signal = (price_up and (low_vol or low_pace)) or (not price_up and high_vol)

    return _score(signal), {
        "open_volume":       today_open,
        "avg_open_volume":   round(avg_open),
        "open_ratio":        open_ratio,
        "vol_class":         vol_class,
        "price_up_from_open": price_up,
        "price_move_from_open": price_move,
        "today_cumvol":      int(today_cumvol),
        "avg_cumvol":        int(avg_cumvol) if avg_cumvol else None,
        "pace_ratio":        pace_ratio,
        "n_bars_today":      n_bars_today,
        "prev_close_volume": prev_close_vol,
        "prev_close_ratio":  prev_close_ratio,
    }


def _score_d3(session):
    rows = session.execute(text("""
        SELECT drawdown_usd FROM daily_pnl
        WHERE date >= CURRENT_DATE - INTERVAL '14 days'
    """)).fetchall()
    if not rows:
        return 1, {"drawdown_usd": 0}
    max_dd = min(float(r[0]) for r in rows if r[0] is not None)
    return _score(max_dd >= -25_000), {"max_drawdown_usd": max_dd}


def compute_auto_scores(session: Session, direction: str, instrument: str = "SBN26") -> dict:
    direction = direction.upper()
    scores, inputs = {}, {}

    def run(key, fn, *args):
        try:
            sc, raw = fn(*args)
            scores[key] = sc
            inputs.update(raw)
        except Exception as exc:
            logger.warning(f"{key}: {exc}")
            scores[key] = None

    run("a1_spec_vs_mean",  _score_a1, session, direction)
    run("a2_spec_change",   _score_a2, session, direction)
    run("a3_comm_vs_mean",  _score_a3, session, direction)
    run("b1_spread",        _score_b1, session, direction)
    run("b2_price_vs_ma20", _score_b2, session, direction)
    run("b3_vwap",          _score_b3, session, direction, instrument)
    run("c2_open_volume",   _score_c2, session, direction)
    run("d3_drawdown",      _score_d3, session)

    for manual in ("c1_key_level", "c3_options", "d1_event_risk", "d2_liquidity"):
        scores[manual] = None

    return {"scores": scores, "inputs": inputs}


def save_scoring(session, direction, scores, inputs, manual_overrides=None, notes=None, scoring_date=None):
    target_date = scoring_date or date.today()
    direction   = direction.upper()
    all_scores  = {**scores, **(manual_overrides or {})}
    row = {"date": target_date, "direction": direction, "inputs": inputs, "notes": notes,
           **{k: v for k, v in all_scores.items() if v is not None}}

    existing = session.query(DailyScoring).filter_by(date=target_date, direction=direction).first()
    if existing:
        for k, v in row.items():
            setattr(existing, k, v)
        scoring = existing
    else:
        scoring = DailyScoring(**row)
        session.add(scoring)

    session.flush()
    scoring.compute()
    session.commit()
    session.refresh(scoring)
    return scoring


def get_latest_scoring(session, direction=None):
    q = session.query(DailyScoring).order_by(DailyScoring.date.desc())
    if direction:
        q = q.filter_by(direction=direction.upper())
    return q.limit(10).all()
