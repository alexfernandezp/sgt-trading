"""
Smoke test manual — Brazil Crop Progress.

Verifica invariantes sin pytest:
  1. season_fortnight_seq: todas las quincenas CS tienen seq 1-24
  2. _decumulate: de-acumulación correcta (net[0]=cum[0], sum(nets)=cum[-1])
  3. cumsum monotónico dentro de cada safra (valores no negativos -> cumsum no decrece)
  4. Alineación seq: same-point-in-season cross-año
  5. compute_crop_progress no crashea con baseline parcial

Uso:
    py scripts/smoke_crop_progress.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0


def ok(msg):
    global PASS
    PASS += 1
    print(f"  [OK] {msg}")


def fail(msg):
    global FAIL
    FAIL += 1
    print(f"  [FAIL] {msg}")


# ── Test 1: season_fortnight_seq ─────────────────────────────────────────────
print("\n[1] season_fortnight_seq — cobertura y unicidad")
from ingestion.unica import season_fortnight_seq, _SEASON_FORTNIGHTS
from datetime import date

seqs_seen = set()
for i, (mon, day) in enumerate(_SEASON_FORTNIGHTS):
    year = 2024 if mon >= 4 else 2025
    d = date(year, mon, day)
    seq = season_fortnight_seq(d)
    if seq != i + 1:
        fail(f"seq({d}) = {seq}, esperado {i + 1}")
    else:
        seqs_seen.add(seq)

if len(seqs_seen) == 24:
    ok("24 quincenas únicas con seq 1-24")
else:
    fail(f"solo {len(seqs_seen)} seqs únicos")

# Fecha fuera de quincena
bad_seq = season_fortnight_seq(date(2024, 4, 15))
if bad_seq is None:
    ok("día 15/04 -> seq=None (no es quincena válida)")
else:
    fail(f"día 15/04 -> seq={bad_seq}, esperado None")


# ── Test 2: _decumulate ──────────────────────────────────────────────────────
print("\n[2] _decumulate — de-acumulación correcta")
from ingestion.unica import _decumulate

cum_series = {1: 10.0, 2: 25.0, 3: 40.0, 5: 70.0}
net = _decumulate(cum_series)

if net[1] == 10.0:
    ok("net[primer_seq] = cum[primer_seq]")
else:
    fail(f"net[1] = {net[1]}, esperado 10.0")

if net[2] == 15.0:
    ok("net[2] = cum[2] - cum[1] = 15.0")
else:
    fail(f"net[2] = {net[2]}, esperado 15.0")

if net[5] == 30.0:
    ok("net[5] = cum[5] - cum[3] = 30.0 (salto de seq)")
else:
    fail(f"net[5] = {net[5]}, esperado 30.0")

total_net = sum(net.values())
max_cum = max(cum_series.values())
if abs(total_net - max_cum) < 0.001:
    ok(f"sum(nets) = {total_net:.1f} == max(cum) = {max_cum:.1f}")
else:
    fail(f"sum(nets) = {total_net:.1f} != max(cum) = {max_cum:.1f}")


# ── Test 3: cumsum monotónico en DB ──────────────────────────────────────────
print("\n[3] cumsum monotónico sobre datos reales de unica_biweekly")
try:
    from database import SessionLocal
    from services.brazil_crop_progress import _cumsum_by_safra, _get_unica_biweekly

    with SessionLocal() as session:
        rows = _get_unica_biweekly(session, region="CS")
        cum_map = _cumsum_by_safra(rows, "cane_crushed_t")

        if not cum_map:
            fail("sin datos en unica_biweekly CS")
        else:
            ok(f"{len(cum_map)} entradas (safra, seq) en cum_map")

        # Verificar monotonía por safra
        from collections import defaultdict
        by_safra = defaultdict(list)
        for (safra, seq), val in cum_map.items():
            by_safra[safra].append((seq, val))

        violations = 0
        for safra, items in by_safra.items():
            items.sort()
            for i in range(1, len(items)):
                if items[i][1] < items[i-1][1] - 1e3:  # tolerancia 1t
                    violations += 1
                    fail(f"cumsum decrece en {safra} seq {items[i-1][0]}->{items[i][0]}: "
                         f"{items[i-1][1]:.0f}->{items[i][1]:.0f}")
                    break

        if violations == 0:
            ok(f"cumsum monotónico en todas las safras ({len(by_safra)} safras)")

except Exception as e:
    fail(f"no se pudo conectar a DB: {e}")


# ── Test 4: alineación seq cross-año ─────────────────────────────────────────
print("\n[4] alineación seq cross-año (same-point-in-season)")
from ingestion.unica import season_fortnight_seq

d_2024 = date(2024, 5, 1)   # seq=2 en safra 2023/24
d_2025 = date(2025, 5, 1)   # seq=2 en safra 2024/25
d_2026 = date(2026, 5, 1)   # seq=2 en safra 2025/26

seqs = [season_fortnight_seq(d) for d in [d_2024, d_2025, d_2026]]
if all(s == 2 for s in seqs):
    ok("01/may mapea a seq=2 en cualquier año (same-point-in-season)")
else:
    fail(f"seqs para 01/may en 2024/25/26 = {seqs}, esperado [2,2,2]")


# ── Test 5: compute_crop_progress no crashea ─────────────────────────────────
print("\n[5] compute_crop_progress — no crashea con baseline real")
try:
    from database import SessionLocal
    from services.brazil_crop_progress import compute_crop_progress

    with SessionLocal() as session:
        result = compute_crop_progress(session, region="CS")

    if result.get("error"):
        fail(f"error en compute_crop_progress: {result['error']}")
    else:
        ok(f"compute_crop_progress OK — safra={result.get('latest_safra')} "
           f"seq={result.get('latest_seq')} "
           f"baseline={result.get('baseline_years')} años")

        # Verificar que las señales no son None si hay suficiente baseline
        n = result.get("baseline_years", 0)
        if n >= 10:
            sig_a = result.get("A_cane_pace") or {}
            if sig_a.get("conviction") != "INSUFFICIENT_DATA":
                ok(f"Señal A con convicción={sig_a.get('conviction')}")
            else:
                fail("Señal A = INSUFFICIENT_DATA con n≥10")
        else:
            ok(f"baseline n={n} < 10, INSUFFICIENT_DATA esperado")

        # Proyección sugar
        proj = (result.get("F_proj") or {}).get("sugar")
        if proj and proj.get("point_mt"):
            ok(f"Proyección azúcar = {proj['point_mt']} Mt")
        else:
            ok("Proyección azúcar no disponible (temprano en safra o sin baseline)")

        # Bias
        bias = result.get("H_bias_ice11")
        if bias is not None:
            ok(f"Bias ICE No.11 = {bias:+.3f}")
        else:
            ok("Bias ICE No.11 no disponible (señales insuficientes)")

except Exception as e:
    import traceback
    fail(f"compute_crop_progress lanzó excepción: {e}")
    traceback.print_exc()


# ── Resumen ───────────────────────────────────────────────────────────────────
print(f"\n{'='*40}")
print(f"Smoke test: {PASS} OK  {FAIL} FAIL")
if FAIL == 0:
    print("  PASS: Todos los invariantes verdes")
else:
    print(f"  FAIL: {FAIL} invariantes fallaron")
    sys.exit(1)
