"""
P3.E.7 — Cleanup de anomalias + computo retroactivo de *_net en brazil_production.

OBJETIVO:
  1. Detectar y reparar 3 clases de anomalias en los datos legacy MAPA:
       A. Monotonicity breaks (cumulative desciende entre seq consecutivos):
          indica fila mal etiquetada, revision fuera de orden, o dato incorrecto.
       B. Seq gaps (quincenas faltantes, ej. seq6→seq8 sin seq7):
          el net del seq posterior al gap se deja NULL (delta ambiguo).
       C. Mislabeled safra (delta < -500M al cruzar seq boundary):
          casi seguro un seq=1 de la safra siguiente mal asignado.
          Fix: UPDATE harvest_year al siguiente y fortnight_seq = 1.
  2. Computar *_net retroactivamente para la revision mas reciente de cada
     (harvest_year, fortnight_seq):
       net[seq=1]           = cumulative[seq=1]          (inicio de safra)
       net[seq>1, no gap]   = cumulative[seq] - cumulative[seq-1]
       net[seq>1, gap]      = NULL (delta abarca quincenas desconocidas)
       net[monoton_break]   = NULL (datos inconsistentes — ver WARN log)
  3. Recomputar sugar_mix_pct desde los valores net:
       sugar_mix_pct = net_sugar / (net_sugar + net_ethanol_total * 1.2) * 100
     Solo para filas donde net_sugar > 0 y net_ethanol_total > 0.

MODOS:
  Dry-run (default, READ-ONLY):
    py scripts/cleanup_brazil_mapa_legacy.py

  Apply (transactional, two-key prompt):
    py scripts/cleanup_brazil_mapa_legacy.py --apply

  Automation override (NO interactivo):
    py scripts/cleanup_brazil_mapa_legacy.py --apply --yes

SCOPE:
  Solo actualiza la fila mas reciente (latest issue_date) por (harvest_year,
  fortnight_seq). Las revisiones historicas se dejan con net=NULL — el
  downstream usa DISTINCT ON y solo consume la fila mas reciente.

Ver BUSINESS_LOGIC §3.2 para rangos validos de *_net y sugar_mix_pct.
"""
import argparse
import sys
from pathlib import Path
from decimal import Decimal

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from database import get_session

# Umbral para detectar mislabeled safra: delta < -MISLABEL_THRESHOLD_T indica
# que el seq K+1 es probablemente seq=1 de la safra siguiente (reset acumulado).
MISLABEL_THRESHOLD_T = 500_000_000   # toneladas (500 Mt — imposible de cruzar en una quinzena)

# Etanol equivalente calorimetrico para mezcla industrial (§3.2.C BUSINESS_LOGIC)
ETHANOL_EQUIV_FACTOR = 1.2


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _int(val) -> int | None:
    if val is None:
        return None
    return int(Decimal(val))


def _next_harvest_year(hy: str) -> str:
    """'2025-2026' → '2026-2027'"""
    parts = hy.split("-")
    if len(parts) == 2:
        try:
            a, b = int(parts[0]), int(parts[1])
            return f"{b}-{b + 1}"
        except ValueError:
            pass
    return hy  # fallback — no change


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — Inspect (always runs)
# ─────────────────────────────────────────────────────────────────────────────

