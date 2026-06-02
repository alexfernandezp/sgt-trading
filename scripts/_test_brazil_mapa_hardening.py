"""
P3 — Hardening defensivo de ingestion/brazil_mapa.py.

Valida las 4 capas implementadas (paralelas a CEPEA/Santos):

  P3.A — Silent excepts erradicados:
    _parse_date_from_url   fallback failure → WARNING
    _fortnight_seq         harvest_year malformado → WARNING + fallback
    _valid_season          key no parseable → WARNING + False
    _num                   string corrupto → WARNING (vacio NO warneado)

  P3.B — Range gates §3.2 dentro de _parse_xls:
    cane_crushed_t > 100M, sugar_t > 5M, ethanol_total_m3 > 5M,
    sugar_mix_pct fuera de [20, 60] → fila descartada + WARN

  P3.C — Structural validator (validate_count, min_success_rate=0.85):
    90/100 OK, 80/100 ERROR, 85/100 boundary OK, n_total=0 raise

  P3.D — Freshness gate read-side (35d, §4.1):
    fresh / stale / boundary / boundary+1 / empty

Uso: py scripts/_test_brazil_mapa_hardening.py
"""
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.brazil_mapa import (
    MAPA_MAX_AGE_DAYS,
    MAPA_STRUCTURAL_MIN_SUCCESS_RATE,
    _fortnight_seq,
    _parse_date_from_url,
    _parse_xls,
    _valid_season,
    get_latest_production,
)
from services.data_quality import DataQualityError, validate_count


# ── WARNING capture ──────────────────────────────────────────────────────
captured: list[tuple[int, str, str]] = []   # (levelno, logger_name, message)


class _Cap(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.WARNING:
            captured.append((record.levelno, record.name, record.getMessage()))


logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(name)s - %(message)s")
for name in ("services.data_quality", "ingestion.brazil_mapa"):
    logging.getLogger(name).addHandler(_Cap())


results: list[tuple[str, bool]] = []


def report(name: str, ok: bool, detail: str = ""):
    results.append((name, ok))
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name} {detail}")


TODAY = date(2026, 6, 2)


# ─────────────────────────────────────────────────────────────────────────
# P3.A — Silent excepts
# ─────────────────────────────────────────────────────────────────────────
print("\n=== P3.A — _valid_season ===")
captured.clear()
report("'2025-2026' (consecutiva) → True", _valid_season("2025-2026") is True)
report("'2025-2027' (gap) → False", _valid_season("2025-2027") is False)
report("'2025' (single) → False", _valid_season("2025") is False)
report("'YYYY-ZZZZ' (no numeric) → False + WARN",
       _valid_season("YYYY-ZZZZ") is False)
warns = [m for lv, _, m in captured if "_valid_season" in m]
report("WARNING emitido para 'YYYY-ZZZZ'", len(warns) >= 1, f"got {len(warns)}")


print("\n=== P3.A — _fortnight_seq fallback ===")
captured.clear()
ref = date(2026, 4, 15)
report("'2026-2027' valido → seq>=1", _fortnight_seq(ref, "2026-2027") >= 1)

captured.clear()
seq = _fortnight_seq(ref, "INVALID_YEAR")
report("'INVALID_YEAR' → fallback ref_date.year, no crash", seq >= 1)
report("WARNING emitido en fallback",
       any("_fortnight_seq" in m for _, _, m in captured))


print("\n=== P3.A — _parse_date_from_url fallback ===")
captured.clear()
# Caso valido: archivo DD/MM/YY al final del nombre
d = _parse_date_from_url(
    "https://x/Acompanhamentodaproduo2526_010526.xls", "2025-2026",
)
report("URL valida → fecha 2026-05-01",
       d == date(2026, 5, 1), f"got={d}")

