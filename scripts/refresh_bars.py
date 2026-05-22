"""
Refresca barras intraday y muestra el estado actual de la sesion.
Uso: py scripts/refresh_bars.py [--no-refresh] [--pnl] [--entry X] [--lots N]
"""
import sys, os, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal
from ingestion.intraday import fetch_intraday
from ingestion.prices   import fetch_prices
from services.scoring   import get_current_price
from sqlalchemy import text

INSTRUMENTS = ["SBN26", "SBV26", "SB_CONT"]


def refresh(session):
    print("Actualizando settlement...", end=" ", flush=True)
    fetch_prices(session, days_back=5)
    row = session.execute(text(
        "SELECT date, close FROM price_history "
        "WHERE instrument = 'SBN26' ORDER BY date DESC LIMIT 1"
    )).fetchone()
    if row:
        print("OK  [Yahoo  settlement %s = %.4f]" % (str(row[0]), float(row[1])))
    else:
        print("sin datos")

    print("Refrescando barras...", end=" ", flush=True)
    result = fetch_intraday(session, INSTRUMENTS, intervals=["5m", "30m", "1h", "4h"])
    totals = {}
    for instr, ivs in result.items():
        for iv, n in ivs.items():
            totals[iv] = totals.get(iv, 0) + n
    parts = ["%s:%d" % (iv, n) for iv, n in totals.items() if n > 0]
    print("OK  [%s]" % ("  ".join(parts) if parts else "sin cambios"))


def show_macro(session):
    """Muestra contexto macro desde price_history (Yahoo): BRL, Brent, White Sugar."""
    rows = session.execute(text(
        "SELECT DISTINCT ON (instrument) instrument, close FROM price_history "
        "WHERE instrument IN ('BRLUSD','BRENT','WHITE_SUGAR','SBN26','SBV26') "
        "ORDER BY instrument, date DESC"
    )).fetchall()
    data = {r[0]: float(r[1]) for r in rows}

    if not data:
        return

    sbn26 = data.get("SBN26", 0)
    sbv26 = data.get("SBV26", 0)
    brl   = data.get("BRLUSD", 0)
    brent = data.get("BRENT", 0)
    ws    = data.get("WHITE_SUGAR", 0)

    print()
    print("  --- CONTEXTO MACRO (settlement ayer) ---")
    if sbn26 and sbv26:
        spread = round(sbv26 - sbn26, 4)
        term   = "contango" if spread > 0 else "backwardation"
        print("  SBN26=%.4f  SBV26=%.4f  Spread N/V=%+.4f  (%s)" % (sbn26, sbv26, spread, term))
    if brl:
        signal = "debil - bearish sugar" if brl > 5.20 else "fuerte - neutral/bullish"
        print("  BRL/USD : %.4f  (%s)" % (brl, signal))
    if brent:
        print("  Brent   : $%.2f/bbl" % brent)
    if ws and sbn26:
        LBS_PER_TONNE = 2204.62
        raw_usd_t = sbn26 * LBS_PER_TONNE / 100
        wsp = round(ws - raw_usd_t, 2)
        signal = "ALTO bullish raw" if wsp > 130 else ("BAJO bearish raw" if wsp < 70 else "normal")
        print("  WSP     : $%.2f/t (White Sugar premium vs raw — %s)" % (wsp, signal))


def show_today(session, instrument="SBN26"):
    price = get_current_price(session, instrument)
    print("\nPrecio actual %-8s: %.4f c/lb  (Yahoo ~15min)" % (instrument, price))

    show_macro(session)

    rows = session.execute(text(
        "SELECT datetime, open, high, low, close, volume "
        "FROM price_bars "
        "WHERE instrument = :instr AND interval = '30m' "
        "  AND DATE(datetime) = CURRENT_DATE "
        "ORDER BY datetime ASC"
    ), {"instr": instrument}).fetchall()

    if not rows:
        print("\n  Sin barras hoy.")
        return price, []

    print()
    print("  %-6s  %6s %6s %6s %6s  %8s  %s" % (
        "HORA", "OPEN", "HIGH", "LOW", "CLOSE", "VOL", ""))
    print("  " + "-" * 56)

    total_vol = 0
    bar_list  = []
    for r in rows:
        dt = r[0]
        o, h, l, c = float(r[1]), float(r[2]), float(r[3]), float(r[4])
        v = float(r[5]) if r[5] else 0
        total_vol += v
        hstr = str(dt)[11:16]
        hour = int(hstr[:2])
        arrow = "^" if c > o else "v" if c < o else "-"
        tag = "  <<< US session" if hour >= 14 else ""
        print("  %-6s  %6.4f %6.4f %6.4f %6.4f  %8.0f  %s%s" % (
            hstr, o, h, l, c, v, arrow, tag))
        bar_list.append({"hour": hour, "vol": v, "close": c})

    print("  " + "-" * 56)
    print("  %-6s  %6s %6s %6s %6s  %8.0f" % ("TOTAL", "", "", "", "", total_vol))

    vols_pre  = [b["vol"] for b in bar_list if b["hour"] < 14  and b["vol"] > 0]
    vols_post = [b["vol"] for b in bar_list if b["hour"] >= 14 and b["vol"] > 0]
    avg_pre   = sum(vols_pre)  / len(vols_pre)  if vols_pre  else 0
    avg_post  = sum(vols_post) / len(vols_post) if vols_post else 0

    print()
    if avg_pre > 0:
        print("  Vol medio manana  (<14:00) : %8.0f contratos/barra" % avg_pre)
    if avg_post > 0:
        mult = avg_post / avg_pre if avg_pre > 0 else 0
        print("  Vol medio tarde  (>=14:00) : %8.0f contratos/barra  (%.1fx manana)" % (avg_post, mult))
    elif vols_pre:
        print("  Sin barras tarde aun -- last bar: %s" % str(rows[-1][0])[11:16])

    return price, bar_list


def pnl_table(entry, lots, price):
    mult = 1120.0
    risk = 0.11
    stop = entry + risk

    print()
    print("  P&L LIVE  SHORT %d lotes  entry=%.4f  stop=%.4f" % (lots, entry, stop))
    print("  USD/tick: $%.0f   USD/cent: $%.0f" % (0.01 * mult * lots, mult * lots))
    print()
    print("  %8s  %6s  %12s  %s" % ("PRECIO", "TICKS", "P&L USD", "NIVEL"))
    print("  " + "-" * 50)

    levels = [
        (stop,   "STOP"),
        (15.00,  "call wall OI 15.00"),
        (price,  "precio ahora"),
        (entry,  "ENTRY"),
        (14.75,  "soporte medio"),
        (14.50,  "put wall OI 14.50"),
    ]
    for lv, label in sorted(levels, reverse=True):
        ticks = round((entry - lv) / 0.01, 1)
        usd   = (entry - lv) * mult * lots
        mark  = " <--" if abs(lv - price) < 0.005 else ""
        print("  %8.4f  %+6.1ft  %+12.0f  %s%s" % (lv, ticks, usd, label, mark))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pnl",        action="store_true")
    parser.add_argument("--entry",      type=float, default=14.94)
    parser.add_argument("--lots",       type=int,   default=20)
    parser.add_argument("--no-refresh", action="store_true")
    args = parser.parse_args()

    session = SessionLocal()

    if not args.no_refresh:
        refresh(session)

    price, bars = show_today(session, "SBN26")

    if args.pnl:
        pnl_table(args.entry, args.lots, price)

    session.close()
