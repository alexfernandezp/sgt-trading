"""
Backtest Modelo 1 — Regimen Fundamental USDA.

Señal: STU (Stocks-to-Use) + superavit/deficit global de azucar.
Fuente: USDA FAS PSD, tabla usda_psd, country_code='WB' (World Balance).

Metodologia (sin look-ahead bias):
  - Entry: primer dia bursatil en o despues del 1 de octubre de cada anio.
  - Señal: datos FINALES del MY anterior (marketing_year=Y-1, pub_month=5 = Mayo Y),
           disponibles 5 meses ANTES de la entrada en octubre.
           Ejemplo: entry Oct 2008 → usa MY 2007 final (pub en May 2008).
  - Regimen:
      BULL  — STU < 22%   (mercado ajustado, inventarios bajos)
      NEUTRAL — 22-26%
      BEAR  — STU > 26%  (excedente, inventarios altos)
  - Conviccion extra: surplus/deficit amplifica la señal.
  - Horizontes: 3m (Jan 1), 6m (Apr 1), 12m (Oct 1 siguiente anio).
  - dir_ret = ret × signo(regimen)  → positivo = modelo acerto.

Universo: 19 observaciones (Oct 2007 — Oct 2025).
"""
import sys, os, math, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

from datetime import date, timedelta
from database import SessionLocal
from sqlalchemy import text

# ── Umbrales de regimen ────────────────────────────────────────────────────────
STU_BULL  = 22.0   # STU por debajo → BULL
STU_BEAR  = 26.0   # STU por encima → BEAR
# Conviccion alta: STU + surplus confirman la misma direccion
SURPLUS_BULL_MT = -2.0   # deficit > 2 Mt → refuerza BULL
SURPLUS_BEAR_MT =  5.0   # superavit > 5 Mt → refuerza BEAR

ENTRY_MONTH = 10   # Octubre — inicio del anio azucarero
HORIZONS    = {
    "3m":  (date(1900, 1, 1), 92),    # ~Jan 1, 3 meses
    "6m":  (date(1900, 4, 1), 182),   # ~Apr 1, 6 meses
    "12m": (date(1900, 10, 1), 365),  # ~Oct 1, 12 meses
}

ENTRY_YEARS = list(range(2007, 2026))   # entry Oct 2007 a Oct 2025


def _label_regime(stu: float, surplus_mt: float, stu_trend: float) -> tuple[str, int]:
    """Retorna (regimen, conviccion). conviccion 1-3."""
    if stu < STU_BULL:
        conv = 1
        if surplus_mt < SURPLUS_BULL_MT:
            conv += 1
        if stu_trend < -2.0:
            conv += 1
        return "BULL", min(conv, 3)
    elif stu > STU_BEAR:
        conv = 1
        if surplus_mt > SURPLUS_BEAR_MT:
            conv += 1
        if stu_trend > 2.0:
            conv += 1
        return "BEAR", min(conv, 3)
    else:
        return "NEUTRAL", 1


def _sharpe(rets: list) -> float:
    if len(rets) < 3:
        return float("nan")
    n  = len(rets)
    mu = sum(rets) / n
    var = sum((r - mu) ** 2 for r in rets) / max(n - 1, 1)
    sd  = math.sqrt(var) if var > 0 else 0
    # Anualizacion: ~2 trades/anio → sqrt(2)
    return round(mu / sd * math.sqrt(2), 2) if sd > 0 else float("nan")


def _win_rate(dir_rets: list) -> float:
    if not dir_rets:
        return float("nan")
    return round(sum(1 for r in dir_rets if r > 0) / len(dir_rets) * 100, 0)