def phase_1_inspect(session) -> dict:
    """
    Lee la DB y reporta el estado actual: anomalias, nulos, y snapshot de datos.

    Devuelve un dict con toda la informacion necesaria para phase_2_apply.
    """
    print("\n[Phase 1] Inspeccion de brazil_production")
    print("=" * 72)

    # Contar filas totales y con *_net nulos
    total_rows = session.execute(text(
        "SELECT COUNT(*) FROM brazil_production"
    )).scalar()
    null_net_rows = session.execute(text(
        "SELECT COUNT(*) FROM brazil_production WHERE cane_crushed_t_net IS NULL"
    )).scalar()
    print(f"\n  Total rows:         {total_rows:,}")
    print(f"  Rows with net=NULL: {null_net_rows:,}")

    # Cargar latest revision por (harvest_year, fortnight_seq)
    # Col indices: 0=id, 1=hy, 2=seq, 3=report_date, 4=issue_date, 5=rev_seq,
    #              6=cum_cane, 7=cum_sugar, 8=cum_eth_anh, 9=cum_eth_hyd, 10=cum_eth_total,
    #              11=net_cane, 12=net_sugar, 13=net_eth_total, 14=sugar_mix
    latest_rows = session.execute(text("""
        SELECT DISTINCT ON (harvest_year, fortnight_seq)
               id, harvest_year, fortnight_seq, report_date,
               report_issue_date, report_revision_seq,
               cane_crushed_t_cumulative, sugar_t_cumulative,
               ethanol_anhydrous_m3_cumulative, ethanol_hydrated_m3_cumulative,
               ethanol_total_m3_cumulative,
               cane_crushed_t_net, sugar_t_net, ethanol_total_m3_net,
               sugar_mix_pct
        FROM   brazil_production
        ORDER  BY harvest_year, fortnight_seq, report_issue_date DESC
    """)).fetchall()

    print(f"\n  Latest revisions (1 per quinzena): {len(latest_rows):,}")

    # Agrupar por harvest_year
    hy_data: dict[str, list] = {}
    for r in latest_rows:
        hy_data.setdefault(r[1], []).append(r)

    anomalies = {
        "mislabeled":       [],   # (hy, prev_seq, bad_seq, row_id, delta)
        "monoton_breaks":   [],   # (hy, prev_seq, bad_seq, row_id, delta)
        "seq_gaps":         [],   # (hy, prev_seq, next_seq)
    }

    print(f"\n  Anomaly scan by harvest_year:")
    print(f"  {'harvest_year':<14} {'rows':>5} {'gaps':>6} {'breaks':>7} {'mislabeled':>11}")
    print(f"  {'-'*14} {'-'*5} {'-'*6} {'-'*7} {'-'*11}")

    for hy in sorted(hy_data.keys()):
        rows = sorted(hy_data[hy], key=lambda r: r[2])  # sort by fortnight_seq
        n_gaps = n_breaks = n_mislabeled = 0

        for i in range(1, len(rows)):
            prev_r, cur_r = rows[i-1], rows[i]
            prev_seq = prev_r[2]
            cur_seq  = cur_r[2]
            prev_cum = _int(prev_r[6])   # cane_crushed_t_cumulative
            cur_cum  = _int(cur_r[6])

            # Seq gap?
            if cur_seq != prev_seq + 1:
                n_gaps += 1
                anomalies["seq_gaps"].append((hy, prev_seq, cur_seq))
                continue   # no delta check across a gap

            # Delta check (cane only — most reliable)
            if prev_cum is not None and cur_cum is not None:
                delta = cur_cum - prev_cum
                if delta < -MISLABEL_THRESHOLD_T:
                    n_mislabeled += 1
                    anomalies["mislabeled"].append(
                        (hy, prev_seq, cur_seq, cur_r[0], delta)
                    )
                elif delta < 0:
                    n_breaks += 1
                    anomalies["monoton_breaks"].append(
                        (hy, prev_seq, cur_seq, cur_r[0], delta)
                    )

        print(f"  {hy:<14} {len(rows):>5} {n_gaps:>6} {n_breaks:>7} {n_mislabeled:>11}")

    # Detalle de anomalias
    if anomalies["mislabeled"]:
        print(f"\n  [MISLABELED] {len(anomalies['mislabeled'])} row(s) — probable safra-label error:")
        for hy, ps, cs, rid, delta in anomalies["mislabeled"]:
            next_hy = _next_harvest_year(hy)
            print(f"    id={rid} {hy} seq{ps}→seq{cs}: cane Δ={delta:+,}t")
            print(f"      -> will UPDATE to harvest_year='{next_hy}' fortnight_seq=1")

    if anomalies["monoton_breaks"]:
        print(f"\n  [MONOTON_BREAK] {len(anomalies['monoton_breaks'])} row(s) — net set to NULL:")
        for hy, ps, cs, rid, delta in anomalies["monoton_breaks"]:
            print(f"    id={rid} {hy} seq{ps}→seq{cs}: cane Δ={delta:+,}t")

    if anomalies["seq_gaps"]:
        print(f"\n  [SEQ_GAP] {len(anomalies['seq_gaps'])} gap(s) — net NULL for seq after gap:")
        for hy, ps, ns in anomalies["seq_gaps"]:
            print(f"    {hy}: seq{ps}→seq{ns} (missing seq{ps + 1}..{ns - 1})")

    # Precomputar los UPDATEs que se harian
    net_updates = _compute_net_updates(hy_data, anomalies)
    print(f"\n  Net updates to apply: {len(net_updates):,} rows")
    null_count = sum(1 for u in net_updates if u["cane_net"] is None)
    ok_count   = len(net_updates) - null_count
    print(f"    With computed net:  {ok_count:,}")
    print(f"    Set to NULL:        {null_count:,}")

    return {
        "total_rows":    total_rows,
        "null_net_rows": null_net_rows,
        "anomalies":     anomalies,
        "net_updates":   net_updates,
        "hy_data":       hy_data,
    }


