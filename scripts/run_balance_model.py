"""
SGT Live Balance Model — CLI de ejecucion y consulta.

Uso:
  py scripts/run_balance_model.py              # balance actual + forward 26/27
  py scripts/run_balance_model.py --year 2024  # marketing year especifico
  py scripts/run_balance_model.py --no-save    # calcular sin guardar en BD
  py scripts/run_balance_model.py --history    # ver ultimos 3 snapshots
  py scripts/run_balance_model.py --compare    # SGT vs USDA oficial lado a lado
"""
import sys, os, argparse, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

from database import SessionLocal


def main():
    parser = argparse.ArgumentParser(description="SGT Live Balance Model — azucar centrifugo")
    parser.add_argument("--year",    type=int, help="Marketing year (default: ultimo en BD)")
    parser.add_argument("--no-save", action="store_true", help="No guardar resultado en BD")
    parser.add_argument("--history", action="store_true", help="Ver historial de forecasts")
    parser.add_argument("--compare", action="store_true", help="Comparar SGT vs USDA oficial")
    args = parser.parse_args()

    session = SessionLocal()

    try:
        if args.history:
            _print_history(session)
            return

        if args.compare:
            _print_comparison(session, args.year)
            return

        _run_and_print(session, args.year, not args.no_save)

    finally:
        session.close()


