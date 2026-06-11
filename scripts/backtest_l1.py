"""
Backtest histórico de señales L1 (COT + precio) sobre SB_CONT 2007-hoy.
Sin look-ahead bias: usa report_date + 3 días como fecha de conocimiento del COT.

Señales:
  A1 — COT speculator net vs percentil 3yr rolling:
         net > P75 → STRETCHED (bearish precio, señal SHORT)
         net < P25 → DEPRESSED (bullish precio, señal LONG)
  B2 — precio vs MA 26 semanas (182 días):
         precio < MA → LONG
         precio > MA → SHORT

Combinaciones evaluadas:
  1) A1 solo
  2) A1 + B2 (ambos alineados)
  3) A1_DEPRESSED (solo long side)
  4) A1_STRETCHED (solo short side)

Resultado: win rate, avg return, Sharpe, nro trades por horizonte (5/10/20d)
"""
import sys, os, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

import math
from datetime import date, timedelta
from database import SessionLocal
from sqlalchemy import text

ROLLING_WINDOW_DAYS = 3 * 365   # 3 años para percentil COT
MA_DAYS             = 182       # 26 semanas
COT_PUB_LAG         = 3         # días desde report_date a publicación CFTC
HORIZONS            = [5, 10, 20]
COT_LONG_PCT        = 75        # threshold STRETCHED
COT_SHORT_PCT       = 25        # threshold DEPRESSED


def _sharpe(rets: list[float]) -> float:
    if len(rets) < 4:
        return float("nan")
    n   = len(rets)
    mu  = sum(rets) / n
    var = sum((r - mu) ** 2 for r in rets) / (n - 1)
    sd  = math.sqrt(var) if var > 0 else 0
    return round(mu / sd * math.sqrt(52), 2) if sd > 0 else float("nan")


def _win_rate(dir_rets: list[float]) -> float:
    if not dir_rets:
        return float("nan")
    wins = sum(1 for r in dir_rets if r > 0)
    return round(wins / len(dir_rets) * 100, 1)


def _stats(label: str, dir_rets: list, horizon: int):
    dir_rets = [float(x) for x in dir_rets if x is not None]
    if not dir_rets:
        return "  %-35s  n=0  —" % label
    n   = len(dir_rets)
    avg = sum(dir_rets) / n * 100
    wr  = _win_rate(dir_rets)
    sh  = _sharpe(dir_rets)
    return "  %-35s  n=%-4d  avg=%+.2f%%  wr=%.0f%%  sharpe=%s  (%dd)" % (
        label, n, avg, wr, ("%.2f" % sh) if not math.isnan(sh) else "n/a", horizon)