def _compute_net_updates(hy_data: dict, anomalies: dict) -> list[dict]:
    """
    Precomputa los net_updates para todas las quinzenas.
    Devuelve lista de {id, cane_net, sugar_net, eth_net, eth_anh_net, eth_hyd_net, sugar_mix}.
    """
    # Sets de row_ids que son anomalias (net=NULL) y mislabeled (excluir del calculo)
    mislabeled_ids = {rid for _, _, _, rid, _ in anomalies["mislabeled"]}
    monoton_ids    = {rid for _, _, _, rid, _ in anomalies["monoton_breaks"]}
    null_after_gap_ids = set()

    updates = []

    for hy in sorted(hy_data.keys()):
        rows = sorted(hy_data[hy], key=lambda r: r[2])   # by fortnight_seq
        prev_row = None

        for i, cur_r in enumerate(rows):
            cur_id   = cur_r[0]
            cur_seq  = cur_r[2]

            # Mislabeled → skip (will be re-tagged to different harvest_year)
            if cur_id in mislabeled_ids:
                prev_row = None  # reset chain — gap after mislabeled
                continue

            # Detect gap from previous
            has_gap = (prev_row is not None and cur_seq != prev_row[2] + 1)
            if has_gap:
                null_after_gap_ids.add(cur_id)

            # Monotonicity break → NULL
            if cur_id in monoton_ids or cur_id in null_after_gap_ids:
                updates.append(_null_update(cur_id))
                prev_row = cur_r
                continue

            # Compute net
            # Col indices per latest_rows query (see phase_1_inspect comment)
            cum_cane    = _int(cur_r[6])
            cum_sug     = _int(cur_r[7])
            cum_eth_anh = _int(cur_r[8])
            cum_eth_hyd = _int(cur_r[9])
            cum_eth     = _int(cur_r[10])

            def _delta(cur_val, prev_val):
                if cur_val is None or prev_val is None:
                    return None
                return cur_val - prev_val

            if cur_seq == 1 or prev_row is None:
                # Season start: net = cumulative
                cane_net    = cum_cane
                sug_net     = cum_sug
                eth_anh_net = cum_eth_anh
                eth_hyd_net = cum_eth_hyd
                eth_net     = cum_eth
            else:
                prev_cum_cane    = _int(prev_row[6])
                prev_cum_sug     = _int(prev_row[7])
                prev_cum_eth_anh = _int(prev_row[8])
                prev_cum_eth_hyd = _int(prev_row[9])
                prev_cum_eth     = _int(prev_row[10])
                cane_net    = _delta(cum_cane,    prev_cum_cane)
                sug_net     = _delta(cum_sug,     prev_cum_sug)
                eth_anh_net = _delta(cum_eth_anh, prev_cum_eth_anh)
                eth_hyd_net = _delta(cum_eth_hyd, prev_cum_eth_hyd)
                eth_net     = _delta(cum_eth,     prev_cum_eth)

            # sugar_mix_pct from net (§3.2.C)
            sugar_mix = None
            if sug_net and eth_net and sug_net > 0 and eth_net > 0:
                denom = sug_net + eth_net * ETHANOL_EQUIV_FACTOR
                sugar_mix = round(sug_net / denom * 100, 3) if denom > 0 else None

            updates.append({
                "id":          cur_id,
                "cane_net":    cane_net,
                "sugar_net":   sug_net,
                "eth_net":     eth_net,
                "eth_anh_net": eth_anh_net,
                "eth_hyd_net": eth_hyd_net,
                "sugar_mix":   sugar_mix,
            })
            prev_row = cur_r

    return updates