def _run_and_print(session, marketing_year, save: bool = True):
    from services.world_balance_model import compute_sgt_balance, save_balance_forecast

    print("=" * 68)
    print("  SGT LIVE BALANCE MODEL -- Azucar Centrifugo ICE No.11")
    print("=" * 68)

    result = compute_sgt_balance(session, marketing_year)

    if not result.usda_baseline:
        print("\n  [!] Sin datos USDA. Ejecutar primero:")
        print("      py scripts/fetch_usda.py")
        return

    usda = result.usda_baseline
    br   = result.brazil
    ind  = result.india
    th   = result.thailand
    fwd  = result.forward_year

    mkt_yr  = result.marketing_year
    yr_s    = "%d/%s" % (mkt_yr, str(mkt_yr + 1)[2:])

    # ── USDA Baseline ──────────────────────────────────────────────────────────
    print("\n  USDA WASDE OFICIAL -- %s" % yr_s)
    print("  " + "-" * 52)
    _row("Produccion mundial", usda.get("production_mt"))
    _row("Consumo mundial",    usda.get("consumption_mt"))
    _row("Stocks iniciales",   usda.get("beginning_stocks_mt"))
    _row("Stocks finales",     usda.get("ending_stocks_mt"))
    stu_usda = usda.get("stocks_to_use_pct")
    if stu_usda:
        print("  Stocks/Uso (STU) : %6.1f%%  %s" % (stu_usda, _stu_tag(stu_usda)))

    eu   = result.eu

    # ── Ajustes por pais ──────────────────────────────────────────────────────
    print("\n  AJUSTES SGT POR PAIS")
    print("  " + "-" * 52)
    _country_row("Brasil   ", br)
    _country_row("India    ", ind)
    _country_row("Tailandia", th)
    _country_row("Union Eur", eu)

    total_adj = sum([
        (br.override_mt  if br  else 0),
        (ind.override_mt if ind else 0),
        (th.override_mt  if th  else 0),
        (eu.override_mt  if eu  else 0),
    ])
    sign_s = "+" if total_adj >= 0 else ""
    print("  Total adj           : %s%.2f Mt vs USDA" % (sign_s, total_adj))

    # China (informativo — ya en WB baseline)
    ch = result.china_info
    if ch:
        print()
        print("  CHINA (informativo — ya incluido en WB USDA):")
        print("  Produccion: %.2f Mt | Consumo: %.2f Mt | Deficit: %.2f Mt (importador neto)" % (
              ch.get("production_mt", 0), ch.get("consumption_mt", 0), ch.get("deficit_mt", 0)))

    # ── Balance SGT ───────────────────────────────────────────────────────────
    print("\n  SGT BALANCE AJUSTADO -- %s" % yr_s)
    print("  " + "-" * 52)
    _row("Produccion SGT",  result.sgt_prod_mt)
    _row("Consumo SGT",     result.sgt_cons_mt)
    _row("Stocks iniciales",result.sgt_beg_mt)
    _row("Stocks finales",  result.sgt_end_mt)

    stu = result.sgt_stu_pct
    if stu:
        stu_tag = _stu_tag(stu)
        print("  Stocks/Uso (STU) : %6.1f%%  %s" % (stu, stu_tag))

    surplus = result.sgt_surplus_mt
    if surplus is not None:
        s_label = "SURPLUS" if surplus >= 0 else "DEFICIT"
        s_sign  = "+" if surplus >= 0 else ""
        print("  Balance S/D      : %s%.2f Mt  [%s]" % (s_sign, surplus, s_label))

    # Divergencia SGT vs USDA
    stu_div = result.stu_divergence
    if stu_div is not None and stu_usda is not None:
        div_sign = "+" if stu_div >= 0 else ""
        print("  Divergencia STU  : %s%.1f pp  (USDA=%.1f%%  SGT=%.1f%%)" % (
              div_sign, stu_div, stu_usda, stu))

    # ── Señal de trading ──────────────────────────────────────────────────────
    print("\n  SEÑAL DE TRADING")
    print("  " + "-" * 52)
    print("  Bias             : %-20s" % result.bias)
    print("  Conviction       : %d / 100" % result.conviction_score)
    print("  Confidence       : %.2f  [%s]" % (result.confidence_score,
          _conf_label(result.confidence_score)))
    print("  Leverage Cap     : %.0f%%  %s" % (result.leverage_cap_pct,
          _leverage_label(result.leverage_cap_pct)))

    if result.iv_warning:
        print("  [IV] %s" % result.iv_warning)

    # Fuentes de datos activas
    print()
    print("  FUENTES ACTIVAS")
    print("  " + "-" * 52)
    print("  India     : %s (adj=%+.2f Mt)" % (
        result.india.data_source    if result.india    else "usda",
        result.india.override_mt    if result.india    else 0))
    print("  Tailandia : %s (adj=%+.2f Mt)" % (
        result.thailand.data_source if result.thailand else "usda",
        result.thailand.override_mt if result.thailand else 0))
    print("  Brasil    : %s (adj=%+.2f Mt)" % (
        result.brazil.data_source   if result.brazil   else "usda",
        result.brazil.override_mt   if result.brazil   else 0))
    print("  Union Eur : %s (adj=%+.2f Mt)" % (
        result.eu.data_source       if result.eu       else "usda",
        result.eu.override_mt       if result.eu       else 0))

    # ── Data freshness ────────────────────────────────────────────────────────
    print("\n  FRESCURA DE DATOS")
    print("  " + "-" * 52)
    for source, age_d in result.data_freshness.items():
        status = "OK" if age_d <= 45 else ("WARN" if age_d <= 90 else "STALE")
        age_s  = "%3d dias" % age_d if age_d < 9000 else "sin datos"
        print("  %-10s : %s  [%s]" % (source, age_s, status))

    # ── Pesos activos ─────────────────────────────────────────────────────────
    print("\n  PESOS APLICADOS")
    print("  " + "-" * 52)
    for src, w in result.signal_weights.items():
        drift = result.weight_drift.get(src)
        drift_s = ""
        if drift and drift < 0.99:
            drift_s = "  <- DRIFT activo (%.2f)" % drift
        print("  %-25s : %.3f%s" % (src, w, drift_s))

    # ── Advertencias ─────────────────────────────────────────────────────────
    if result.warnings:
        print("\n  ADVERTENCIAS")
        for w in result.warnings:
            print("  [!] %s" % w)

    # ── Proyeccion 26/27 ──────────────────────────────────────────────────────
    if fwd:
        fwd_yr  = fwd.get("marketing_year", mkt_yr + 1)
        fwd_yr_s = "%d/%s" % (fwd_yr, str(fwd_yr + 1)[2:])
        lst_adj = fwd.get("lst_trend_adj", 0)

        print("\n  PROYECCION FORWARD -- %s" % fwd_yr_s)
        print("  " + "-" * 52)
        print("  Metodo           : %s" % fwd.get("method", "?"))
        if lst_adj:
            print("  Ajuste LST trend : %+.2f%%  (heat stress acumulado 2yr)" % lst_adj)
        unica_note = fwd.get("brazil_unica_note")
        if unica_note:
            print("  Brasil UNICA     : %s" % unica_note)
        _row("Produccion fwd",   fwd.get("production_mt"))
        _row("Consumo fwd",      fwd.get("consumption_mt"))
        _row("Stocks iniciales", fwd.get("beginning_stocks_mt"))
        _row("Stocks finales",   fwd.get("ending_stocks_mt"))
        fwd_stu = fwd.get("stocks_to_use_pct")
        if fwd_stu:
            print("  Stocks/Uso fwd   : %6.1f%%  %s" % (fwd_stu, _stu_tag(fwd_stu)))
        fwd_sur = fwd.get("surplus_mt")
        if fwd_sur is not None:
            s_label = "SURPLUS" if fwd_sur >= 0 else "DEFICIT"
            s_sign  = "+" if fwd_sur >= 0 else ""
            print("  Balance S/D fwd  : %s%.2f Mt  [%s]" % (s_sign, fwd_sur, s_label))

    print()

    # ── Guardar ───────────────────────────────────────────────────────────────
    if save:
        ok = save_balance_forecast(session, result)
        if ok:
            print("  [OK] Forecast guardado en BD (sgt_balance_forecast)")
        else:
            print("  [!] Error al guardar forecast — ver logs")


