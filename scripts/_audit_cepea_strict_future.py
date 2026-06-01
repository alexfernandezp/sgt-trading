"""
A.2 ampliado — escaneo estricto cepea_prices.

Threshold = 0 (price_date > today). Para cada row en futuro absoluto:
  1. Stored row data
  2. DD/MM swap candidate (interchange day↔month)
  3. Veredicto de plausibilidad (¿el swap cae en past pero cercano?)
  4. Contexto de vecindario (rows ±14 días alrededor del swap target)

READ-ONLY. Ningún DELETE. Output para decisión quirúrgica humana.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal
from sqlalchemy import text


def try_swap(d: date):
    """Day↔month swap. Returns None si la combinación resultante es inválida."""
    try:
        return date(d.year, d.day, d.month)
    except ValueError:
        return None


def main():
    today = date.today()
    print("=" * 100)
    print(f"  A.2 ampliado — escaneo estricto cepea_prices  today={today}")
    print("=" * 100)

    with SessionLocal() as s:
        rows = s.execute(text("""
            SELECT series_name, price_date, price_usd, unit,
                   pct_daily, pct_weekly, pct_monthly,
                   source_page, created_at
            FROM cepea_prices
            WHERE price_date > :today
            ORDER BY series_name, price_date
        """), {"today": today}).fetchall()

        if not rows:
            print("\n  [OK] No hay filas con price_date > today")
            return

        print(f"\n  Filas con price_date > today (futuro absoluto): {len(rows)}")
        print()
        print(f"  {'#':>2} {'Series':<32} {'Stored':<12} {'Price':>9}  "
              f"{'DD/MM swap':<13} {'Plausibilidad':<28} {'Created'}")
        print("  " + "-" * 130)

        for i, r in enumerate(rows, 1):
            series, pdate, price, unit, pd_d, pd_w, pd_m, page, created = r
            swap = try_swap(pdate)
            if swap is None:
                swap_str = "INVALID"
                plaus = "swap result invalid date"
            else:
                swap_str = str(swap)
                if swap > today:
                    plaus = "still future (suspicious)"
                elif swap == pdate:
                    plaus = "no swap effect (dd==mm)"
                else:
                    days_back = (today - swap).days
                    plaus = f"PAST t-{days_back}d (likely correct date)"

            print(
                f"  {i:>2} {series:<32} {str(pdate):<12} "
                f"{float(price):>9.4f}  {swap_str:<13} {plaus:<28} {created}"
            )

        # ── Vecindario para cada row contaminada ─────────────────────────
        print()
        print("  Contexto del vecindario (rows ±14d alrededor del swap target):")
        print()

        for i, r in enumerate(rows, 1):
            series, pdate, price = r[0], r[1], float(r[2])
            swap = try_swap(pdate)
            print(f"  [{i}] {series}  stored={pdate}  price={price:.4f}")
            if swap is None or swap > today:
                print(f"      swap candidate {swap}: not actionable (invalid or future)")
                continue
            window_start = swap - timedelta(days=14)
            window_end   = swap + timedelta(days=14)
            ctx = s.execute(text("""
                SELECT price_date, price_usd
                FROM cepea_prices
                WHERE series_name = :s
                  AND price_date BETWEEN :a AND :b
                ORDER BY price_date
            """), {"s": series, "a": window_start, "b": window_end}).fetchall()
            print(f"      vecindario {window_start} → {window_end}:")
            target_in_window = False
            for c in ctx:
                marker = ""
                if c[0] == swap:
                    marker = "  <-- SWAP TARGET (already exists!)"
                    target_in_window = True
                print(f"        {c[0]}  price={float(c[1]):.4f}{marker}")
            if not target_in_window:
                print(f"        [!] {swap} NOT in DB → contaminated row likely "
                      f"reflects este día perdido (substituir parece seguro)")
            print()

    print("=" * 100)
    print("Escaneo terminado. Ninguna mutación ejecutada.")
    print("=" * 100)


if __name__ == "__main__":
    main()