# Caso fallback: URL sin fecha pero harvest_year valido → April 1 first_year
captured.clear()
d = _parse_date_from_url("https://x/no_date_in_name.xls", "2025-2026")
report("URL sin fecha + harvest valido → fallback April 1 2025",
       d == date(2025, 4, 1))

# Caso fallback FALLA: harvest_year malformado
captured.clear()
d = _parse_date_from_url("https://x/no_date.xls", "BADYEAR")
report("URL sin fecha + harvest malformado → None + WARN",
       d is None and any("_parse_date_from_url" in m for _, _, m in captured))


# ─────────────────────────────────────────────────────────────────────────
# FakeSheet / FakeWorkbook para test de _parse_xls + _num
# ─────────────────────────────────────────────────────────────────────────
class FakeSheet:
    """Sheet stub con la API mínima usada por _parse_xls (xlrd-compatible)."""
    def __init__(self, name: str, grid: list[list]):
        self.name  = name
        self.grid  = grid
        self.nrows = len(grid)
        self.ncols = max(len(r) for r in grid) if grid else 0

    def cell_value(self, r, c):
        if r >= self.nrows or c >= len(self.grid[r]):
            return ""
        return self.grid[r][c]


class FakeWorkbook:
    def __init__(self, sheets): self._sheets = sheets
    def sheets(self): return self._sheets


def _make_mapa_grid(cane=40_000_000, sugar=2_000_000, etanol=1_500_000,
                    cane_str=False):
    """
    Sheet MAPA mínimamente coherente. Headers:
      row 0: 'UF' en col 0
      row 1: subheaders 'Cana', 'Açúcar', 'Etanol' en cols 1, 2, 3
      row 2: units '(t)', '(t)', '(m3)'
      row 3: TOTAL BRASIL
      row 4: Tot. con valores numericos
    """
    cane_val = str(cane) if cane_str else cane
    return [
        ["UF",            "",       "",        ""],         # 0
        ["",              "Cana",   "Açúcar",  "Etanol"],   # 1
        ["",              "(t)",    "(t)",     "(m3)"],     # 2
        ["TOTAL BRASIL",  "",       "",        ""],         # 3
        ["Tot.",          cane_val, sugar,     etanol],     # 4
    ]


def _patch_workbook(grid):
    sheet = FakeSheet("gerarRelMain", grid)
    wb    = FakeWorkbook([sheet])
    return patch("ingestion.brazil_mapa.xlrd.open_workbook", return_value=wb)


# ─────────────────────────────────────────────────────────────────────────
# P3.A — _num via _parse_xls (silent except → WARNING para basura)
# ─────────────────────────────────────────────────────────────────────────
print("\n=== P3.A — _num WARNING en string corrupto ===")
captured.clear()
# Cana col contiene string no parseable → debe WARN + retornar None
grid = _make_mapa_grid()
grid[4][1] = "ZZZ-BROKEN-VALUE"
with _patch_workbook(grid):
    out = _parse_xls(b"fake", "https://x/test.xls", "2025-2026")
report("Cana=garbage → _parse_xls retorna None", out is None)
report("WARN emitido por _num para string corrupto",
       any("_num" in m for _, _, m in captured))


print("\n=== P3.A — _num silencio en celda vacia ===")
captured.clear()
# Sugar col vacio (legitimo: MAPA deja celdas '' al final) → no debe WARN
grid = _make_mapa_grid()
grid[4][2] = ""
with _patch_workbook(grid):
    out = _parse_xls(b"fake", "https://x/test.xls", "2025-2026")
# Debe parsear OK con sugar=None
report("Sugar='' (vacio) → _parse_xls retorna dict",
       out is not None and out.get("sugar_t") is None)
report("sin WARN para celda vacia (legitimo silencio)",
       not any("_num" in m for _, _, m in captured))


# ─────────────────────────────────────────────────────────────────────────
# P3.B — Range gates §3.2
# ─────────────────────────────────────────────────────────────────────────
print("\n=== P3.B — Range gates ===")