# ── Historial ──────────────────────────────────────────────────────────────────

def _print_history(session):
    from models.market_data import SgtBalanceForecast
    from sqlalchemy import select

    rows = session.execute(
        select(SgtBalanceForecast)
        .order_by(SgtBalanceForecast.forecast_date.desc())
        .limit(10)
    ).scalars().all()

    if not rows:
        print("  Sin historial en BD. Ejecutar sin --history primero.")
        return

    print("=" * 72)
    print("  HISTORIAL SGT BALANCE FORECASTS")
    print("=" * 72)
    print("  %-12s %-6s %-8s %-8s %-8s %-7s %-6s %-12s" % (
          "Fecha", "Año", "Prod", "Cons", "End", "STU%", "Conf", "Bias"))
    print("  " + "-" * 68)

    for r in rows:
        print("  %-12s %-6s %-8s %-8s %-8s %-7s %-6s %-12s" % (
            str(r.forecast_date),
            "%d/%s" % (r.marketing_year, str(r.marketing_year + 1)[2:]),
            "%.1f" % float(r.sgt_prod_mt) if r.sgt_prod_mt else "-",
            "%.1f" % float(r.sgt_cons_mt) if r.sgt_cons_mt else "-",
            "%.1f" % float(r.sgt_end_mt)  if r.sgt_end_mt  else "-",
            "%.1f%%" % float(r.sgt_stu_pct) if r.sgt_stu_pct else "-",
            "%.2f"  % float(r.confidence_score) if r.confidence_score else "-",
            r.bias or "-",
        ))


# ── Comparativa SGT vs USDA ────────────────────────────────────────────────────