def run():
    with SessionLocal() as s:
        # Cargar datos WB por MY (usar pub_month mas alto disponible)
        rows = s.execute(text("""
            SELECT marketing_year, pub_month, attribute_name, value_1000mt
            FROM usda_psd
            WHERE country_code = 'WB'
              AND attribute_name IN ('production','dom_consumption','ending_stocks',
                                     'beginning_stocks')
            ORDER BY marketing_year, pub_month DESC
        """)).fetchall()

        # Cargar precios SB_CONT
        px_rows = s.execute(text(
            "SELECT date, close FROM price_history "
            "WHERE instrument='SB_CONT' ORDER BY date"
        )).fetchall()

    # ── Construir serie USDA por MY (tomar pub_month mas alto = revision mas reciente) ──
    stu_series = {}   # {marketing_year: {stu, surplus_mt, prod, cons, ends}}
    by_my = {}
    for r in rows:
        my = r.marketing_year
        attr = r.attribute_name
        if my not in by_my:
            by_my[my] = {}
        if attr not in by_my[my]:   # primer registro = pub_month mas alto
            by_my[my][attr] = float(r.value_1000mt) if r.value_1000mt else None

    for my, d in by_my.items():
        prod  = d.get("production")
        cons  = d.get("dom_consumption")
        ends  = d.get("ending_stocks")
        if not (prod and cons and ends and cons > 0):
            continue
        stu = ends / cons * 100
        surplus_mt = (prod - cons) / 1000.0
        stu_series[my] = {
            "stu":        round(stu, 2),
            "surplus_mt": round(surplus_mt, 2),
            "prod_mt":    round(prod / 1000, 2),
            "cons_mt":    round(cons / 1000, 2),
            "ends_mt":    round(ends / 1000, 2),
        }

    # ── Construir mapa de precios ──────────────────────────────────────────────
    price_map = {}
    for r in px_rows:
        if r.close is not None:
            try:
                price_map[r.date] = float(r.close)
            except (TypeError, ValueError):
                pass
    price_dates = sorted(price_map)

    def _price_on_or_after(d: date):
        for pd in price_dates:
            if pd >= d:
                return pd, price_map[pd]
        return None, None

    def _exit_price(entry_date: date, months: int) -> float | None:
        target = date(entry_date.year + (1 if months >= 10 else 0),
                      ((entry_date.month + months - 1) % 12) + 1,
                      1)
        _, px = _price_on_or_after(target)
        return px

    # ── Loop principal ─────────────────────────────────────────────────────────
    records = []
    print()
    print("=" * 78)
    print("  BACKTEST MODELO 1 — REGIMEN FUNDAMENTAL USDA  (Oct 2007 – Oct 2025)")
    print("  Señal: STU global + superavit/deficit  |  SB No. 11 Continuo")
    print("=" * 78)
    print()
    print("  %-4s  %-7s  %-4s  %-8s  %-8s  %-6s  %5s  %5s  %5s  conv" % (
        "ENTR", "MY sig", "STU%", "surpl Mt", "STU tnd", "REGIM", "3m%", "6m%", "12m%"))
    print("  " + "-" * 74)

    for entry_year in ENTRY_YEARS:
        signal_my = entry_year - 1   # MY completado que usamos como señal
        if signal_my not in stu_series:
            continue
        prev_my = signal_my - 1
        if prev_my not in stu_series:
            continue

        d_curr = stu_series[signal_my]
        d_prev = stu_series[prev_my]
        stu   = d_curr["stu"]
        surp  = d_curr["surplus_mt"]
        trend = round(stu - d_prev["stu"], 2)

        regime, conv = _label_regime(stu, surp, trend)
        sign = 1.0 if regime == "BULL" else (-1.0 if regime == "BEAR" else 0.0)

        entry_date, entry_px = _price_on_or_after(date(entry_year, ENTRY_MONTH, 1))
        if not entry_px:
            continue

        rets = {}
        for label, (_, days) in HORIZONS.items():
            exit_target = entry_date + timedelta(days=days)
            _, exit_px = _price_on_or_after(exit_target)
            if exit_px and entry_px:
                ret = (exit_px - entry_px) / entry_px
                rets[label] = round(ret, 5)
            else:
                rets[label] = None

        records.append({
            "entry_year": entry_year,
            "entry_date": entry_date,
            "signal_my":  signal_my,
            "stu":        stu,
            "surplus_mt": surp,
            "stu_trend":  trend,
            "regime":     regime,
            "conv":       conv,
            "entry_px":   entry_px,
            **{f"ret_{k}": v for k, v in rets.items()},
        })

        def _fmt(v): return ("%+.1f%%" % (v * 100)) if v is not None else "  n/a"
        print("  %4d  MY%4d  %4.1f  %+8.1f  %+8.2f  %-6s  %5s  %5s  %5s  [%d]" % (
            entry_year, signal_my, stu, surp, trend, regime,
            _fmt(rets.get("3m")), _fmt(rets.get("6m")), _fmt(rets.get("12m")), conv))

    # ── Estadisticas por regimen ───────────────────────────────────────────────
    print()
    print("=" * 78)
    print("  RESULTADOS POR REGIMEN")
    print("=" * 78)

    for horizon in ["3m", "6m", "12m"]:
        print()
        print("  ── Horizonte %s ────────────────────────────────────────────────────" % horizon)
        for regime in ["BULL", "NEUTRAL", "BEAR"]:
            subset = [r for r in records if r["regime"] == regime]
            raw_rets = [r[f"ret_{horizon}"] for r in subset
                        if r.get(f"ret_{horizon}") is not None]
            if not raw_rets:
                print("  %-8s  n=0  —" % regime)
                continue
            n = len(raw_rets)
            avg_raw = sum(raw_rets) / n * 100

            if regime == "NEUTRAL":
                # NEUTRAL no es señal direccional — mostrar solo retorno promedio
                rising = sum(1 for r in raw_rets if r > 0)
                print("  %-8s  n=%2d  precio sube=%d/%d  avg_raw=%+.1f%%  [sin señal]" % (
                    regime, n, rising, n, avg_raw))
                continue

            sign = 1.0 if regime == "BULL" else -1.0
            dir_rets = [r * sign for r in raw_rets]
            wr   = _win_rate(dir_rets)
            avg_dir = sum(dir_rets) / n * 100
            sh   = _sharpe(dir_rets)
            edge = "EDGE" if (wr >= 60 and avg_dir > 1.0) else ("sin edge" if wr < 50 else "marginal")
            print("  %-8s  n=%2d  wr=%.0f%%  avg_dir=%+.1f%%  avg_raw=%+.1f%%  sharpe=%s  [%s]" % (
                regime, n, wr, avg_dir, avg_raw,
                ("%.2f" % sh) if not math.isnan(sh) else "n/a", edge))

    # ── Conviccion alta vs baja (regimen no-neutral) ───────────────────────────
    print()
    print("=" * 78)
    print("  CONVICCION: señales BULL+BEAR con conv >= 2 vs conv = 1  (horizonte 6m)")
    print("=" * 78)
    horizon = "6m"
    for min_conv in [1, 2]:
        label = ("conv >= %d" % min_conv)
        subset = [r for r in records
                  if r["regime"] != "NEUTRAL" and r["conv"] >= min_conv
                  and r.get(f"ret_{horizon}") is not None]
        if not subset:
            print("  %-12s  n=0" % label)
            continue
        sign_map = {"BULL": 1.0, "BEAR": -1.0, "NEUTRAL": 0.0}
        dir_rets = [r[f"ret_{horizon}"] * sign_map[r["regime"]] for r in subset]
        n   = len(dir_rets)
        wr  = _win_rate(dir_rets)
        avg = sum(dir_rets) / n * 100
        sh  = _sharpe(dir_rets)
        entries = sorted([str(r["entry_year"]) for r in subset])
        print("  %-12s  n=%2d  wr=%.0f%%  avg=%+.1f%%  sharpe=%s  [%s]" % (
            label, n, wr, avg,
            ("%.2f" % sh) if not math.isnan(sh) else "n/a",
            ", ".join(entries)))

    # ── Tabla resumen USDA ─────────────────────────────────────────────────────
    print()
    print("=" * 78)
    print("  SERIE HISTORICA STU MUNDIAL (MY 2000-2026)")
    print("=" * 78)
    print("  %-6s  %6s  %8s  %6s  %7s" % ("MY", "STU%", "surpl Mt", "STU tnd", "regimen"))
    print("  " + "-" * 44)
    for my in sorted(stu_series.keys()):
        d = stu_series[my]
        prev = stu_series.get(my - 1)
        trend_s = ("%+.1f" % (d["stu"] - prev["stu"])) if prev else "  n/a"
        reg, _ = _label_regime(d["stu"], d["surplus_mt"], float(trend_s) if "n/a" not in trend_s else 0)
        print("  %-6d  %5.1f%%  %+8.1f  %6s  %s" % (
            my, d["stu"], d["surplus_mt"], trend_s, reg))


if __name__ == "__main__":
    run()