# Baseline: todo in-range
captured.clear()
with _patch_workbook(_make_mapa_grid()):
    out = _parse_xls(b"f", "https://x/ok.xls", "2025-2026")
report("baseline in-range → dict retornado",
       out is not None and out.get("cane_crushed_t") == 40_000_000)

# Cane out of range (>100M)
captured.clear()
with _patch_workbook(_make_mapa_grid(cane=200_000_000)):
    out = _parse_xls(b"f", "https://x/cane_huge.xls", "2025-2026")
report("cane=200M (>100M) → fila DESCARTADA", out is None)
report("WARN brazil_mapa.cane_crushed_t",
       any("cane_crushed_t" in m for _, _, m in captured))

# Sugar out of range (>5M)
captured.clear()
with _patch_workbook(_make_mapa_grid(sugar=10_000_000)):
    out = _parse_xls(b"f", "https://x/sugar_huge.xls", "2025-2026")
report("sugar=10M (>5M) → fila DESCARTADA", out is None)
report("WARN brazil_mapa.sugar_t",
       any("sugar_t" in m for _, _, m in captured))

# Ethanol out of range (>5M)
captured.clear()
with _patch_workbook(_make_mapa_grid(etanol=10_000_000)):
    out = _parse_xls(b"f", "https://x/etanol_huge.xls", "2025-2026")
report("ethanol=10M (>5M) → fila DESCARTADA", out is None)
report("WARN brazil_mapa.ethanol_total_m3",
       any("ethanol_total_m3" in m for _, _, m in captured))

# Sugar mix forzado < 20% (ratio bajo) — fuerzo con sugar muy pequeño
captured.clear()
# sugar=100k, etanol=4M → equiv= 4M*1.2 = 4.8M; mix = 100k/(100k+4.8M)*100 = ~2%
with _patch_workbook(_make_mapa_grid(sugar=100_000, etanol=4_000_000)):
    out = _parse_xls(b"f", "https://x/lowmix.xls", "2025-2026")
report("sugar_mix~2% (<20%) → fila DESCARTADA", out is None)
report("WARN brazil_mapa.sugar_mix_pct (bajo)",
       any("sugar_mix_pct" in m for _, _, m in captured))

# Sugar mix > 60% — sugar grande, etanol pequeño
captured.clear()
# sugar=4M, etanol=100k m3 → equiv=120k t; mix = 4M/(4M+120k)*100 = ~97%
with _patch_workbook(_make_mapa_grid(sugar=4_000_000, etanol=100_000)):
    out = _parse_xls(b"f", "https://x/highmix.xls", "2025-2026")
report("sugar_mix~97% (>60%) → fila DESCARTADA", out is None)
report("WARN brazil_mapa.sugar_mix_pct (alto)",
       any("sugar_mix_pct" in m for _, _, m in captured))

# Boundary: cane=100M exacto → OK
captured.clear()
with _patch_workbook(_make_mapa_grid(cane=100_000_000)):
    out = _parse_xls(b"f", "https://x/boundary.xls", "2025-2026")
report("cane=100M boundary (==max) → ACEPTADO",
       out is not None and out.get("cane_crushed_t") == 100_000_000)


# ─────────────────────────────────────────────────────────────────────────
# P3.C — Structural validator (validate_count)
# ─────────────────────────────────────────────────────────────────────────
print("\n=== P3.C — validate_count structural ===")

# 90/100 = 0.9 > 0.85 → OK
rate = validate_count(
    90, 100,
    min_success_rate=MAPA_STRUCTURAL_MIN_SUCCESS_RATE,
    source="brazil_mapa", field="xls_parse_success_rate",
)
report("90/100 (0.9 > 0.85) → rate retornado", abs(rate - 0.9) < 1e-9)

