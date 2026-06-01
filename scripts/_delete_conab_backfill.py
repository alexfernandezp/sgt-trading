"""
Limpieza del registro CONAB 2025/26 lev=1 backfilleado manualmente.

Razón (BUSINESS_LOGIC §7.5.8 revised):
  El backfill manual era necesario para la lógica vieja sugar-based (APERTURA
  YoY usando sugar_total_mt). Con el refactor a CANE-level (yoy_cane_pct
  PRIMARY directo de CONAB-published), el backfill ya no es necesario:
  la fila 2026/27 lev=1 trae yoy_cane_pct=+5.30% que la PRIMARY consume
  sin derivación ni histórico previo.

  Eliminar la fila previene ruido (sugar=45.92 derivado, otros campos NULL).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal
from sqlalchemy import text


def main():
    print("=" * 65)
    print("  Delete CONAB backfill row: 2025/26 lev=1")
    print("=" * 65)
    with SessionLocal() as s:
        before = s.execute(text("""
            SELECT season, levantamento, pub_date, sugar_total_mt
            FROM conab_cana_levantamento
            WHERE season = '2025/26' AND levantamento = 1
        """)).fetchall()
        print(f"\n  Pre-delete: {len(before)} row(s)")
        for r in before:
            print(f"    {r}")

        s.execute(text("""
            DELETE FROM conab_cana_levantamento
            WHERE season = '2025/26' AND levantamento = 1
        """))
        s.commit()

        after = s.execute(text("""
            SELECT season, levantamento, pub_date, sugar_total_mt
            FROM conab_cana_levantamento
            WHERE season = '2025/26' AND levantamento = 1
        """)).fetchall()
        print(f"\n  Post-delete: {len(after)} row(s)")

    print("\nCleanup complete.")


if __name__ == "__main__":
    main()