def _print_comparison(session, marketing_year=None):
    from services.world_balance_model import compute_sgt_balance

    result = compute_sgt_balance(session, marketing_year)
    usda   = result.usda_baseline or {}

    if not usda:
        print("  Sin datos USDA.")
        return

    mkt_yr  = result.marketing_year
    yr_s    = "%d/%s" % (mkt_yr, str(mkt_yr + 1)[2:])

    print("=" * 72)
    print("  COMPARATIVA SGT vs USDA -- %s" % yr_s)
    print("=" * 72)
    print("  %-22s %10s %10s %10s" % ("", "USDA WASDE", "SGT Adj", "Diferencia"))
    print("  " + "-" * 58)

    rows = [
        ("Produccion (Mt)",      usda.get("production_mt"),  result.sgt_prod_mt),
        ("Consumo (Mt)",         usda.get("consumption_mt"), result.sgt_cons_mt),
        ("Stocks finales (Mt)",  usda.get("ending_stocks_mt"), result.sgt_end_mt),
        ("STU (%)",              usda.get("stocks_to_use_pct"), result.sgt_stu_pct),
        ("Surplus/Deficit (Mt)", None, result.sgt_surplus_mt),
    ]

    for label, usda_v, sgt_v in rows:
        usda_s = "%.2f" % usda_v if usda_v is not None else "-"
        sgt_s  = "%.2f" % sgt_v  if sgt_v  is not None else "-"
        diff_s = ""
        if usda_v is not None and sgt_v is not None:
            diff = sgt_v - usda_v
            diff_s = "%+.2f" % diff
        print("  %-22s %10s %10s %10s" % (label, usda_s, sgt_s, diff_s))

    print()
    print("  Confidence score : %.2f  (%s)" % (result.confidence_score,
          _conf_label(result.confidence_score)))
    print("  Leverage cap     : %.0f%%  %s" % (result.leverage_cap_pct,
          _leverage_label(result.leverage_cap_pct)))

    # Ajustes detallados
    print()
    print("  Ajustes individuales:")
    for cb in [result.brazil, result.india, result.thailand, result.eu]:
        if cb is None:
            continue
        if cb.override_mt:
            pct = cb.adj_pct
            pct_s = "(%+.1f%%)" % pct if pct is not None else ""
            print("    %-10s : %+.2f Mt  %s  [%s]" % (
                  cb.name, cb.override_mt, pct_s, cb.data_source))
            for note in cb.notes:
                print("              %s" % note)


# ── Helpers de formato ─────────────────────────────────────────────────────────

def _row(label: str, value):
    if value is not None:
        print("  %-18s : %6.2f Mt" % (label, float(value)))


def _country_row(label: str, cb):
    if cb is None:
        return
    usda_s = "%.2f" % cb.usda_prod_mt if cb.usda_prod_mt else "-"
    sgt_s  = "%.2f" % cb.sgt_prod_mt  if cb.sgt_prod_mt  else "-"
    adj_s  = ""
    if cb.override_mt:
        sign = "+" if cb.override_mt >= 0 else ""
        adj_s = "  (adj %s%.2f Mt)" % (sign, cb.override_mt)
    conf_s = "c=%.2f" % cb.confidence
    print("  %s : USDA=%s  SGT=%s%s  [%s  %s]" % (
          label, usda_s, sgt_s, adj_s, cb.data_source, conf_s))


def _stu_tag(stu: float) -> str:
    if stu < 28:
        return "[ESCASEZ CRITICA — STRONG LONG]"
    elif stu < 32:
        return "[mercado ajustado — LONG]"
    elif stu < 38:
        return "[zona equilibrada — neutral]"
    elif stu < 42:
        return "[excedente moderado — SHORT]"
    else:
        return "[surplus evidente — STRONG SHORT]"


def _conf_label(conf: float) -> str:
    if conf >= 0.80:
        return "alineacion plena"
    elif conf >= 0.70:
        return "datos aceptables"
    elif conf >= 0.60:
        return "datos parciales"
    else:
        return "VALLEY — baja confianza"


def _leverage_label(cap: float) -> str:
    if cap >= 100:
        return "[posicion completa]"
    elif cap >= 75:
        return "[gating leve]"
    elif cap >= 50:
        return "[gating moderado]"
    elif cap >= 25:
        return "[gating severo]"
    else:
        return "[EXPOSICION MINIMA]"


if __name__ == "__main__":
    main()
