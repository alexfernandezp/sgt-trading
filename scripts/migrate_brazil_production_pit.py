"""
P3.E.1 — Migracion brazil_production a esquema Point-in-Time (PIT).

OBJETIVO:
  Preservar historico de revisiones MAPA (no machacar filas previas). Pasar
  de UNIQUE(harvest_year, fortnight_seq) a UNIQUE(harvest_year, fortnight_seq,
  report_issue_date) y separar la materia prima ACUMULADA del DELTA neto
  por quincena (que se calculara en P3.E.7 con flujo PIT limpio).

CAMBIOS DE ESQUEMA:
  RENAME 5 columnas numericas → *_cumulative (materia prima MAPA acumulada)
    cane_crushed_t        → cane_crushed_t_cumulative
    sugar_t               → sugar_t_cumulative
    ethanol_anhydrous_m3  → ethanol_anhydrous_m3_cumulative
    ethanol_hydrated_m3   → ethanol_hydrated_m3_cumulative
    ethanol_total_m3      → ethanol_total_m3_cumulative
  ADD 5 columnas nuevas → *_net (NULL inicial, se llenan en P3.E.7)
    cane_crushed_t_net
    sugar_t_net
    ethanol_anhydrous_m3_net
    ethanol_hydrated_m3_net
    ethanol_total_m3_net
  ADD report_issue_date DATE NOT NULL  (backfill = report_date, proxy historico)
  ADD report_revision_seq INTEGER DEFAULT 1  (sufijo _N del filename MAPA)
  DROP CONSTRAINT uq_brazil_harvest_fortnight
  ADD  CONSTRAINT uq_brazil_pit UNIQUE (harvest_year, fortnight_seq, report_issue_date)

NO HACE:
  - Desacumulacion retroactiva de *_net (se difiere a P3.E.7 con datos limpios).
  - Limpieza de filas con monotonia rota (se hace por separado, fuera de migracion).
  - Recalculo de sugar_mix_pct (downstream lo recomputara desde *_net cuando exista).

MODOS:
  Dry-run (default, READ-ONLY):
    py scripts/migrate_brazil_production_pit.py

  Apply (transactional, two-key prompt):
    py scripts/migrate_brazil_production_pit.py --apply
    # despues: typed confirmation literal "MIGRATE BRAZIL PIT"

  Automation override (NO interactivo):
    py scripts/migrate_brazil_production_pit.py --apply --yes
"""
import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal
from sqlalchemy import text


CONFIRMATION_PHRASE = "MIGRATE BRAZIL PIT"

# DDL plan — listo como tuplas (sql, descripcion) para imprimir el plan
# en Fase 1 antes de aplicar nada.
RENAME_PAIRS = [
    ("cane_crushed_t",       "cane_crushed_t_cumulative"),
    ("sugar_t",              "sugar_t_cumulative"),
    ("ethanol_anhydrous_m3", "ethanol_anhydrous_m3_cumulative"),
    ("ethanol_hydrated_m3",  "ethanol_hydrated_m3_cumulative"),
    ("ethanol_total_m3",     "ethanol_total_m3_cumulative"),
]

NEW_NET_COLUMNS = [
    "cane_crushed_t_net",
    "sugar_t_net",
    "ethanol_anhydrous_m3_net",
    "ethanol_hydrated_m3_net",
    "ethanol_total_m3_net",
]

OLD_CONSTRAINT_NAME = "uq_brazil_harvest_fortnight"
NEW_CONSTRAINT_NAME = "uq_brazil_pit"


