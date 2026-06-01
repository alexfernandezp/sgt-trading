"""
Auditoría forense A.2 — cepea_prices.

Dos investigaciones:
  Q1: Rows con price_date > today+7d (contaminadas por el bug de date parsing)
  Q2: Misterio crystal_sugar2: ¿duplicación de crystal_sugar o serie nueva legítima?

READ-ONLY. Ningún DELETE. La purga viene en A.3 tras analizar la evidencia.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal
from sqlalchemy import text


def main():
    today = date.today()
    threshold = today + timedelta(days=7)
    print("=" * 90)
    print(f"  Auditoría Forense cepea_prices — today={today} threshold={threshold}")
    print("=" * 90)

    with SessionLocal() as s:

        # ──────────────────────────────────────────────────────────────────
        # Q1: Forensic future dates
        # ──────────────────────────────────────────────────────────────────
        print("\n" + "─" * 90)
        print("Q1. Rows con price_date > threshold (sospecha de contaminación)")
        print("─" * 90)

        rows = s.execute(text("""
            SELECT series_name, price_date, price_usd, unit,
                   pct_daily, pct_weekly, pct_monthly, source_page, created_at
            FROM cepea_prices
            WHERE price_date > :threshold
            ORDER BY series_name, price_date
        """), {"threshold": threshold}).fetchall()

        if not rows:
            print("\n  [OK] No hay rows con price_date > threshold")
        else:
            print(f"\n  Filas contaminadas detectadas: {len(rows)}")
            print()
            print(f"  {'Series':<32} {'Date':<12} {'Price':>10} {'Unit':<10} "
                  f"{'pct_d':>7} {'pct_w':>7} {'pct_m':>7} {'Page':<8} "
                  f"{'Created'}")
            print("  " + "-" * 110)
            for r in rows:
                series, pdate, price, unit, pd_d, pd_w, pd_m, page, created = r
                fmt_pct = lambda v: f"{float(v):+5.2f}" if v is not None else "  N/A"
                print(
                    f"  {series:<32} {str(pdate):<12} "
                    f"{float(price):>10.4f} {(unit or ''):<10} "
                    f"{fmt_pct(pd_d):>7} {fmt_pct(pd_w):>7} {fmt_pct(pd_m):>7} "
                    f"{(page or ''):<8} {created}"
                )

            # Para cada series afectada, mostrar la fila previa legítima
            print()
            print("  Contexto — última fila legítima (no-futura) de cada serie afectada:")
            affected_series = set(r[0] for r in rows)
            for ser in sorted(affected_series):
                ctx = s.execute(text("""
                    SELECT price_date, price_usd, unit
                    FROM cepea_prices
                    WHERE series_name = :s AND price_date <= :t
                    ORDER BY price_date DESC LIMIT 3
                """), {"s": ser, "t": threshold}).fetchall()
                print(f"\n    {ser}:")
                for c in ctx:
                    print(f"      {c[0]}  price={float(c[1]):.4f}  unit={c[2]}")

        # ──────────────────────────────────────────────────────────────────
        # Q2: crystal_sugar2 mystery
        # ──────────────────────────────────────────────────────────────────
        print("\n" + "─" * 90)
        print("Q2. crystal_sugar2_usd_bag50kg — análisis comparativo")
        print("─" * 90)

        sugar2_rows = s.execute(text("""
            SELECT price_date, price_usd, unit, pct_daily, pct_weekly, pct_monthly,
                   source_page, created_at
            FROM cepea_prices
            WHERE series_name = 'crystal_sugar2_usd_bag50kg'
            ORDER BY price_date
        """)).fetchall()

        print(f"\n  crystal_sugar2_usd_bag50kg total: {len(sugar2_rows)} filas")
        print(f"  Rango: {sugar2_rows[0][0] if sugar2_rows else 'N/A'}"
              f" -> {sugar2_rows[-1][0] if sugar2_rows else 'N/A'}")

        # Comparación side-by-side con crystal_sugar en las MISMAS fechas
        print()
        print(f"  {'Date':<12} {'crystal_sugar':>16} {'crystal_sugar2':>16} "
              f"{'Diff':>10} {'%diff':>8} {'Verdict'}")
        print("  " + "-" * 88)

        n_identical = 0
        n_different = 0
        n_missing_main = 0

        for s2_row in sugar2_rows:
            pdate, s2_price = s2_row[0], float(s2_row[1])
            s1_row = s.execute(text("""
                SELECT price_usd, unit
                FROM cepea_prices
                WHERE series_name = 'crystal_sugar_usd_bag50kg'
                  AND price_date = :pd
            """), {"pd": pdate}).fetchone()

            if s1_row is None:
                print(f"  {str(pdate):<12} {'(no row)':>16} {s2_price:>16.4f}"
                      f" {'-':>10} {'-':>8}  ❓ no main")
                n_missing_main += 1
                continue

            s1_price = float(s1_row[0])
            diff = s2_price - s1_price
            pct_diff = (diff / s1_price * 100) if s1_price else 0.0
            if abs(diff) < 0.0001:
                verdict = "= IDENTICAL"
                n_identical += 1
            else:
                verdict = "≠ DIFFERENT"
                n_different += 1
            print(f"  {str(pdate):<12} {s1_price:>16.4f} {s2_price:>16.4f}"
                  f" {diff:>+10.4f} {pct_diff:>+7.2f}%  {verdict}")

        print()
        print(f"  Verdict counts: identical={n_identical}, different={n_different},"
              f" missing_main={n_missing_main}")

        if n_identical == len(sugar2_rows) and n_identical > 0:
            print()
            print("  >>> CONCLUSIÓN: 100% identical → "
                  "DUPLICATE artifact, NOT a new CEPEA series.")
            print("      El parser está leyendo la misma tabla dos veces "
                  "y clasificando la 2ª como 'crystal_sugar2'.")
        elif n_different == len(sugar2_rows) and n_identical == 0:
            print()
            print("  >>> CONCLUSIÓN: 100% different → "
                  "NEW LEGITIMATE CEPEA series (VHP o refined sugar).")
            print("      Tratar como serie útil, mantener.")
        elif n_missing_main > 0:
            print()
            print(f"  >>> CONCLUSIÓN: missing_main = {n_missing_main} → "
                  "crystal_sugar tiene gaps esos días.")
            print("      Indica problema separado en el scraping de "
                  "crystal_sugar principal.")
        else:
            print()
            print("  >>> CONCLUSIÓN: MIXED — analisis caso a caso requerido.")

        # Extra: ¿el unit es el mismo?
        units = set(r[2] for r in sugar2_rows)
        print(f"\n  Unit field de crystal_sugar2: {units}")
        units_main = s.execute(text("""
            SELECT DISTINCT unit FROM cepea_prices
            WHERE series_name = 'crystal_sugar_usd_bag50kg'
            AND price_date >= '2026-05-01'
        """)).fetchall()
        print(f"  Unit field de crystal_sugar (mayo 2026): "
              f"{[r[0] for r in units_main]}")

    print("\n" + "=" * 90)
    print("Auditoría completa. Ninguna mutación ejecutada.")
    print("=" * 90)


if __name__ == "__main__":
    main()
