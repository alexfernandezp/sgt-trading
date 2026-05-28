"""
USDA FAS PSD — Production, Supply and Distribution (balance global azúcar).

Dos modos de acceso:
  1. API api.data.gov  (requiere USDA_API_KEY en .env — gratuita, registro en https://api.data.gov/signup/)
  2. Bulk Excel download (fallback automático sin key)

Commodity: Sugar, Centrifugal (código 0612000)
Frecuencia: mensual (publicación ~día 12 de cada mes con WASDE)
Unidad: valores en 1000 MT

Uso manual:
  py scripts/fetch_usda.py              # años actuales
  py scripts/fetch_usda.py --years 5   # backfill 5 años
"""
import io
import logging
import requests
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

_TIMEOUT   = 90
_HEADERS   = {"User-Agent": "SGTTrading/1.0 (research)"}

# Atributos clave por nombre (bulk Excel fallback)
_ATTR_NAMES = {
    "Beginning Stocks":           "beginning_stocks",
    "Production":                 "production",
    "Imports":                    "imports",
    "Total Supply":               "total_supply",
    "Domestic Consumption":       "dom_consumption",
    "Food Use Dom. Consumption":  "dom_consumption",
    "Total Domestic Consumption": "dom_consumption",
    "Exports":                    "exports",
    "Ending Stocks":              "ending_stocks",
}

# Atributos clave por ID (API V2 — inferidos de datos BR/2024 donde balance cuadra)
# Verificacion: beg(20)+prod(28)+imp(57)=supply(86); cons(126)+exp(88)+end(176)=supply(86)
_ATTR_IDS = {
    20:  "beginning_stocks",
    28:  "production",
    57:  "imports",
    86:  "total_supply",
    88:  "exports",
    126: "dom_consumption",
    176: "ending_stocks",
}

# Países que importan para el balance global de azúcar
_COUNTRIES = {"WB", "BR", "IN", "TH", "EU", "AU", "CN", "US", "PK", "MX"}


# ── Acceso API ─────────────────────────────────────────────────────────────────

