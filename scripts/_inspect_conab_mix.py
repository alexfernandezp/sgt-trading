"""
Auditoría del balance de masa agrícola CONAB:
Cane crushing vs Sugar production vs Ethanol diversion.

Hipótesis del usuario (2026-06-01):
  La caída en sugar_total_mt entre 2026/27 lev=1 y 2025/26 lev=1 NO implica
  destrucción de cultivo. Puede ser DESVÍO INDUSTRIAL hacia etanol (el mix
  azúcar/etanol es decisión de las moliendas, no del campo). El satélite ve
  campos verdes igual; el azúcar baja porque el mill envía más caña a etanol.

Plan: extraer cane_total_mt + sugar_total_mt + ethanol_cana_blt para ambos
levantamentos #1 y computar ratios efectivos. Identificar qué dato falta.
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal
from sqlalchemy import text


def main():
    print("=" * 78)
    print("  Auditoría Balance de Masa Agrícola — CONAB lev=1 YoY")
    print("=" * 78)

    with SessionLocal() as s:
        rows = s.execute(text("""
            SELECT season, levantamento, pub_date,
                   cane_total_mt, sugar_total_mt, ethanol_cana_blt,
                   ethanol_total_blt, ethanol_hydrous_blt, ethanol_anhydrous_blt,
                   yoy_cane_pct, yoy_sugar_pct, yoy_ethanol_cana_pct,
                   revision_sugar_pct, revision_ethanol_pct,
                   sp_cane_mt, sp_sugar_mt
            FROM conab_cana_levantamento
            WHERE season IN ('2025/26', '2026/27') AND levantamento = 1
            ORDER BY season ASC
        """)).fetchall()

        if not rows:
            print("[!] No hay filas con levantamento=1 en season ∈ {2025/26, 2026/27}")
            return 1

        print(f"\n  Filas encontradas: {len(rows)}")
        for r in rows:
            (
                season, lev, pub_date,
                cane_t, sugar_t, eth_cana, eth_total, eth_hyd, eth_anh,
                yoy_cane, yoy_sugar, yoy_eth,
                rev_sugar, rev_eth,
                sp_cane, sp_sugar,
            ) = r
            print(f"\n  ── {season} lev={lev}  pub_date={pub_date} ──")
            print(f"      cane_total_mt    : {cane_t}")
            print(f"      sugar_total_mt   : {sugar_t}")
            print(f"      ethanol_cana_blt : {eth_cana}")
            print(f"      ethanol_total_blt: {eth_total}  (caña+maíz)")
            print(f"      ethanol_hydrous  : {eth_hyd}")
            print(f"      ethanol_anhydrous: {eth_anh}")
            print(f"      yoy_cane_pct     : {yoy_cane}")
            print(f"      yoy_sugar_pct    : {yoy_sugar}")
            print(f"      yoy_ethanol_pct  : {yoy_eth}")
            print(f"      revision_sugar   : {rev_sugar}")
            print(f"      revision_ethanol : {rev_eth}")
            print(f"      sp_cane_mt       : {sp_cane}")
            print(f"      sp_sugar_mt      : {sp_sugar}")

        # ── Mix ratio computation ──
        if len(rows) == 2:
            r25, r26 = rows
            cane_25, sugar_25, eth_25 = r25[3], r25[4], r25[5]
            cane_26, sugar_26, eth_26 = r26[3], r26[4], r26[5]

            print("\n" + "═" * 78)
            print("  BALANCE DE MASA — comparación lev=1 YoY")
            print("═" * 78)

            def fmt(v):
                return f"{float(v):>10.2f}" if v is not None else "       N/D"

            def pct_delta(new, old):
                if new is None or old is None or float(old) == 0:
                    return "    N/D"
                return f"{(float(new)/float(old) - 1) * 100:+6.2f}%"

            print(f"\n  {'Metric':<25} {'2025/26 lev=1':>15} {'2026/27 lev=1':>15} {'Δ YoY':>10}")
            print("  " + "─" * 70)
            print(f"  {'cane_total_mt':<25} {fmt(cane_25):>15} {fmt(cane_26):>15} {pct_delta(cane_26, cane_25):>10}")
            print(f"  {'sugar_total_mt':<25} {fmt(sugar_25):>15} {fmt(sugar_26):>15} {pct_delta(sugar_26, sugar_25):>10}")
            print(f"  {'ethanol_cana_blt':<25} {fmt(eth_25):>15} {fmt(eth_26):>15} {pct_delta(eth_26, eth_25):>10}")

            # Sugar yield per ton of cane (kg/t)
            def sugar_yield_kg_per_t(sugar_mt, cane_mt):
                if sugar_mt is None or cane_mt is None or float(cane_mt) == 0:
                    return None
                # sugar_mt = millones de toneladas, cane_mt = millones de toneladas
                # ratio = kg azúcar por tonelada caña = (sugar_mt × 1e9 kg) / (cane_mt × 1e6 t) = sugar_mt/cane_mt × 1000
                return float(sugar_mt) / float(cane_mt) * 1000

            sy_25 = sugar_yield_kg_per_t(sugar_25, cane_25)
            sy_26 = sugar_yield_kg_per_t(sugar_26, cane_26)
            print(f"\n  {'sugar yield (kg/t cane)':<25} "
                  f"{(f'{sy_25:>10.1f}' if sy_25 else '   N/D'):>15} "
                  f"{(f'{sy_26:>10.1f}' if sy_26 else '   N/D'):>15}")
            if sy_25 and sy_26:
                delta_sy = (sy_26 / sy_25 - 1) * 100
                print(f"  {'Δ sugar yield YoY':<25} {' ':>30} {delta_sy:>+9.2f}%")
                print(f"\n  Interpretación:")
                print(f"    Si Δ cane es +X% pero Δ sugar es −Y%:")
                print(f"      → cane sí está fuerte (no destrucción cultivo)")
                print(f"      → sugar baja por MIX (más caña a etanol)")
                print(f"      → satélite vería NDVI alto/normal a pesar del shock azúcar")

            # Ethanol yield per ton of cane (L/t)
            def eth_yield_l_per_t(eth_blt, cane_mt):
                if eth_blt is None or cane_mt is None or float(cane_mt) == 0:
                    return None
                # eth_blt = billones de litros, cane_mt = millones de toneladas
                # L/t = (eth_blt × 1e9 L) / (cane_mt × 1e6 t) = eth_blt/cane_mt × 1000
                return float(eth_blt) / float(cane_mt) * 1000

            ey_25 = eth_yield_l_per_t(eth_25, cane_25)
            ey_26 = eth_yield_l_per_t(eth_26, cane_26)
            print(f"\n  {'ethanol yield (L/t cane)':<25} "
                  f"{(f'{ey_25:>10.1f}' if ey_25 else '   N/D'):>15} "
                  f"{(f'{ey_26:>10.1f}' if ey_26 else '   N/D'):>15}")

        print("\n" + "═" * 78)
        print("Inspection complete.")


if __name__ == "__main__":
    main()