def _null_update(row_id: int) -> dict:
    return {
        "id":          row_id,
        "cane_net":    None,
        "sugar_net":   None,
        "eth_net":     None,
        "eth_anh_net": None,
        "eth_hyd_net": None,
        "sugar_mix":   None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Two-key confirmation
# ─────────────────────────────────────────────────────────────────────────────

def phase_2_confirm(*, skip_prompt: bool):
    print("\n[Phase 2] Two-key safety prompt")
    if skip_prompt:
        print("  --yes passed: skipping interactive confirmation.")
        return
    print("  This will MODIFY brazil_production rows (net columns + sugar_mix_pct).")
    print("  Mislabeled rows will also be re-tagged to a different harvest_year.")
    print()
    print("  Type the exact phrase to confirm:  CLEANUP BRAZIL MAPA LEGACY")
    print("  (or Ctrl-C to abort)")
    answer = input("  > ").strip()
    if answer != "CLEANUP BRAZIL MAPA LEGACY":
        print(f"\n  Got '{answer}' — does not match. Aborting.")
        sys.exit(1)
    print("  Confirmed.")


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3 — Apply
# ─────────────────────────────────────────────────────────────────────────────

def phase_3_apply(session, inspection: dict) -> None:
    """
    Ejecuta todos los cambios en una sola transaccion:
      3a. Re-tag mislabeled rows (UPDATE harvest_year / fortnight_seq)
      3b. Batch UPDATE *_net + sugar_mix_pct para latest revisions
    """
    print("\n[Phase 3] Applying changes (single transaction)")
    print("=" * 72)

    anomalies  = inspection["anomalies"]
    net_updates = inspection["net_updates"]
    hy_data     = inspection["hy_data"]

    n_mislabeled_fixed = 0
    n_net_updated      = 0
    n_net_nulled       = 0

    # 3a. Fix mislabeled rows
    if anomalies["mislabeled"]:
        print(f"\n  [3a] Fixing {len(anomalies['mislabeled'])} mislabeled row(s)...")
        for hy, prev_seq, bad_seq, row_id, delta in anomalies["mislabeled"]:
            next_hy = _next_harvest_year(hy)
            # Check if target slot already exists
            conflict = session.execute(text("""
                SELECT COUNT(*) FROM brazil_production
                WHERE harvest_year = :hy AND fortnight_seq = 1
                  AND report_issue_date = (
                      SELECT report_issue_date FROM brazil_production WHERE id = :rid
                  )
            """), {"hy": next_hy, "rid": row_id}).scalar()

            if conflict:
                print(f"    id={row_id}: SKIP — target ({next_hy}, seq=1) already exists")
                continue

            session.execute(text("""
                UPDATE brazil_production
                SET    harvest_year    = :next_hy,
                       fortnight_seq  = 1
                WHERE  id = :rid
            """), {"next_hy": next_hy, "rid": row_id})
            print(f"    id={row_id}: {hy} seq{bad_seq} → {next_hy} seq=1  (Δ={delta:+,}t)")
            n_mislabeled_fixed += 1

    # 3b. Batch UPDATE *_net + sugar_mix_pct
    print(f"\n  [3b] Updating net columns for {len(net_updates):,} latest-revision rows...")
    for upd in net_updates:
        session.execute(text("""
            UPDATE brazil_production SET
              cane_crushed_t_net          = :cane_net,
              sugar_t_net                 = :sugar_net,
              ethanol_total_m3_net        = :eth_net,
              ethanol_anhydrous_m3_net    = :eth_anh_net,
              ethanol_hydrated_m3_net     = :eth_hyd_net,
              sugar_mix_pct               = :sugar_mix
            WHERE id = :id
        """), upd)
        if upd["cane_net"] is None:
            n_net_nulled += 1
        else:
            n_net_updated += 1

    session.commit()
    print(f"\n  Commit OK.")
    print(f"  Mislabeled fixed:    {n_mislabeled_fixed}")
    print(f"  Net computed:        {n_net_updated}")
    print(f"  Net set NULL:        {n_net_nulled}")

    # Post-check: conteo de nulos residuales
    residual_null = session.execute(text(
        "SELECT COUNT(*) FROM brazil_production WHERE cane_crushed_t_net IS NULL"
    )).scalar()
    print(f"\n  Residual net=NULL:   {residual_null:,}  "
          f"(expected: old revisions only, not latest)")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="P3.E.7 — Cleanup anomalias + computo retroactivo *_net",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Aplicar cambios (default: dry-run read-only)",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Saltar confirmacion interactiva (uso en automacion)",
    )
    args = parser.parse_args()

    session = get_session()
    try:
        inspection = phase_1_inspect(session)

        if not args.apply:
            print("\n[DRY-RUN] No changes applied. Use --apply to execute.")
            print(f"  {len(inspection['net_updates'])} net updates queued.")
            print(f"  {len(inspection['anomalies']['mislabeled'])} mislabeled rows would be fixed.")
            return

        phase_2_confirm(skip_prompt=args.yes)
        phase_3_apply(session, inspection)

        print("\n[P3.E.7] COMPLETE.")

    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(1)
    finally:
        session.close()


if __name__ == "__main__":
    main()