# 85/100 = 0.85 exacto → OK
rate = validate_count(
    85, 100,
    min_success_rate=MAPA_STRUCTURAL_MIN_SUCCESS_RATE,
    source="brazil_mapa", field="xls_parse_success_rate",
)
report("85/100 boundary (==0.85) → rate retornado", abs(rate - 0.85) < 1e-9)

# 80/100 = 0.8 < 0.85 → DataQualityError
try:
    validate_count(
        80, 100,
        min_success_rate=MAPA_STRUCTURAL_MIN_SUCCESS_RATE,
        source="brazil_mapa", field="xls_parse_success_rate",
    )
    report("80/100 (<0.85) → DataQualityError", False, "no raise")
except DataQualityError as e:
    report("80/100 (<0.85) → DataQualityError", True)
    report("error menciona '80.0%'", "80.0%" in str(e))
    report("error carga source=brazil_mapa", e.source == "brazil_mapa")
    report("error carga expected '>= 85%'",
           "85%" in str(e.expected) or "85" in str(e.expected))

# n_total=0 → raise (no rows to evaluate)
try:
    validate_count(
        0, 0,
        min_success_rate=MAPA_STRUCTURAL_MIN_SUCCESS_RATE,
        source="brazil_mapa", field="xls_parse_success_rate",
    )
    report("0/0 → DataQualityError ('no rows')", False, "no raise")
except DataQualityError as e:
    report("0/0 → DataQualityError ('no rows')",
           "no rows" in str(e).lower())


# ─────────────────────────────────────────────────────────────────────────
# P3.D — get_latest_production freshness gate
# ─────────────────────────────────────────────────────────────────────────
class _ExecResult:
    def __init__(self, rows): self._rows = rows
    def fetchall(self): return self._rows


class _StubSession:
    def __init__(self, rows): self._rows = rows
    def execute(self, _stmt, _params=None): return _ExecResult(self._rows)


def _mapa_row(days_old: int):
    """
    Row shape de get_latest_production:
      (report_date, harvest_year, fortnight_seq,
       cane, sugar, ethanol_total, sugar_mix)
    """
    return (
        TODAY - timedelta(days=days_old),
        "2025-2026", 5, 40_000_000, 2_000_000, 1_500_000, 47.5,
    )


print("\n=== P3.D — freshness gate get_latest_production ===")
captured.clear()
sess = _StubSession([_mapa_row(days_old=15), _mapa_row(days_old=30)])
out = get_latest_production(sess, n=4, reference=TODAY)
report("latest 15d old (fresh) → lista con N filas",
       len(out) == 2)
report("sin WARN para fresh",
       not any("latest_report_date" in m for _, _, m in captured))


captured.clear()
sess = _StubSession([_mapa_row(days_old=MAPA_MAX_AGE_DAYS)])
out = get_latest_production(sess, n=4, reference=TODAY)
report("boundary exacto (35d) → ACEPTADO", len(out) == 1)


captured.clear()
sess = _StubSession([_mapa_row(days_old=MAPA_MAX_AGE_DAYS + 1)])
out = get_latest_production(sess, n=4, reference=TODAY)
report("boundary+1 (36d) → lista vacia + WARN", out == [])
report("WARN emitido (latest_report_date stale)",
       any("latest_report_date" in m for _, _, m in captured))


captured.clear()
sess = _StubSession([_mapa_row(days_old=60)])
out = get_latest_production(sess, n=4, reference=TODAY)
report("muy stale (60d) → lista vacia", out == [])


captured.clear()
sess = _StubSession([])
out = get_latest_production(sess, n=4, reference=TODAY)
report("empty DB → lista vacia sin WARN",
       out == []
       and not any("latest_report_date" in m for _, _, m in captured))


# ── Summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 72)
passed = sum(1 for _, ok in results if ok)
total = len(results)
print(f"RESULTS: {passed}/{total} passed")
if passed < total:
    for name, ok in results:
        if not ok:
            print(f"  FAIL: {name}")
    sys.exit(1)
print("All brazil_mapa hardening tests passed.")
