"""
ISMA (Indian Sugar & Bio-energy Manufacturers Association) — ingesta manual de datos.

Publica produccion quincenal Oct-Abr. Cifras NETAS: ya incluyen la diversion
juice-to-ethanol. NO aplicar factor etanol adicional.
1 lakh tonne = 0.1 Mt.

Uso:
  py scripts/ingest_isma.py --date 2026-04-30 --lakh 275.28
  py scripts/ingest_isma.py --date 2026-04-30 --lakh 275.28 --yoy 7.0 --mills 5
  py scripts/ingest_isma.py --date 2026-04-30 --mt 27.528 --yoy 7.0
  py scripts/ingest_isma.py --list
  py scripts/ingest_isma.py --seed
"""
import sys
import os
import argparse
import logging
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ingest_isma")

# ---------------------------------------------------------------------------
# Mapa de progreso de temporada por mes (Oct=inicio, Apr=casi fin)
# Basado en ritmo historico de molienda india.
# ---------------------------------------------------------------------------
SEASON_PROGRESS = {
    10: 5.0,   # octubre  — apertura de temporada
    11: 18.0,  # noviembre
    12: 36.0,  # diciembre
    1:  55.0,  # enero
    2:  72.0,  # febrero
    3:  86.0,  # marzo
    4:  96.0,  # abril
    5:  100.0, # mayo en adelante — temporada cerrada
}