# ─────────────────────────────────────────────────────────────────────────
# Phase 1 — Inspect (READ-ONLY)
# ─────────────────────────────────────────────────────────────────────────
def phase_1_inspect(session) -> dict:
    print("\n[Phase 1] Inspect current state — READ-ONLY")
    print("-" * 88)

    # Row count + harvests
    row = session.execute(text("""
        SELECT COUNT(*) AS n,
               COUNT(DISTINCT harvest_year) AS harvests,
               MIN(report_date) AS first_d,
               MAX(report_date) AS last_d
        FROM brazil_production
    """)).fetchone()
    n_rows, n_harvests, first_d, last_d = row
    print(f"\n  rows={n_rows}  harvests={n_harvests}  range=[{first_d} .. {last_d}]")

    # Schema check — current columns
    cols = session.execute(text("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'brazil_production'
        ORDER BY ordinal_position
    """)).fetchall()
    print(f"\n  Current schema ({len(cols)} columns):")
    for cname, dtype, nullable in cols:
        print(f"    {cname:<32} {dtype:<25} {'NULL' if nullable=='YES' else 'NOT NULL'}")

    # Pre-flight: confirm old columns exist (no doble-migracion)
    col_names = {c[0] for c in cols}
    old_cols_present = [old for old, _ in RENAME_PAIRS if old in col_names]
    new_cols_present = [new for _, new in RENAME_PAIRS if new in col_names]
    if not old_cols_present and new_cols_present:
        print("\n  [STOP] Las columnas *_cumulative ya existen; la migracion parece")
        print("         haberse aplicado previamente. No hay nada que hacer.")
        return {"already_migrated": True, "n_rows": n_rows}
    if "report_issue_date" in col_names:
        print("\n  [STOP] report_issue_date ya existe — migracion previa detectada.")
        return {"already_migrated": True, "n_rows": n_rows}

    # Constraints actuales
    constraints = session.execute(text("""
        SELECT con.conname, con.contype
        FROM pg_constraint con
        JOIN pg_class cl ON cl.oid = con.conrelid
        WHERE cl.relname = 'brazil_production' AND con.contype IN ('u', 'p')
    """)).fetchall()
    print(f"\n  Existing unique/primary constraints:")
    for name, ctype in constraints:
        print(f"    {name:<40} type={ctype}")

    # Anomalia detection: monotonia del acumulado por safra
    print(f"\n  Monotonicity check (per harvest_year, cane_crushed_t deltas):")
    monotonicity_breaks = []
    for hy_row in session.execute(text(
        "SELECT DISTINCT harvest_year FROM brazil_production ORDER BY harvest_year"
    )).fetchall():
        hy = hy_row[0]
        rows = session.execute(text("""
            SELECT fortnight_seq, cane_crushed_t
            FROM brazil_production
            WHERE harvest_year = :hy
            ORDER BY fortnight_seq
        """), {"hy": hy}).fetchall()
        seqs = [r[0] for r in rows]
        canes = [int(r[1]) if r[1] else 0 for r in rows]
        # Gaps en fortnight_seq
        gaps = []
        for i in range(1, len(seqs)):
            if seqs[i] != seqs[i-1] + 1:
                gaps.append((seqs[i-1], seqs[i]))
        # Monotonicity breaks (Δ < 0)
        breaks = []
        for i in range(1, len(canes)):
            if canes[i] < canes[i-1]:
                breaks.append((seqs[i-1], seqs[i], canes[i-1], canes[i]))
                monotonicity_breaks.append((hy, seqs[i-1], seqs[i]))
        print(f"    {hy}: {len(rows)} rows  gaps={gaps}  breaks={len(breaks)}")
        for prev_seq, seq, prev_cane, cane in breaks:
            delta = cane - prev_cane
            print(f"      seq{prev_seq}→seq{seq}: cane {prev_cane:>12,} → {cane:>12,}  "
                  f"Δ={delta:+,}")

    if monotonicity_breaks:
        print(f"\n  [WARNING] {len(monotonicity_breaks)} monotonicity breaks detected.")
        print(f"            Migration NO desacumula retroactivamente — *_net se deja NULL.")
        print(f"            P3.E.7 recomputa *_net al re-ingerir con flujo PIT limpio.")
    else:
        print(f"\n  [OK] Acumulado monotonico en todas las safras.")

    # Print the DDL plan
    print(f"\n  DDL plan to execute (in single transaction):")
    print(f"    -- 1. Rename 5 cumulative columns")
    for old, new in RENAME_PAIRS:
        print(f"    ALTER TABLE brazil_production RENAME COLUMN {old} TO {new};")
    print(f"    -- 2. Add 5 net columns (nullable, to be filled by P3.E.7)")
    for new in NEW_NET_COLUMNS:
        print(f"    ALTER TABLE brazil_production ADD COLUMN {new} NUMERIC(18, 0);")
    print(f"    -- 3. Add PIT timestamps")
    print(f"    ALTER TABLE brazil_production ADD COLUMN report_issue_date DATE;")
    print(f"    UPDATE brazil_production SET report_issue_date = report_date;")
    print(f"    ALTER TABLE brazil_production ALTER COLUMN report_issue_date SET NOT NULL;")
    print(f"    ALTER TABLE brazil_production ADD COLUMN report_revision_seq INTEGER DEFAULT 1;")
    print(f"    -- 4. Swap UNIQUE constraint")
    print(f"    ALTER TABLE brazil_production DROP CONSTRAINT {OLD_CONSTRAINT_NAME};")
    print(f"    ALTER TABLE brazil_production ADD CONSTRAINT {NEW_CONSTRAINT_NAME}")
    print(f"        UNIQUE (harvest_year, fortnight_seq, report_issue_date);")

    return {
        "already_migrated": False,
        "n_rows": n_rows,
        "monotonicity_breaks": monotonicity_breaks,
    }


# ─────────────────────────────────────────────────────────────────────────
# Phase 1b — Two-key safety prompt
# ─────────────────────────────────────────────────────────────────────────
def phase_1b_confirm(*, skip_prompt: bool):
    print("\n[Phase 1b] Two-key safety prompt")
    print("-" * 88)
    print(f"\n  Para ejecutar la migracion escribe LITERALMENTE: {CONFIRMATION_PHRASE}")
    if skip_prompt:
        print(f"  [--yes flag] auto-confirmado.")
        return
    answer = input("  > ").strip()
    if answer != CONFIRMATION_PHRASE:
        print(f"\n  [ABORT] Frase recibida {answer!r} != esperada {CONFIRMATION_PHRASE!r}.")
        print(f"          ROLLBACK preventivo. Ningun cambio realizado.")
        sys.exit(1)
    print(f"  Confirmacion correcta. Procediendo.")


# ─────────────────────────────────────────────────────────────────────────
# Phase 2 — Apply (transactional)
# ─────────────────────────────────────────────────────────────────────────
def phase_2_apply(session):
    print("\n[Phase 2] Apply DDL — single transaction, rollback on error")
    print("-" * 88)
    try:
        # 1. RENAME 5 cumulative columns
        print("\n  1/4 Renombrando 5 columnas → *_cumulative ...")
        for old, new in RENAME_PAIRS:
            session.execute(text(
                f"ALTER TABLE brazil_production RENAME COLUMN {old} TO {new}"
            ))
            print(f"      {old} → {new}")

        # 2. ADD 5 net columns (NULL initial)
        print("\n  2/4 Anadiendo 5 columnas *_net (NULL, sin backfill)...")
        for new in NEW_NET_COLUMNS:
            session.execute(text(
                f"ALTER TABLE brazil_production ADD COLUMN {new} NUMERIC(18, 0)"
            ))
            print(f"      + {new}")

        # 3. ADD PIT timestamps
        print("\n  3/4 Anadiendo report_issue_date + report_revision_seq...")
        session.execute(text(
            "ALTER TABLE brazil_production ADD COLUMN report_issue_date DATE"
        ))
        # Backfill con report_date como proxy historico
        result = session.execute(text(
            "UPDATE brazil_production SET report_issue_date = report_date"
        ))
        print(f"      report_issue_date backfilled ({result.rowcount} rows from report_date)")
        session.execute(text(
            "ALTER TABLE brazil_production ALTER COLUMN report_issue_date SET NOT NULL"
        ))
        print(f"      report_issue_date SET NOT NULL")
        session.execute(text(
            "ALTER TABLE brazil_production ADD COLUMN report_revision_seq INTEGER DEFAULT 1"
        ))
        print(f"      + report_revision_seq INTEGER DEFAULT 1")

        # 4. Swap UNIQUE constraint
        print(f"\n  4/4 Swap UNIQUE constraint...")
        session.execute(text(
            f"ALTER TABLE brazil_production DROP CONSTRAINT {OLD_CONSTRAINT_NAME}"
        ))
        print(f"      DROP {OLD_CONSTRAINT_NAME}")
        session.execute(text(
            f"ALTER TABLE brazil_production ADD CONSTRAINT {NEW_CONSTRAINT_NAME} "
            f"UNIQUE (harvest_year, fortnight_seq, report_issue_date)"
        ))
        print(f"      ADD  {NEW_CONSTRAINT_NAME} (harvest_year, fortnight_seq, report_issue_date)")

        session.commit()
        print(f"\n  Transaction COMMITTED.")
    except Exception as e:
        session.rollback()
        print(f"\n  [ROLLBACK] Error during DDL: {e}")
        raise


# ─────────────────────────────────────────────────────────────────────────
# Phase 3 — Post-verify (assertions)
# ─────────────────────────────────────────────────────────────────────────
def phase_3_postverify(session, expected_rowcount: int):
    print("\n[Phase 3] Post-verify — rigid assertions")
    print("-" * 88)

    # Assertion 1: rowcount preserved
    row = session.execute(text("SELECT COUNT(*) FROM brazil_production")).fetchone()
    assert row[0] == expected_rowcount, (
        f"FAIL: rowcount={row[0]} != expected {expected_rowcount}"
    )
    print(f"\n  [OK] rowcount preserved: {row[0]}")

    # Assertion 2: new columns exist
    cols = session.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'brazil_production'
    """)).fetchall()
    col_set = {c[0] for c in cols}

    required_new = set(NEW_NET_COLUMNS) | {
        new for _, new in RENAME_PAIRS
    } | {"report_issue_date", "report_revision_seq"}
    missing = required_new - col_set
    assert not missing, f"FAIL: columnas faltantes {missing}"
    print(f"  [OK] {len(required_new)} columnas nuevas/renombradas presentes")

    # Assertion 3: old columns gone
    old_cols = {old for old, _ in RENAME_PAIRS}
    leftover = old_cols & col_set
    assert not leftover, f"FAIL: columnas viejas todavia presentes {leftover}"
    print(f"  [OK] Columnas viejas removidas: {sorted(old_cols)}")

    # Assertion 4: new constraint exists, old gone
    constraints = session.execute(text("""
        SELECT con.conname FROM pg_constraint con
        JOIN pg_class cl ON cl.oid = con.conrelid
        WHERE cl.relname = 'brazil_production' AND con.contype = 'u'
    """)).fetchall()
    con_names = {c[0] for c in constraints}
    assert NEW_CONSTRAINT_NAME in con_names, f"FAIL: nueva UNIQUE {NEW_CONSTRAINT_NAME} ausente"
    assert OLD_CONSTRAINT_NAME not in con_names, (
        f"FAIL: vieja UNIQUE {OLD_CONSTRAINT_NAME} todavia presente"
    )
    print(f"  [OK] UNIQUE constraints swapped correctly")

    # Assertion 5: report_issue_date NOT NULL en todas las filas + igual a report_date
    row = session.execute(text("""
        SELECT COUNT(*) FROM brazil_production
        WHERE report_issue_date IS NULL OR report_issue_date != report_date
    """)).fetchone()
    assert row[0] == 0, f"FAIL: {row[0]} filas con report_issue_date mal backfilled"
    print(f"  [OK] report_issue_date NOT NULL + igual a report_date en {expected_rowcount} filas")

    # Assertion 6: cumulative columns retain values (rename, no data loss)
    row = session.execute(text("""
        SELECT COUNT(*) FROM brazil_production WHERE cane_crushed_t_cumulative IS NULL
    """)).fetchone()
    # Algunas filas pueden tener NULL legitimo si el _parse_xls fallo en su momento — solo logueamos
    print(f"  [INFO] {row[0]} filas con cane_crushed_t_cumulative NULL (pre-existente)")

    # Assertion 7: *_net columns todas NULL (no backfill — esperado)
    row = session.execute(text("""
        SELECT COUNT(*) FROM brazil_production
        WHERE cane_crushed_t_net IS NOT NULL
    """)).fetchone()
    assert row[0] == 0, f"FAIL: {row[0]} filas con _net no NULL — backfill no esperado"
    print(f"  [OK] *_net columns todas NULL (esperado, backfill diferido a P3.E.7)")

    # Assertion 8: report_revision_seq = 1 default
    row = session.execute(text("""
        SELECT COUNT(*) FROM brazil_production WHERE report_revision_seq != 1
    """)).fetchone()
    assert row[0] == 0, f"FAIL: {row[0]} filas con revision_seq != 1"
    print(f"  [OK] report_revision_seq = 1 en todas las filas historicas")

    print(f"\n  All Phase 3 assertions passed.")


# ─────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Execute DDL changes (default: dry-run, read-only)",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Skip interactive two-key prompt (CI/automation override)",
    )
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print("=" * 88)
    print(f"  P3.E.1 — Migrate brazil_production to PIT schema  [{mode}]")
    print("=" * 88)

    with SessionLocal() as session:
        result = phase_1_inspect(session)

        if result.get("already_migrated"):
            print("\n" + "=" * 88)
            print("  Nada que hacer. Estado actual ya parece migrado.")
            print("=" * 88)
            sys.exit(0)

        if not args.apply:
            print("\n" + "=" * 88)
            print("  DRY-RUN mode. No DDL ejecutado. Para aplicar:")
            print("    py scripts/migrate_brazil_production_pit.py --apply")
            print("=" * 88)
            sys.exit(0)

        phase_1b_confirm(skip_prompt=args.yes)
        phase_2_apply(session)
        phase_3_postverify(session, expected_rowcount=result["n_rows"])

    print("\n" + "=" * 88)
    print(f"  P3.E.1 migration complete. Schema PIT-ready. {result['n_rows']} rows preserved.")
    print("=" * 88)


if __name__ == "__main__":
    main()