def _fetch_api(commodity_code: str, api_key: str,
               years: Optional[list[int]] = None) -> list[dict]:
    """
    Descarga via API — una llamada por año con todos los países.
    Endpoint: /commodity/{code}/country/all/year/{year}
    """
    from config import USDA_PSD_BASE_URL

    records = []
    years_to_fetch = years or [date.today().year - 1, date.today().year]

    for year in years_to_fetch:
        hdrs = {**_HEADERS, "X-Api-Key": api_key}

        # 1. Balance mundial oficial (/world/) — sin double-counting
        url_world = f"{USDA_PSD_BASE_URL}/commodity/{commodity_code}/world/year/{year}"
        try:
            r = requests.get(url_world, headers=hdrs, timeout=_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                # Marcar como "WB" para distinguirlo de países individuales
                for rec in data:
                    rec["countryCode"] = "WB"
                    rec["isWorld"] = True
                records.extend(data)
                logger.info("USDA API world/%d: %d atributos", year, len(data))
        except Exception as e:
            logger.warning("USDA API world error %d: %s", year, e)

        # 2. Países clave individuales (/country/all/) — para breakdown por país
        url_all = f"{USDA_PSD_BASE_URL}/commodity/{commodity_code}/country/all/year/{year}"
        try:
            r = requests.get(url_all, headers=hdrs, timeout=_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                records.extend(data)
                logger.info("USDA API all countries/%d: %d registros", year, len(data))
            elif r.status_code == 404:
                logger.warning("USDA API: año %d sin datos", year)
            else:
                logger.warning("USDA API %d: HTTP %d", year, r.status_code)
        except Exception as e:
            logger.warning("USDA API error year %d: %s", year, e)

    return records


def _fetch_bulk_excel(commodity_code: str) -> list[dict]:
    """
    Fallback: descarga el archivo bulk Excel de USDA FAS PSD.
    No requiere API key. Contiene todo el histórico.
    """
    # URL oficial del bulk download de USDA PSD Online
    url = "https://apps.fas.usda.gov/psdonline/downloads/psd_sugar.xlsx"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=60)
        if r.status_code != 200:
            logger.error("USDA bulk download falló: HTTP %d", r.status_code)
            return []
    except Exception as e:
        logger.error("USDA bulk download error: %s", e)
        return []

    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []

        # Detectar encabezados (primera fila)
        headers = [str(h).strip() if h else "" for h in rows[0]]
        logger.info("USDA bulk Excel: %d filas, columnas: %s", len(rows) - 1, headers[:8])

        records = []
        for row in rows[1:]:
            if not any(row):
                continue
            rec = dict(zip(headers, row))
            records.append(rec)
        return records

    except ImportError:
        logger.error("openpyxl no instalado. Ejecutar: pip install openpyxl")
        return []
    except Exception as e:
        logger.error("USDA bulk Excel parse error: %s", e)
        return []


# ── Normalización de registros ─────────────────────────────────────────────────

def _normalize_api_record(rec: dict) -> Optional[dict]:
    """Convierte registro API V2 al formato interno.
    V2 usa attributeId (int) en lugar de attributeName.
    marketYear y month vienen como strings.
    """
    attr_id = rec.get("attributeId")
    try:
        attr_id = int(attr_id)
    except (TypeError, ValueError):
        return None
    if attr_id not in _ATTR_IDS:
        return None

    country_code = rec.get("countryCode", "")
    if not country_code:
        return None

    value = rec.get("value")
    if value is None:
        return None

    try:
        mkt_year = int(rec.get("marketYear", 0))
        pub_month = int(rec.get("month", 0))
    except (TypeError, ValueError):
        mkt_year = 0; pub_month = 0

    return {
        "commodity_code": str(rec.get("commodityCode", "")),
        "country_code":   country_code,
        "country_name":   "",   # V2 no incluye countryName
        "marketing_year": mkt_year,
        "pub_month":      pub_month,
        "attribute_id":   attr_id,
        "attribute_name": _ATTR_IDS[attr_id],
        "value_1000mt":   float(value),
    }


def _normalize_bulk_record(rec: dict, commodity_code: str) -> Optional[dict]:
    """Convierte fila de Excel bulk al formato interno."""
    # Columnas típicas del bulk Excel USDA: Commodity_Code, Country_Code,
    # Market_Year, Attribute_ID, Attribute_Description, Unit_ID, Value
    try:
        cc = str(rec.get("Commodity_Code", rec.get("commodityCode", ""))).strip()
        if cc != commodity_code:
            return None
        country_code = str(rec.get("Country_Code", rec.get("countryCode", ""))).strip()
        if country_code not in _COUNTRIES:
            return None
        attr_name = str(rec.get("Attribute_Description",
                                 rec.get("attributeName", ""))).strip()
        if attr_name not in _ATTR_NAMES:
            return None
        value = rec.get("Value", rec.get("value"))
        if value is None or str(value).strip() in ("", "None", "null"):
            return None
        mkt_year = rec.get("Market_Year", rec.get("marketYear", 0))
        attr_id  = rec.get("Attribute_ID", rec.get("attributeId", 0))
        return {
            "commodity_code": cc,
            "country_code":   country_code,
            "country_name":   str(rec.get("Country_Name",
                                           rec.get("countryName", ""))).strip(),
            "marketing_year": int(mkt_year) if mkt_year else 0,
            "pub_month":      0,   # bulk no tiene mes de publicación
            "attribute_id":   int(attr_id) if attr_id else 0,
            "attribute_name": attr_name,
            "value_1000mt":   float(value),
        }
    except (TypeError, ValueError):
        return None


# ── Upsert a BD ────────────────────────────────────────────────────────────────

def _upsert(session, records: list[dict]) -> int:
    """Inserta o actualiza registros en usda_psd. Retorna número de filas."""
    from models.market_data import UsdaPsdRecord
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    now = datetime.utcnow()
    count = 0
    for rec in records:
        if not rec or rec.get("marketing_year", 0) < 2000:
            continue
        stmt = pg_insert(UsdaPsdRecord).values(
            **rec,
            updated_at=now,
        ).on_conflict_do_update(
            index_elements=["commodity_code", "country_code", "marketing_year",
                             "attribute_id", "pub_month"],
            set_={"value_1000mt": rec["value_1000mt"], "updated_at": now},
        )
        try:
            session.execute(stmt)
            count += 1
        except Exception as e:
            logger.warning("USDA upsert error: %s", e)
            session.rollback()
            continue

    session.commit()
    return count


# ── Función principal ──────────────────────────────────────────────────────────

def fetch_usda_psd(session, years: Optional[list[int]] = None,
                   force_bulk: bool = False) -> dict:
    """
    Descarga y almacena datos USDA PSD para azúcar centrífugo.

    Prioridad:
      1. API api.data.gov si USDA_API_KEY está configurada
      2. Bulk Excel download (fallback automático)

    Args:
        years: lista de años a descargar (default: año actual y anterior)
        force_bulk: forzar descarga bulk aunque haya API key

    Returns dict con: rows_upserted, source, years_fetched, errors
    """
    from config import USDA_API_KEY, USDA_SUGAR_CODE
    from database import create_all_tables
    create_all_tables()

    result = {
        "rows_upserted": 0,
        "source": None,
        "years_fetched": years or [],
        "errors": [],
    }

    raw_records = []

    # Prioridad: clave propia → DEMO_KEY (limitada, 30 req/hora) → bulk Excel
    api_key = USDA_API_KEY or "DEMO_KEY"

    if not force_bulk:
        key_label = "API key configurada" if USDA_API_KEY else "DEMO_KEY (limitado — set USDA_API_KEY en .env)"
        logger.info("USDA PSD: usando API api.data.gov (%s)", key_label)
        raw_records = _fetch_api(USDA_SUGAR_CODE, api_key, years)
        result["source"] = "api" if USDA_API_KEY else "api_demo"
        if not raw_records:
            logger.warning("USDA API sin registros — intentando bulk Excel")
            raw_records = _fetch_bulk_excel(USDA_SUGAR_CODE)
            result["source"] = "bulk_excel_fallback"
    else:
        logger.info("USDA PSD: forzando bulk Excel")
        raw_records = _fetch_bulk_excel(USDA_SUGAR_CODE)
        result["source"] = "bulk_excel"

    if not raw_records:
        result["errors"].append("Sin datos descargados de USDA")
        return result

    # Normalizar según la fuente
    if result["source"] in ("api",):
        normalized = [_normalize_api_record(r) for r in raw_records]
    else:
        normalized = [_normalize_bulk_record(r, USDA_SUGAR_CODE) for r in raw_records]

    normalized = [r for r in normalized if r is not None]
    logger.info("USDA PSD: %d registros normalizados de %d raw", len(normalized), len(raw_records))

    if not normalized:
        result["errors"].append("0 registros normalizados — verificar formato de respuesta")
        return result

    result["rows_upserted"] = _upsert(session, normalized)
    result["years_fetched"] = sorted(set(r["marketing_year"] for r in normalized))
    logger.info("USDA PSD: %d filas upserted, años: %s",
                result["rows_upserted"], result["years_fetched"])
    return result


# ── Consulta ───────────────────────────────────────────────────────────────────

def get_world_balance(session, marketing_year: Optional[int] = None) -> dict:
    """
    Calcula el balance mundial sumando todos los países en BD.
    No depende de un código 'WB' — suma directa de datos individuales.

    Para evitar doble-conteo, usa solo atributos de flujo real:
      production, dom_consumption, exports, ending_stocks, beginning_stocks
    (excluye total_supply que ya es derivado)

    Returns dict con: marketing_year, production_mt, consumption_mt,
                      ending_stocks_mt, beginning_stocks_mt,
                      stocks_to_use_pct, exports_mt
    """
    from models.market_data import UsdaPsdRecord
    from sqlalchemy import select, func

    if marketing_year is None:
        row = session.execute(
            select(func.max(UsdaPsdRecord.marketing_year))
            .where(UsdaPsdRecord.country_code == "WB")
        ).scalar()
        if not row:
            return {}
        marketing_year = row

    # Usar el balance mundial oficial (country_code="WB") — endpoint /world/
    # Tomar el pub_month más reciente (última publicación WASDE)
    rows = session.execute(
        select(UsdaPsdRecord.attribute_name, UsdaPsdRecord.value_1000mt)
        .where(
            UsdaPsdRecord.country_code == "WB",
            UsdaPsdRecord.marketing_year == marketing_year,
        )
        .order_by(UsdaPsdRecord.pub_month.desc())
    ).fetchall()

    if not rows:
        return {}

    _VALID_KEYS = {"beginning_stocks", "production", "imports", "total_supply",
                   "dom_consumption", "exports", "ending_stocks"}
    seen = set()
    attr_map = {}
    for attr_name, value in rows:
        if attr_name in _VALID_KEYS and attr_name not in seen and value is not None:
            attr_map[attr_name] = float(value)
            seen.add(attr_name)

    prod  = attr_map.get("production")
    cons  = attr_map.get("dom_consumption")
    end_s = attr_map.get("ending_stocks")
    beg_s = attr_map.get("beginning_stocks")
    exp   = attr_map.get("exports")

    stu = round(end_s / cons * 100, 1) if (end_s and cons and cons > 0) else None

    return {
        "marketing_year":      marketing_year,
        "production_mt":       round(prod  / 1000, 2) if prod  else None,
        "consumption_mt":      round(cons  / 1000, 2) if cons  else None,
        "ending_stocks_mt":    round(end_s / 1000, 2) if end_s else None,
        "beginning_stocks_mt": round(beg_s / 1000, 2) if beg_s else None,
        "exports_mt":          round(exp   / 1000, 2) if exp   else None,
        "stocks_to_use_pct":   stu,
    }


def get_country_production(session, country_code: str,
                            n_years: int = 3) -> list[dict]:
    """Retorna producción histórica de un país (últimos n_years)."""
    from models.market_data import UsdaPsdRecord
    from sqlalchemy import select, func

    max_year = session.execute(
        select(func.max(UsdaPsdRecord.marketing_year))
        .where(UsdaPsdRecord.country_code == country_code)
    ).scalar()
    if not max_year:
        return []

    rows = session.execute(
        select(UsdaPsdRecord.marketing_year, UsdaPsdRecord.value_1000mt)
        .where(
            UsdaPsdRecord.country_code == country_code,
            UsdaPsdRecord.attribute_name == "production",
            UsdaPsdRecord.marketing_year >= max_year - n_years + 1,
        )
        .order_by(UsdaPsdRecord.marketing_year, UsdaPsdRecord.pub_month.desc())
    ).fetchall()

    seen = {}
    for year, value in rows:
        if year not in seen and value is not None:
            seen[year] = round(float(value) / 1000, 2)

    return [{"year": y, "production_mt": v} for y, v in sorted(seen.items())]
