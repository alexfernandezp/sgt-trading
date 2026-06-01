"""
Backfill manual del registro CONAB 2025/26 lev=1.

Razón (BUSINESS_LOGIC §7.5.8):
  El scraper de fetch_conab.py no localiza el PDF histórico (URL movido/expirado
  en el sitio CONAB). Necesario para que la lógica "Apertura de Zafra" YoY del
  shadow test pueda comparar 2026/27 lev=1 (sugar=43.95) contra 2025/26 lev=1.

Derivación del sugar_total_mt:
  Tenemos 2025/26 lev=2: sugar=44.50 Mt, revision_sugar_pct=-3.10%
  Entonces lev=2 = lev=1 × (1 - 0.0310)
  → lev=1 = 44.50 / 0.969 = 45.92 Mt

  Este valor es internamente consistente con la cadena de revisiones CONAB.
  pub_date estimado: 2025-04-22 (martes típico para boletim de apertura).
  revision_sugar_pct = NULL (convención CONAB: no se emite en levantamento 1).

Script idempotente: usa ON CONFLICT del UniqueConstraint (season, levantamento).
Re-ejecutar es seguro: actualiza el row existente.
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal
from models import ConabCanaLevantamento
from sqlalchemy.dialects.postgresql import insert


RECORD = {
    "season":         "2025/26",
    "levantamento":   1,
    "pub_date":       date(2025, 4, 22),
    "sugar_total_mt": 45.92,
    "revision_sugar_pct": None,
    # Otros campos (cane, ethanol, area, sp_*) quedan NULL — no necesarios
    # para la inferencia direccional; solo sugar_total_mt + pub_date son
    # consultados por _infer_conab_direction.
}


def main():
    print("=" * 60)
    print("  Backfill CONAB 2025/26 lev=1")
    print("=" * 60)
    print(f"  Record: {RECORD}")

    with SessionLocal() as session:
        stmt = (
            insert(ConabCanaLevantamento)
            .values(**RECORD)
            .on_conflict_do_update(
                constraint="uq_conab_season_lev",
                set_={k: v for k, v in RECORD.items()
                      if k not in ("season", "levantamento")},
            )
        )
        session.execute(stmt)
        session.commit()
        print("  -> upsert OK")

        # Verify
        from sqlalchemy import text
        rows = session.execute(text("""
            SELECT season, levantamento, pub_date, sugar_total_mt, revision_sugar_pct
            FROM conab_cana_levantamento
            WHERE season = '2025/26' AND levantamento = 1
        """)).fetchall()
        print(f"\n  Verify (post-upsert):")
        for r in rows:
            print(f"    {r}")

    print("\nBackfill complete.")


if __name__ == "__main__":
    main()
