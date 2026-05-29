"""
Curve Analysis & Calendar Spread Signals — ICE No.11 Sugar.

Va más allá del signal binario LONG/SHORT: modela la estructura de plazos completa
para identificar oportunidades de spread trading y anomalías en la curva.

Aplicaciones:
  1. Calendar spread trades: compra/venta de spreads específicos por fundamentales
  2. Butterfly spreads: valor relativo de un vencimiento vs sus vecinos
  3. Fundamental linkage: mapa de ventanas de producción por contrato
  4. Roll cost tracker: coste de mantener posición outright a través del tiempo
  5. Curve shape anomalies: inversiones locales, jorobas, fat belly

Lógica de spread trades (ejemplos):
  V26/H27 LONG SPREAD (buy V26, sell H27):
    → apostar a que la prima de H27 se estrecha (India/TH menos apretados de lo esperado)
  V26/H27 SHORT SPREAD (sell V26, buy H27):
    → apostar a que H27 se aprecia más (India/TH más apretados de lo esperado)

Ventana de producción por contrato ICE No.11:
  SBN26 (Jul'26): Brasil pico molienda (Jun-Nov). India/TH fuera de temporada.
  SBV26 (Oct'26): Brasil final temporada. India/TH aún sin arrancar.
  SBH27 (Mar'27): Pico India (Oct'26-Mar'27) + Tailandia (Nov'26-Apr'27). Brasil parado.
  SBK27 (May'27): Brasil arrancando. India/TH cerrando.
  SBN27 (Jul'27): Brasil pico. India/TH fuera de temporada.
  SBV27 (Oct'27): Idem SBV26 un año después.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Mapa contrato → ventana de producción dominante
_CONTRACT_FUNDAMENTALS = {
    "SBN26": {
        "dominant": "Brazil peak",
        "brazil":   "peak",      # Jun-Nov crushing
        "india":    "off",
        "thailand": "off",
        "notes":    "Brasil domina — UNICA/MAPA determinantes",
    },
    "SBV26": {
        "dominant": "Brazil late / India start",
        "brazil":   "late",
        "india":    "early",
        "thailand": "off",
        "notes":    "Transición: Brasil cerrando, India arrancando (Oct)",
    },
    "SBH27": {
        "dominant": "India + Thailand peak",
        "brazil":   "off",
        "india":    "peak",      # pico Nov-Mar
        "thailand": "peak",      # pico Nov-Apr
        "notes":    "Más sensible a ISMA/OCSB. Cualquier shock India/TH aquí.",
    },
    "SBK27": {
        "dominant": "Brazil start / India late",
        "brazil":   "early",
        "india":    "late",
        "thailand": "late",
        "notes":    "Brasil arrancando. India/TH cerrando campaña.",
    },
    "SBN27": {
        "dominant": "Brazil peak",
        "brazil":   "peak",
        "india":    "off",
        "thailand": "off",
        "notes":    "Replicación N26 un año después. UNICA 26/27 determinante.",
    },
    "SBV27": {
        "dominant": "Brazil late / India start",
        "brazil":   "late",
        "india":    "early",
        "thailand": "off",
        "notes":    "Mismo perfil que SBV26.",
    },
}

# Meses entre vencimientos consecutivos
_MONTHS_BETWEEN = {
    ("SBN26", "SBV26"): 3,
    ("SBV26", "SBH27"): 5,
    ("SBH27", "SBK27"): 2,
    ("SBK27", "SBN27"): 2,
    ("SBN27", "SBV27"): 3,
}

SOFR           = 0.045
STORAGE_M      = 0.012   # c/lb/mes
QUALITY_M      = 0.003   # c/lb/mes


@dataclass
class SpreadTrade:
    label:          str          # ej. "V26/H27"
    near:           str          # contrato a vender (short spread)
    far:            str          # contrato a comprar (long spread)
    near_price:     float
    far_price:      float
    spread:         float        # near - far (positivo = backwardation)
    full_carry:     float        # carry esperado (siempre positivo = normal contango)
    excess_carry:   float        # |spread| - full_carry si contango; negativo si back
    carry_ratio:    float        # |spread| / full_carry
    percentile:     Optional[float]      # rango 0-100 en ventana rolling 52w
    modified_z:     Optional[float]      # Iglewicz-Hoaglin robust Z (MAD-based)
    regime:         str
    fundamental_view: str        # interpretación fundamental
    trade_signal:   str          # "BUY_SPREAD" / "SELL_SPREAD" / "NEUTRAL"
    conviction:     str          # "HIGH" / "MEDIUM" / "LOW"
    rationale:      str


@dataclass
class ButterflySpread:
    label:          str          # ej. "V26/H27/K27"
    wing_near:      str
    body:           str
    wing_far:       str
    value:          float        # body - (wing_near + wing_far) / 2
    interpretation: str


@dataclass
class CurveSnapshot:
    prices:         dict[str, float]
    spreads:        list[SpreadTrade]
    butterflies:    list[ButterflySpread]
    roll_cost_ann:  float           # coste de roll anualizado en c/lb
    curve_shape:    str             # "NORMAL_CONTANGO" / "SUPER_CONTANGO" / "BACKWARDATION" / "HUMPED"
    anomalies:      list[str]       # inversiones locales, humps, etc.
    top_trade:      Optional[SpreadTrade]


def _carry(price: float, months: int) -> float:
    return (SOFR * price / 12 + STORAGE_M + QUALITY_M) * months


def _classify_spread(spread: float, full_carry: float,
                     rs: dict,
                     near: str = "", far: str = "") -> tuple[str, str, str, str]:
    """
    Retorna (regime, trade_signal, conviction, rationale).
    spread = near - far (positivo = backwardation).
    full_carry > 0 siempre.
    rs = output de robust_stats() — percentile rank + modified Z (AND gate).

    Distinción crítica:
      Backwardation ESTADÍSTICA (nearby, N26/V26): probable fade → SELL SPREAD
      Backwardation ESTRUCTURAL (cross-season, ej. H27/K27):
        near = India/TH peak, far = Brazil start → backwardation es NATURAL.
        Señal: NEUTRAL si en rango histórico, BUY_SPREAD si mZ extremo positivo
        (mercado sub-precia el tightening), SELL_SPREAD solo si mZ extremo negativo.
    """
    # Identificar si es un spread cross-season (diferente driver fundamental)
    near_fund = _CONTRACT_FUNDAMENTALS.get(near, {})
    far_fund  = _CONTRACT_FUNDAMENTALS.get(far,  {})
    cross_season = (
        near_fund.get("india") in ("peak",) and far_fund.get("brazil") in ("peak", "early")
    ) or (
        near_fund.get("thailand") in ("peak",) and far_fund.get("brazil") in ("peak", "early")
    )

    # Extraer estadísticas robustas del AND gate
    mz              = rs.get("modified_z") or 0.0
    pct             = rs.get("percentile_rank")
    is_extreme_high = rs.get("is_extreme_high", False)
    is_extreme_low  = rs.get("is_extreme_low",  False)

    carry_ratio = -spread / full_carry if spread < 0 else 0.0

    if spread > 0:
        regime = "BACKWARDATION"
        if cross_season:
            # Backwardation estructural: India/TH peak vs Brazil start.
            # La prima de near es NATURAL — no fadear automáticamente.
            # Solo actuamos cuando la AND gate confirma extremo estadístico.
            if is_extreme_high:
                # Near caro en percentil Y mZ ambos confirman → pequeño fade
                signal, conviction = "SELL_SPREAD", "LOW"
                rationale = (
                    f"Backwardation estructural {spread:+.3f} c/lb (near={near_fund.get('dominant')}, "
                    f"far={far_fund.get('dominant')}). pct={pct:.0f} mZ={mz:.2f} — ambas capas "
                    f"confirman extremo alto → fade marginal posible, pero fundamentales pueden sostener prima."
                )
            elif is_extreme_low:
                # Near históricamente barato con doble confirmación → BUY SPREAD
                signal, conviction = "BUY_SPREAD", "MEDIUM"
                rationale = (
                    f"Backwardation estructural {spread:+.3f} c/lb MENOR de lo histórico "
                    f"(pct={pct:.0f}, mZ={mz:.2f}). Near={near_fund.get('dominant')} sub-valorado. "
                    f"Si India/TH apretados como se espera → BUY SPREAD (long {near.replace('SB','')})."
                )
            else:
                # Backwardation en rango histórico normal — la dirección depende de la
                # vista fundamental India/TH, no de estadística
                signal, conviction = "FUNDAMENTAL_LONG_NEAR", "MEDIUM"
                rationale = (
                    f"Backwardation estructural {spread:+.3f} c/lb: {near_fund.get('dominant')} "
                    f"vs {far_fund.get('dominant')} (pct={pct if pct is not None else 'N/A'}). "
                    f"Prima justificada por estacionalidad. "
                    f"BUY near (long {near.replace('SB','')}) si India/TH apretada; "
                    f"SELL near si crees mercado sobreestima tightening."
                )
        else:
            # Backwardation en spread nearby → estadística → fade
            signal     = "SELL_SPREAD"
            conviction = "HIGH" if is_extreme_high else "MEDIUM"
            rationale  = (
                f"Backwardation nearby {spread:+.3f} c/lb (pct={pct if pct is not None else 'N/A'}, "
                f"mZ={mz:.2f}). Mean reversion estadistica → SELL SPREAD "
                f"(short {near.replace('SB','')}, long {far.replace('SB','')})."
            )
    elif carry_ratio > 2.0:
        regime     = "SUPER_CONTANGO"
        signal     = "BUY_SPREAD"
        conviction = "HIGH" if carry_ratio > 3.0 else "MEDIUM"
        rationale  = (
            f"Contango {-spread:.3f} c/lb = {carry_ratio:.1f}x full carry "
            f"({full_carry:.3f} c/lb) → mercado sobre-premia diferimiento. "
            f"BUY SPREAD (long {near.replace('SB','')}, short {far.replace('SB','')})."
        )
    elif carry_ratio > 1.2:
        regime     = "FULL_CARRY"
        signal     = "NEUTRAL"
        conviction = "LOW"
        rationale  = (
            f"Contango {-spread:.3f} c/lb aprox full carry ({full_carry:.3f} c/lb). "
            f"Mercado equilibrado."
        )
    elif 0 <= carry_ratio <= 1.2:
        regime     = "WEAK_CONTANGO"
        if cross_season and carry_ratio < 0.5:
            signal     = "BUY_SPREAD"
            conviction = "MEDIUM"
            rationale  = (
                f"Contango {-spread:.3f} c/lb << full carry. Near={near_fund.get('dominant')} "
                f"barato vs far={far_fund.get('dominant')}. Mercado no precia tightening India/TH. "
                f"BUY SPREAD si vista fundamental alcista India/TH."
            )
        else:
            signal     = "NEUTRAL"
            conviction = "LOW"
            rationale  = f"Contango {-spread:.3f} c/lb bajo full carry ({full_carry:.3f}). Sin señal clara."
    else:
        regime, signal, conviction = "NEUTRAL", "NEUTRAL", "LOW"
        rationale = "Sin señal clara."

    # AND gate ajusta convicción final (salvo en FUNDAMENTAL_LONG_NEAR donde
    # la convicción viene de la vista fundamental, no de estadística)
    if signal != "FUNDAMENTAL_LONG_NEAR":
        if (is_extreme_high or is_extreme_low) and conviction == "MEDIUM":
            conviction = "HIGH"
        elif not is_extreme_high and not is_extreme_low and mz != 0.0 and abs(mz) < 0.5:
            if conviction == "HIGH":
                conviction = "MEDIUM"

    return regime, signal, conviction, rationale


def _fundamental_view(near: str, far: str, spread: float) -> str:
    """Interpretación fundamental del spread basada en ventanas de producción."""
    fn  = _CONTRACT_FUNDAMENTALS.get(near, {})
    ff  = _CONTRACT_FUNDAMENTALS.get(far,  {})
    dom_near = fn.get("dominant", near)
    dom_far  = ff.get("dominant", far)

    if spread > 0:
        return (f"Backwardation {near}/{far}: mercado valora más el {dom_near} "
                f"que el {dom_far}. Presión física en nearby.")
    else:
        return (f"Contango {near}/{far}: {dom_far} cotiza a prima sobre {dom_near}. "
                f"Mercado anticipa tightening en ventana {dom_far}.")


def _get_historical_spread(session: Session, near: str, far: str,
                            weeks: int = 52) -> list[float]:
    """Historia del spread near-far para Z-score."""
    from sqlalchemy import text
    rows = session.execute(text("""
        SELECT n.close - f.close AS spread
        FROM price_history n
        JOIN price_history f ON n.date = f.date AND f.instrument = :far
        WHERE n.instrument = :near
        ORDER BY n.date DESC
        LIMIT :w
    """), {"near": near, "far": far, "w": weeks}).fetchall()
    return [float(r[0]) for r in rows if r[0] is not None]


def analyze_curve(session: Session) -> CurveSnapshot:
    """
    Análisis completo de la curva de futuros ICE No.11.

    Returns CurveSnapshot con spreads, butterflies, anomalías y top trade.
    """
    from services.spread_signal import _fetch_live_prices, _TERM_STRUCTURE_CONTRACTS
    from sqlalchemy import text

    # Precios live → DB fallback
    live   = _fetch_live_prices()
    prices = {}
    for c in _TERM_STRUCTURE_CONTRACTS:
        if c in live:
            prices[c] = live[c]
        else:
            row = session.execute(text(
                "SELECT close FROM price_history WHERE instrument=:c ORDER BY date DESC LIMIT 1"
            ), {"c": c}).fetchone()
            if row:
                prices[c] = float(row[0])

    if len(prices) < 2:
        return CurveSnapshot(prices={}, spreads=[], butterflies=[], roll_cost_ann=0.0,
                             curve_shape="INSUFFICIENT_DATA", anomalies=[], top_trade=None)

    front_price = prices.get("SBN26", prices.get("SBV26", 15.0))

    from services.stats_utils import robust_stats

    # ── Spreads consecutivos ──────────────────────────────────────────────────
    spreads: list[SpreadTrade] = []
    for (near, far), months in _MONTHS_BETWEEN.items():
        if near not in prices or far not in prices:
            continue
        spread     = prices[near] - prices[far]
        fc         = _carry(front_price, months)
        hist       = _get_historical_spread(session, near, far, weeks=52)
        rs         = robust_stats(hist, spread) if hist else {}
        carry_r    = -spread / fc if fc > 0 and spread < 0 else (0 if spread >= 0 else 0)
        excess_c   = -spread - fc if spread < 0 else spread

        regime, signal, conviction, rationale = _classify_spread(spread, fc, rs, near=near, far=far)
        fview = _fundamental_view(near, far, spread)
        label = f"{near.replace('SB','')}/{far.replace('SB','')}"

        spreads.append(SpreadTrade(
            label=label, near=near, far=far,
            near_price=prices[near], far_price=prices[far],
            spread=round(spread, 4), full_carry=round(fc, 4),
            excess_carry=round(excess_c, 4), carry_ratio=round(carry_r, 2),
            percentile=rs.get("percentile_rank"), modified_z=rs.get("modified_z"),
            regime=regime, fundamental_view=fview,
            trade_signal=signal, conviction=conviction,
            rationale=rationale,
        ))

    # ── Butterflies ───────────────────────────────────────────────────────────
    butterflies: list[ButterflySpread] = []
    triplets = [
        ("SBN26", "SBV26", "SBH27"),
        ("SBV26", "SBH27", "SBK27"),
        ("SBH27", "SBK27", "SBN27"),
        ("SBK27", "SBN27", "SBV27"),
    ]
    for wn, body, wf in triplets:
        if wn not in prices or body not in prices or wf not in prices:
            continue
        val   = prices[body] - (prices[wn] + prices[wf]) / 2
        label = f"{wn.replace('SB','')}/{body.replace('SB','')}/{wf.replace('SB','')}"
        if val > 0.10:
            interp = f"{body.replace('SB','')} caro vs vecinos (+{val:.3f}) — posible mean reversion SHORT body"
        elif val < -0.10:
            interp = f"{body.replace('SB','')} barato vs vecinos ({val:.3f}) — posible mean reversion LONG body"
        else:
            interp = f"{body.replace('SB','')} a fair value vs vecinos ({val:+.3f})"
        butterflies.append(ButterflySpread(
            label=label, wing_near=wn, body=body, wing_far=wf,
            value=round(val, 4), interpretation=interp,
        ))

    # ── Anomalías ─────────────────────────────────────────────────────────────
    anomalies: list[str] = []
    sorted_contracts = [c for c in _TERM_STRUCTURE_CONTRACTS if c in prices]
    for i in range(len(sorted_contracts) - 1):
        c1, c2 = sorted_contracts[i], sorted_contracts[i + 1]
        if prices[c1] > prices[c2]:
            anomalies.append(
                f"Inversión local: {c1.replace('SB','')} ({prices[c1]:.3f}) > "
                f"{c2.replace('SB','')} ({prices[c2]:.3f}) — backwardation en este tramo"
            )

    # ── Forma de la curva ─────────────────────────────────────────────────────
    vals = [prices[c] for c in sorted_contracts]
    if all(vals[i] <= vals[i+1] for i in range(len(vals)-1)):
        shape = "NORMAL_CONTANGO"
    elif all(vals[i] >= vals[i+1] for i in range(len(vals)-1)):
        shape = "FULL_BACKWARDATION"
    elif any(vals[i] > vals[i+1] for i in range(len(vals)-1)):
        shape = "HUMPED" if anomalies else "MIXED"
    else:
        shape = "NORMAL_CONTANGO"

    # ── Roll cost anualizado ──────────────────────────────────────────────────
    # Coste de rodar de front a siguiente vencimiento, anualizado
    front_spread_obj = next((s for s in spreads if s.label.startswith("N26/")), None)
    if front_spread_obj:
        months_front = _MONTHS_BETWEEN.get(("SBN26", "SBV26"), 3)
        roll_cost_ann = round(-front_spread_obj.spread / months_front * 12, 4)
    else:
        roll_cost_ann = 0.0

    # ── Top trade: mayor convicción y zscore extremo ──────────────────────────
    ranked = [s for s in spreads if s.trade_signal != "NEUTRAL"]
    ranked.sort(key=lambda s: (
        {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[s.conviction],
        -abs(s.modified_z or 0)
    ))
    top_trade = ranked[0] if ranked else None

    return CurveSnapshot(
        prices=prices, spreads=spreads, butterflies=butterflies,
        roll_cost_ann=roll_cost_ann, curve_shape=shape,
        anomalies=anomalies, top_trade=top_trade,
    )


def format_curve_report(snap: CurveSnapshot) -> str:
    """Formatea el análisis de curva para mostrar en terminal."""
    lines = []
    lines.append("\n=== CURVE ANALYSIS - ICE No.11 ===")

    lines.append("\nTerm Structure (live prices):")
    for c, p in snap.prices.items():
        fund = _CONTRACT_FUNDAMENTALS.get(c, {}).get("dominant", "")
        lines.append(f"  {c.replace('SB',''):6} {p:7.4f} c/lb   [{fund}]")

    lines.append(f"\nCurve shape: {snap.curve_shape}  |  Roll cost ann.: {snap.roll_cost_ann:+.3f} c/lb/yr")

    if snap.anomalies:
        lines.append("\nAnomalías:")
        for a in snap.anomalies:
            lines.append(f"  [!] {a}")

    lines.append("\nCalendar Spreads:")
    lines.append(f"  {'Spread':12} {'Near':7} {'Far':7} {'Spread':>8} {'Carry':>7} {'Ratio':>6} {'Pct':>5} {'mZ':>6}  Regime          Signal")
    lines.append("  " + "-" * 100)
    for s in snap.spreads:
        pct_str = f"{s.percentile:.0f}" if s.percentile is not None else "N/A"
        mz_str  = f"{s.modified_z:+.2f}" if s.modified_z is not None else "  N/A"
        lines.append(
            f"  {s.label:12} {s.near_price:7.4f} {s.far_price:7.4f} "
            f"{s.spread:+8.4f} {s.full_carry:7.4f} {s.carry_ratio:6.2f} "
            f"{pct_str:>5} {mz_str:>6}  "
            f"{s.regime:18} {s.trade_signal} ({s.conviction})"
        )

    lines.append("\nButterfly Spreads (value = body - avg(wings)):")
    for b in snap.butterflies:
        lines.append(f"  {b.label:20} value={b.value:+.4f}  {b.interpretation}")

    if snap.top_trade:
        t = snap.top_trade
        lines.append(f"\n[TOP SPREAD TRADE]: {t.label}")
        lines.append(f"  Signal: {t.trade_signal} | Conviction: {t.conviction} | pct={t.percentile} mZ={t.modified_z}")
        lines.append(f"  {t.rationale}")
        lines.append(f"  Fundamental: {t.fundamental_view}")

    return "\n".join(lines)