# Datos historicos 2025-26 conocidos para --seed
SEED_DATA_2526 = [
    {
        "data_date":          date(2025, 12, 31),
        "pub_date":           None,
        "cumulative_lakh_t":  118.97,
        "yoy_change_pct":     25.0,
        "mills_operating":    None,
        "notes":              "Seed: dic 31, 2025. +25% YoY.",
    },
    {
        "data_date":          date(2026, 3, 17),
        "pub_date":           None,
        "cumulative_lakh_t":  262.10,
        "yoy_change_pct":     10.5,
        "mills_operating":    None,
        "notes":              "Seed: mar 17, 2026. +10.5% YoY.",
    },
    {
        "data_date":          date(2026, 3, 31),
        "pub_date":           None,
        "cumulative_lakh_t":  272.31,
        "yoy_change_pct":     9.0,
        "mills_operating":    None,
        "notes":              "Seed: mar 31, 2026. +9% YoY.",
    },
    {
        "data_date":          date(2026, 4, 15),
        "pub_date":           None,
        "cumulative_lakh_t":  274.80,
        "yoy_change_pct":     8.0,
        "mills_operating":    None,
        "notes":              "Seed: abr 15, 2026. +8% YoY.",
    },
    {
        "data_date":          date(2026, 4, 30),
        "pub_date":           None,
        "cumulative_lakh_t":  275.28,
        "yoy_change_pct":     7.0,
        "mills_operating":    5,
        "notes":              "Seed: abr 30, 2026. FINAL — solo 5 fabricas activas. +7% YoY.",
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def infer_marketing_year(d: date) -> int:
    """Devuelve el año inicio de temporada: si mes>=10 => ese año; si <10 => año-1."""
    return d.year if d.month >= 10 else d.year - 1


def compute_season_progress(d: date) -> float:
    """Devuelve el % de temporada completado segun el mes del dato.
    Temporada india: Oct-Abr. May-Sep = fuera de temporada (100%).
    """
    month = d.month
    # May-Sep: temporada cerrada (fuera de molido)
    if 5 <= month <= 9:
        return SEASON_PROGRESS[5]  # 100.0
    return SEASON_PROGRESS.get(month, 100.0)


def compute_estimated_full_year(cumulative_mt: float, season_progress_pct: float) -> float:
    """
    Proyecta el full-year. Sincronizado con isma_india._project_full_year():
      >= 95%: temporada practicamente cerrada → cumulative * 1.003
      < 5% : inicio de temporada → cumulative * 1.15 (sin dividir)
      resto : cumulative / progress, cap 115% del cumulativo
    """
    progress = season_progress_pct / 100.0
    if progress >= 0.95:
        return round(cumulative_mt * 1.003, 3)
    if progress < 0.05:
        return round(cumulative_mt * 1.15, 3)
    raw = cumulative_mt / progress
    cap = cumulative_mt * 1.15
    return round(min(raw, cap), 3)


def upsert_isma(session, record_data: dict) -> tuple[object, bool]:
    """
    Inserta o actualiza un IsmaRelease. Devuelve (objeto, is_new).
    """
    from models.market_data import IsmaRelease
    from sqlalchemy.exc import IntegrityError

    data_date      = record_data["data_date"]
    marketing_year = record_data["marketing_year"]

    existing = (
        session.query(IsmaRelease)
        .filter_by(data_date=data_date, marketing_year=marketing_year)
        .first()
    )

    if existing:
        for key, value in record_data.items():
            setattr(existing, key, value)
        session.commit()
        return existing, False
    else:
        obj = IsmaRelease(**record_data)
        session.add(obj)
        session.commit()
        return obj, True


def build_record(
    data_date: date,
    lakh: float | None,
    mt: float | None,
    yoy: float | None,
    mills: int | None,
    mh: float | None,
    up: float | None,
    ka: float | None,
    pub_date: date | None = None,
    source: str = "manual_cli",
    notes: str | None = None,
) -> dict:
    """Construye el diccionario de campos para IsmaRelease."""
    # Resolver lakh/mt
    if lakh is not None and mt is None:
        mt_val = round(lakh / 10.0, 3)
        lakh_val = lakh
    elif mt is not None and lakh is None:
        lakh_val = round(mt * 10.0, 2)
        mt_val = mt
    elif lakh is not None and mt is not None:
        # Ambos dados: usar lakh como referencia
        lakh_val = lakh
        mt_val = round(lakh / 10.0, 3)
    else:
        raise ValueError("Debes proporcionar --lakh o --mt")

    marketing_year     = infer_marketing_year(data_date)
    season_progress    = compute_season_progress(data_date)
    estimated_full_yr  = compute_estimated_full_year(mt_val, season_progress)

    return {
        "data_date":             data_date,
        "pub_date":              pub_date,
        "marketing_year":        marketing_year,
        "cumulative_lakh_t":     lakh_val,
        "cumulative_mt":         mt_val,
        "yoy_change_pct":        yoy,
        "season_progress_pct":   round(season_progress, 1),
        "estimated_full_year_mt": estimated_full_yr,
        "mills_operating":       mills,
        "maharashtra_lakh_t":    mh,
        "up_lakh_t":             up,
        "karnataka_lakh_t":      ka,
        "source":                source,
        "notes":                 notes,
    }


def print_record(obj, is_new: bool):
    """Muestra confirmacion formateada de un registro guardado."""
    tag = "NUEVO" if is_new else "ACTUALIZADO"
    yoy_str = f"{obj.yoy_change_pct:+.1f}%" if obj.yoy_change_pct is not None else "N/D"
    mills_str = str(obj.mills_operating) if obj.mills_operating is not None else "N/D"
    progress_str = f"{obj.season_progress_pct:.1f}%" if obj.season_progress_pct is not None else "N/D"
    fy_str = f"{obj.estimated_full_year_mt:.3f} Mt" if obj.estimated_full_year_mt is not None else "N/D"

    print(f"\n  [{tag}] ISMA Release — Temporada {obj.marketing_year}/{str(obj.marketing_year+1)[2:]}")
    print(f"    Fecha dato  : {obj.data_date}  (marketing year: {obj.marketing_year})")
    print(f"    Produccion  : {obj.cumulative_lakh_t:.2f} lakh t  ({obj.cumulative_mt:.3f} Mt)")
    print(f"    YoY         : {yoy_str}")
    print(f"    Progreso    : {progress_str}  -> Full-year est.: {fy_str}")
    print(f"    Fabricas    : {mills_str}")
    print(f"    Fuente      : {obj.source}")
    if obj.notes:
        print(f"    Notas       : {obj.notes}")
    print()


def cmd_list(session):
    """Muestra todos los releases en BD ordenados por fecha."""
    from models.market_data import IsmaRelease

    rows = (
        session.query(IsmaRelease)
        .order_by(IsmaRelease.marketing_year, IsmaRelease.data_date)
        .all()
    )

    if not rows:
        print("\n  (No hay datos ISMA en la base de datos)\n")
        return

    header = (
        f"{'Fecha':12}  {'My':4}  {'Lakh t':>8}  {'Mt':>7}  "
        f"{'YoY':>6}  {'Prog%':>6}  {'FY est Mt':>9}  {'Mills':>5}  Fuente"
    )
    print()
    print("  " + header)
    print("  " + "-" * len(header))
    for r in rows:
        yoy   = f"{r.yoy_change_pct:+.1f}%" if r.yoy_change_pct is not None else "  N/D"
        mills = str(r.mills_operating) if r.mills_operating is not None else "  N/D"
        prog  = f"{r.season_progress_pct:.1f}" if r.season_progress_pct is not None else " N/D"
        fy    = f"{r.estimated_full_year_mt:.3f}" if r.estimated_full_year_mt is not None else "    N/D"
        print(
            f"  {str(r.data_date):12}  {r.marketing_year:4}  "
            f"{float(r.cumulative_lakh_t):>8.2f}  {float(r.cumulative_mt):>7.3f}  "
            f"{yoy:>6}  {prog:>6}  {fy:>9}  {mills:>5}  {r.source}"
        )
    print(f"\n  Total: {len(rows)} registro(s)\n")


def cmd_seed(session):
    """Carga los datos historicos conocidos de la temporada 2025-26."""
    logger.info("Cargando seed data temporada 2025-26...")
    count_new = count_upd = 0

    for entry in SEED_DATA_2526:
        record = build_record(
            data_date  = entry["data_date"],
            lakh       = entry["cumulative_lakh_t"],
            mt         = None,
            yoy        = entry.get("yoy_change_pct"),
            mills      = entry.get("mills_operating"),
            mh         = None,
            up         = None,
            ka         = None,
            pub_date   = entry.get("pub_date"),
            source     = "manual_cli",
            notes      = entry.get("notes"),
        )
        obj, is_new = upsert_isma(session, record)
        print_record(obj, is_new)
        if is_new:
            count_new += 1
        else:
            count_upd += 1

    logger.info(
        "Seed completado: %d nuevo(s), %d actualizado(s).",
        count_new, count_upd,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Ingesta manual de datos ISMA (Indian Sugar & Bio-energy Manufacturers Association)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Modos de operacion
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true",
                      help="Muestra todos los releases en BD")
    mode.add_argument("--seed", action="store_true",
                      help="Carga datos historicos 2025-26 hardcodeados")

    # Datos del release
    parser.add_argument("--date", type=str, metavar="YYYY-MM-DD",
                        help="Fecha del dato ISMA (as on date)")
    parser.add_argument("--lakh", type=float,
                        help="Produccion acumulada en lakh tonnes")
    parser.add_argument("--mt", type=float,
                        help="Produccion acumulada en Mt (alternativa a --lakh)")
    parser.add_argument("--yoy", type=float,
                        help="Variacion YoY en porcentaje (ej. 7.0 = +7%%)")
    parser.add_argument("--mills", type=int,
                        help="Fabricas activas en esa fecha")
    parser.add_argument("--mh", type=float,
                        help="Maharashtra en lakh t (opcional)")
    parser.add_argument("--up", type=float,
                        help="Uttar Pradesh en lakh t (opcional)")
    parser.add_argument("--ka", type=float,
                        help="Karnataka en lakh t (opcional)")
    parser.add_argument("--pub-date", type=str, metavar="YYYY-MM-DD",
                        help="Fecha de publicacion del press release (opcional)")
    parser.add_argument("--source", type=str, default="manual_cli",
                        help="Fuente del dato (default: manual_cli)")
    parser.add_argument("--notes", type=str,
                        help="Notas adicionales (opcional)")

    args = parser.parse_args()

    from database import SessionLocal, create_all_tables
    create_all_tables()
    session = SessionLocal()

    try:
        if args.list:
            cmd_list(session)
            return

        if args.seed:
            cmd_seed(session)
            return

        # Modo ingesta de un registro
        if not args.date:
            parser.error("--date es obligatorio (o usa --list / --seed)")
        if args.lakh is None and args.mt is None:
            parser.error("Se requiere --lakh o --mt")

        data_date = date.fromisoformat(args.date)
        pub_date  = date.fromisoformat(args.pub_date) if args.pub_date else None

        record = build_record(
            data_date = data_date,
            lakh      = args.lakh,
            mt        = args.mt,
            yoy       = args.yoy,
            mills     = args.mills,
            mh        = args.mh,
            up        = args.up,
            ka        = args.ka,
            pub_date  = pub_date,
            source    = args.source,
            notes     = args.notes,
        )

        obj, is_new = upsert_isma(session, record)
        print_record(obj, is_new)

    except Exception as e:
        logger.error("Error fatal: %s", e, exc_info=True)
        session.rollback()
        sys.exit(1)
    finally:
        session.close()


if __name__ == "__main__":
    main()