def run():
    with SessionLocal() as s:
        # Cargar precios diarios SB_CONT
        price_rows = s.execute(text(
            "SELECT date, close FROM price_history "
            "WHERE instrument='SB_CONT' ORDER BY date"
        )).fetchall()

        # Cargar COT (especuladores + comerciales nets)
        cot_rows = s.execute(text(
            "SELECT report_date, ncomm_net FROM cot_data "
            "ORDER BY report_date"
        )).fetchall()

    if not price_rows:
        print("ERROR: sin datos en price_history para SB_CONT")
        return
    if not cot_rows:
        print("ERROR: sin datos en cot_data")
        return

    # Indexar precios por fecha
    price_map: dict = {}
    for r in price_rows:
        try:
            if r.close is not None:
                price_map[r.date] = float(r.close)
        except (TypeError, ValueError):
            pass
    price_dates = sorted(price_map)

    def _price_on_or_after(d: date) -> tuple[date, float] | None:
        for pd in price_dates:
            if pd >= d:
                return pd, price_map[pd]
        return None

    def _price_n_days_after(ref_date: date, n_cal_days: int) -> float | None:
        target = ref_date + timedelta(days=n_cal_days)
        result = _price_on_or_after(target)
        return result[1] if result else None

    def _ma(ref_date: date, window_days: int) -> float | None:
        cutoff = ref_date - timedelta(days=window_days)
        vals = [price_map[d] for d in price_dates if cutoff <= d < ref_date]
        return sum(vals) / len(vals) if len(vals) >= window_days // 5 else None

    # Indexar COT por fecha de conocimiento (report_date + lag)
    cot_data = []
    for r in cot_rows:
        if r.ncomm_net is not None:
            try:
                cot_data.append((r.report_date + timedelta(days=COT_PUB_LAG), float(r.ncomm_net)))
            except (TypeError, ValueError):
                pass
    cot_data.sort()

    print()
    print("=" * 72)
    print("  BACKTEST L1 — SGT Trading  (sin look-ahead bias)")
    print("  COT: %d semanas  |  Precio: %d días  |  2007-hoy" % (
        len(cot_data), len(price_dates)))
    print("=" * 72)
    print()

    # Resultados por combinación y horizonte
    results: dict[str, dict[int, list[float]]] = {
        "A1_only_LONG":   {h: [] for h in HORIZONS},
        "A1_only_SHORT":  {h: [] for h in HORIZONS},
        "A1_B2_LONG":     {h: [] for h in HORIZONS},
        "A1_B2_SHORT":    {h: [] for h in HORIZONS},
        "A1_only_BOTH":   {h: [] for h in HORIZONS},
        "A1_B2_BOTH":     {h: [] for h in HORIZONS},
    }
    entries = []

    for i, (know_date, net) in enumerate(cot_data):
        # Percentil rolling 3yr
        window_start = know_date - timedelta(days=ROLLING_WINDOW_DAYS)
        rolling = [v for d, v in cot_data[:i] if d >= window_start]
        if len(rolling) < 26:
            continue  # necesitamos al menos 6 meses

        sorted_r = sorted(rolling)
        n        = len(sorted_r)
        pct      = sum(1 for v in sorted_r if v <= net) / n * 100

        # Señal A1
        if pct >= COT_LONG_PCT:
            a1_dir = "SHORT"   # especuladores demasiado largos → contrarian SHORT
        elif pct <= COT_SHORT_PCT:
            a1_dir = "LONG"    # especuladores demasiado cortos → contrarian LONG
        else:
            continue  # zona neutral, no hay señal

        # Precio en fecha de conocimiento
        px_entry = _price_on_or_after(know_date)
        if not px_entry:
            continue
        entry_date, entry_price = px_entry

        # Señal B2: precio vs MA26w
        ma = _ma(entry_date, MA_DAYS)
        if ma:
            b2_dir = "LONG" if entry_price < ma else "SHORT"
        else:
            b2_dir = None

        sign_a1 = 1.0 if a1_dir == "LONG" else -1.0

        # Registrar entry
        entry_rec = {
            "date": entry_date,
            "entry": entry_price,
            "a1_dir": a1_dir,
            "b2_dir": b2_dir,
            "cot_pct": round(pct, 1),
        }
        entries.append(entry_rec)

        for h in HORIZONS:
            exit_px = _price_n_days_after(entry_date, h * 2)  # aprox bursátil
            if exit_px is None:
                continue
            try:
                ret = (float(exit_px) - float(entry_price)) / float(entry_price)
            except (TypeError, ZeroDivisionError):
                continue

            # A1 solo
            dir_ret_a1 = ret * sign_a1
            results["A1_only_BOTH"][h].append(dir_ret_a1)
            if a1_dir == "LONG":
                results["A1_only_LONG"][h].append(dir_ret_a1)
            else:
                results["A1_only_SHORT"][h].append(dir_ret_a1)

            # A1 + B2 alineados
            if b2_dir and b2_dir == a1_dir:
                results["A1_B2_BOTH"][h].append(dir_ret_a1)
                if a1_dir == "LONG":
                    results["A1_B2_LONG"][h].append(dir_ret_a1)
                else:
                    results["A1_B2_SHORT"][h].append(dir_ret_a1)

    print("  Señales generadas: %d  (A1 en zona extrema)" % len(entries))
    if entries:
        long_n  = sum(1 for e in entries if e["a1_dir"] == "LONG")
        short_n = sum(1 for e in entries if e["a1_dir"] == "SHORT")
        print("  LONG: %d  |  SHORT: %d" % (long_n, short_n))
    print()

    for h in HORIZONS:
        print("  ── Horizonte %dd ──────────────────────────────────────────────" % h)
        print(_stats("A1 solo (LONG+SHORT)",    results["A1_only_BOTH"][h],  h))
        print(_stats("A1 solo — solo LONG",     results["A1_only_LONG"][h],  h))
        print(_stats("A1 solo — solo SHORT",    results["A1_only_SHORT"][h], h))
        print(_stats("A1+B2 alineados (ambos)", results["A1_B2_BOTH"][h],    h))
        print(_stats("A1+B2 — solo LONG",       results["A1_B2_LONG"][h],    h))
        print(_stats("A1+B2 — solo SHORT",      results["A1_B2_SHORT"][h],   h))
        print()

    # Tabla resumen ejecutiva
    print("=" * 72)
    print("  RESUMEN EJECUTIVO (horizonte 10d)")
    print("=" * 72)
    h = 10
    for key, label in [
        ("A1_only_BOTH",  "A1 solo (long+short)"),
        ("A1_B2_BOTH",    "A1+B2 filtrado      "),
        ("A1_only_LONG",  "A1 solo → LONG      "),
        ("A1_only_SHORT", "A1 solo → SHORT     "),
    ]:
        dr = results[key][h]
        if not dr:
            continue
        n   = len(dr)
        avg = sum(dr) / n * 100
        wr  = _win_rate(dr)
        sh  = _sharpe(dr)
        edge = "✓ EDGE" if wr > 55 and avg > 0.3 else ("✗ sin edge" if wr < 50 else "~ marginal")
        print("  %-22s  n=%3d  avg=%+.2f%%  wr=%.0f%%  sharpe=%s  %s" % (
            label, n, avg, wr, ("%.2f" % sh) if not math.isnan(sh) else "n/a", edge))
    print()
    print("  Nota: B2 excluido del scoring SHORT (falla OOS a 18 años — ver LAYER1_SHORT_KEYS)")
    print()


if __name__ == "__main__":
    run()
