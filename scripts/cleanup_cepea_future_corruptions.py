"""
A.3 — Cleanup CEPEA future corruptions (surgical DELETE).

Triple-guard filter:
  series_name  = 'hydrous_other_usd_liter'
  price_date   IN ('2026-06-03', '2026-08-05', '2026-10-04')
  created_at   ∈ [2026-05-25 00:00:00, 2026-05-26 00:00:00)

SRE protocol:
  Phase 1  — Pre-verification (read-only SELECT + print + interactive confirm)
  Phase 2  — Execute DELETE (transaction + rowcount assert + commit)
  Phase 3  — Post-verification (assertions: MAX(price_date) + COUNT future=0)

Modos:
  Interactivo (canonical): py scripts/cleanup_cepea_future_corruptions.py
  Automation override:     py scripts/cleanup_cepea_future_corruptions.py --yes
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal
from sqlalchemy import bindparam, text


# ── Filter constants — todo explícito, ningún hardcoded mágico ───────────
SERIES               = "hydrous_other_usd_liter"
CORRUPTED_DATES      = ["2026-06-03", "2026-08-05", "2026-10-04"]
CREATED_FROM         = "2026-05-25 00:00:00"
CREATED_TO           = "2026-05-26 00:00:00"

# Expected outcomes — aserciones rígidas
EXPECTED_DELETE_COUNT    = 3
EXPECTED_MAX_DATE_POST   = date(2026, 5, 22)


# ── Phase 1 ─────────────────────────────────────────────────────────────
def phase_1_preverify(session) -> list:
    print("\n[Phase 1] Pre-verification — READ-ONLY scan")
    print("-" * 88)
    stmt = text("""
        SELECT id, series_name, price_date, price_usd, unit,
               pct_weekly, source_page, created_at
        FROM cepea_prices
        WHERE series_name = :series
          AND price_date IN :dates
          AND created_at >= :cstart
          AND created_at <  :cend
        ORDER BY price_date
    """).bindparams(bindparam("dates", expanding=True))
    rows = session.execute(stmt, {
        "series": SERIES,
        "dates":  CORRUPTED_DATES,
        "cstart": CREATED_FROM,
        "cend":   CREATED_TO,
    }).fetchall()

    print(f"\n  Filter applied:")
    print(f"    series_name = '{SERIES}'")
    print(f"    price_date  IN {CORRUPTED_DATES}")
    print(f"    created_at  ∈ [{CREATED_FROM}, {CREATED_TO})")
    print(f"\n  Rows matched: {len(rows)}")

    if not rows:
        return []

    print()
    print(f"  {'id':>6} {'price_date':<12} {'price':>9} {'unit':<10} "
          f"{'pct_w':>7} {'page':<8} created_at")
    print("  " + "-" * 88)
    for r in rows:
        rid, ser, pdate, price, unit, pct_w, page, created = r
        pct_str = f"{float(pct_w):+5.2f}" if pct_w is not None else "  N/A"
        print(f"  {rid:>6} {str(pdate):<12} {float(price):>9.4f} "
              f"{(unit or ''):<10} {pct_str:>7} {(page or ''):<8} {created}")
    return rows


def phase_1b_confirm(n_rows: int, *, skip_prompt: bool):
    """Two-key safety gate."""
    print("\n[Phase 1b] Two-key safety prompt")
    print("-" * 88)
    if n_rows == 0:
        print("  [STOP] No rows match the triple-guard filter. Clean exit.")
        sys.exit(0)
    if n_rows != EXPECTED_DELETE_COUNT:
        print(f"  [ABORT] rowcount={n_rows} != expected {EXPECTED_DELETE_COUNT}"
              f" — scope drift suspected, no DELETE will execute.")
        sys.exit(2)

    prompt_text = (
        f"  ¿Confirmas la eliminación quirúrgica de estas {n_rows} filas? (y/n): "
    )
    if skip_prompt:
        print(prompt_text + "[--yes flag] auto-confirmado")
        return
    answer = input(prompt_text).strip().lower()
    if answer != "y":
        print("  [ABORT] Confirmación NO recibida. Salida limpia, ningún DELETE.")
        sys.exit(1)


# ── Phase 2 ─────────────────────────────────────────────────────────────
def phase_2_delete(session) -> int:
    print("\n[Phase 2] Execute DELETE (transactional, rollback on rowcount drift)")
    print("-" * 88)
    stmt = text("""
        DELETE FROM cepea_prices
        WHERE series_name = :series
          AND price_date IN :dates
          AND created_at >= :cstart
          AND created_at <  :cend
    """).bindparams(bindparam("dates", expanding=True))
    try:
        result = session.execute(stmt, {
            "series": SERIES,
            "dates":  CORRUPTED_DATES,
            "cstart": CREATED_FROM,
            "cend":   CREATED_TO,
        })
        deleted = result.rowcount
        if deleted != EXPECTED_DELETE_COUNT:
            session.rollback()
            raise AssertionError(
                f"rowcount={deleted} != expected {EXPECTED_DELETE_COUNT}. "
                f"ROLLBACK executed. NO data was deleted."
            )
        session.commit()
        print(f"\n  DELETE rowcount = {deleted}  "
              f"(matches expected = {EXPECTED_DELETE_COUNT})")
        print("  Transaction COMMITTED.")
        return deleted
    except Exception:
        session.rollback()
        raise


# ── Phase 3 ─────────────────────────────────────────────────────────────
def phase_3_postverify(session):
    print("\n[Phase 3] Post-verification — rigid assertions")
    print("-" * 88)

    # Assertion 1: MAX(price_date) for affected series
    row = session.execute(text(
        "SELECT MAX(price_date) FROM cepea_prices WHERE series_name = :s"
    ), {"s": SERIES}).fetchone()
    max_date = row[0]
    print(f"\n  MAX(price_date) for {SERIES}: {max_date}")
    assert max_date <= EXPECTED_MAX_DATE_POST, (
        f"FAIL: max(price_date)={max_date} > expected={EXPECTED_MAX_DATE_POST}"
    )
    print(f"  [OK] max <= {EXPECTED_MAX_DATE_POST}")

    # Assertion 2: COUNT(*) for price_date > today (entire table)
    today = date.today()
    row = session.execute(text(
        "SELECT COUNT(*) FROM cepea_prices WHERE price_date > :t"
    ), {"t": today}).fetchone()
    future_count = row[0]
    print(f"\n  COUNT(price_date > {today}) across all series: {future_count}")
    assert future_count == 0, (
        f"FAIL: {future_count} rows still have future price_date"
    )
    print(f"  [OK] 0 rows en futuro absoluto")

    # Assertion 3: Verify the swap-target rows still exist (data integrity)
    print("\n  Swap-target preservation check (los datos correctos NO se borraron):")
    swap_targets = [
        ("2026-03-06", 0.5630),
        ("2026-05-08", 0.4791),
        ("2026-04-10", 0.5692),
    ]
    for target_date, expected_price in swap_targets:
        row = session.execute(text("""
            SELECT price_usd FROM cepea_prices
            WHERE series_name = :s AND price_date = :d
        """), {"s": SERIES, "d": target_date}).fetchone()
        assert row is not None, f"FAIL: swap target {target_date} missing!"
        actual_price = float(row[0])
        assert abs(actual_price - expected_price) < 0.001, (
            f"FAIL: {target_date} price drifted: {actual_price} vs {expected_price}"
        )
        print(f"    [OK] {target_date}  price={actual_price:.4f} (expected {expected_price:.4f})")

    print("\n  All Phase 3 assertions passed.")


# ── Main ────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes", action="store_true",
        help="Skip interactive prompt (CI/automation override).",
    )
    args = parser.parse_args()

    print("=" * 88)
    print("  A.3 — Cleanup CEPEA future corruptions  (surgical DELETE)")
    print("=" * 88)

    with SessionLocal() as session:
        rows = phase_1_preverify(session)
        phase_1b_confirm(len(rows), skip_prompt=args.yes)
        deleted = phase_2_delete(session)
        phase_3_postverify(session)

    print("\n" + "=" * 88)
    print(f"A.3 cleanup complete. Deleted {deleted} rows. DB integrity verified.")
    print("=" * 88)


if __name__ == "__main__":
    main()
