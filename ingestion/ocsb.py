"""
OCSB Thailand Sugar Production Scraper.

Fuente: OCSB Open Data Portal — Office of the Cane and Sugar Board
  Portal: https://opendata.ocsb.go.th/en/
  CKAN API: https://catalog.ocsb.go.th/api/3/action/

Datos disponibles:
  - Produccion de azucar por planta (fabrica) y por temporada
  - Cana molida (toneladas), CCS%, azucar producida
  - Temporada Nov-Apr (Thai crushing season)
  - Año en calendario budista (BE = CE + 543)

Metodologia:
  1. Query CKAN API para encontrar el dataset de produccion de azucar
  2. Descargar CSV de recursos
  3. Agregar por temporada → total Mt
  4. Convertir año budista → gregoriano
"""
import io
import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

_TIMEOUT = 30
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SGTTrading/1.0; +https://github.com/alexfernandezp/sgt-trading)",
    "Accept": "application/json, text/csv, */*",
}

CKAN_BASE = "https://catalog.ocsb.go.th/api/3/action"

# Calendario budista: BE = CE + 543
BUDDHIST_OFFSET = 543

# Produccion Tailandia plausible: 8-16 Mt
_TH_PROD_MIN = 8.0
_TH_PROD_MAX = 16.0


@dataclass
class OcsbData:
    total_production_mt: float
    cane_crushed_mt: Optional[float]  # cana molida en toneladas metricas
    season_year: int               # año CE de inicio de temporada (Nov)
    data_date: date                # fecha de descarga / ultima actualizacion
    source_url: str
    confidence: float


# ── CKAN API helpers ───────────────────────────────────────────────────────────

def _ckan_get(endpoint: str, params: dict) -> Optional[dict]:
    try:
        import httpx
        url = f"{CKAN_BASE}/{endpoint}"
        r = httpx.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code == 200:
            return r.json()
        logger.debug("CKAN %s: HTTP %d", endpoint, r.status_code)
    except Exception as e:
        logger.debug("CKAN API error: %s", e)
    return None


def _find_sugar_production_resource() -> Optional[dict]:
    """
    Busca el recurso CSV de produccion de azucar en el portal OCSB.
    Retorna dict con {url, name, package_id} del recurso mas relevante.
    """
    search_terms = [
        "sugar production",
        "ผลิตน้ำตาล",     # "sugar production" en tailandes
        "cane sugar",
        "crushing",
    ]

    for term in search_terms:
        resp = _ckan_get("package_search", {"q": term, "rows": 10})
        if not resp or not resp.get("success"):
            continue

        packages = resp.get("result", {}).get("results", [])
        for pkg in packages:
            for resource in pkg.get("resources", []):
                fmt = resource.get("format", "").upper()
                name = resource.get("name", "").lower()
                url  = resource.get("url", "")

                if fmt in ("CSV", "XLSX", "XLS") and url:
                    # Priorizar datasets de produccion de azucar
                    if any(kw in name for kw in ["sugar", "production", "crushing",
                                                  "น้ำตาล", "ผลิต"]):
                        return {
                            "url": url,
                            "name": resource.get("name", ""),
                            "format": fmt,
                            "package_id": pkg.get("id", ""),
                            "package_title": pkg.get("title", ""),
                        }

    return None


def _buddhist_to_ce(year_val) -> Optional[int]:
    """Convierte año budista a gregoriano. Si ya es gregoriano (>2000), no convierte.
    Maneja formato "2565/66" (OCSB) tomando solo el primer numero.
    """
    try:
        year_str = str(year_val).strip()
        # Formato "2565/66" → tomar la parte antes de la barra
        if "/" in year_str:
            year_str = year_str.split("/")[0].strip()
        y = int(year_str)
        if y > 2500:   # claramente budista (2567 = 2024)
            return y - BUDDHIST_OFFSET
        if y > 1900:   # ya es gregoriano
            return y
        return None
    except (ValueError, TypeError):
        return None


def _parse_csv_production(csv_text: str, target_ce_year: Optional[int] = None) -> dict:
    """
    Parsea CSV de OCSB para extraer produccion total de azucar.

    Columnas esperadas (pueden variar en nombre):
      - year/season: año de temporada (budista o gregoriano)
      - sugar_produced / sugar_quantity / factory_sugar: azucar producida (toneladas)
      - cane_crushed / cane_quantity: cana molida (toneladas)

    Retorna dict: {season_year: {"sugar_mt": float, "cane_mt": float}}
    """
    import csv

    results: dict[int, dict] = {}

    try:
        reader = csv.DictReader(io.StringIO(csv_text))
        # Eliminar BOM (﻿) del primer campo y normalizar a minusculas
        raw_fields = reader.fieldnames or []
        fieldnames = [f.lstrip('﻿').lower().strip() for f in raw_fields]

        # Identificar columnas relevantes.
        # OCSB real: ProductionYear, Sugartotal, SugarofCane (por provincia)
        # Orden de keywords: mas especifico primero para evitar match prematuro
        year_col   = _find_col(fieldnames, ["productionyear", "year", "season", "crop_year", "ปี", "ฤดู"])
        sugar_col  = _find_col(fieldnames, ["sugartotal", "sugar_total", "total_sugar",
                                             "sugar", "น้ำตาล", "production", "quantity"])
        cane_col   = _find_col(fieldnames, ["cane", "อ้อย", "crushed"])

        logger.debug("OCSB CSV campos: %s", fieldnames)
        logger.debug("OCSB CSV cols detectadas: year=%s  sugar=%s  cane=%s",
                     year_col, sugar_col, cane_col)

        if year_col is None or sugar_col is None:
            logger.warning("OCSB CSV: no se encontraron columnas year/sugar en %s", fieldnames)
            return {}

        # Mapear nombres normalizados → originales para acceso correcto
        field_map = {fn: orig for fn, orig in zip(fieldnames, raw_fields)}
        year_col_orig  = field_map.get(year_col)
        sugar_col_orig = field_map.get(sugar_col)
        cane_col_orig  = field_map.get(cane_col)

        reader2 = csv.DictReader(io.StringIO(csv_text))
        # Reparchar nombres de campos en el reader2 para que coincidan con originales
        reader2.fieldnames = raw_fields

        for row in reader2:
            try:
                year_raw  = row.get(year_col_orig, "")
                sugar_raw = row.get(sugar_col_orig, "0") if sugar_col_orig else "0"
                cane_raw  = row.get(cane_col_orig, "0")  if cane_col_orig  else "0"

                ce_year = _buddhist_to_ce(year_raw)
                if ce_year is None:
                    continue

                # Filtrar por año objetivo si se especifica
                if target_ce_year and abs(ce_year - target_ce_year) > 1:
                    continue

                sugar_t = _safe_float(sugar_raw)
                cane_t  = _safe_float(cane_raw)

                if sugar_t is None:
                    continue

                if ce_year not in results:
                    results[ce_year] = {"sugar_t": 0.0, "cane_t": 0.0}

                results[ce_year]["sugar_t"] += sugar_t
                results[ce_year]["cane_t"]  += (cane_t or 0.0)

            except Exception:
                continue

    except Exception as e:
        logger.warning("OCSB CSV parse error: %s", e)

    # OCSB CSV: Sugartotal en quintales (100 kg), SugarofCane en ratio (no es cana)
    # Conversion: quintales * 100 kg / quintal / 1e9 kg/Mt = / 10_000_000
    out = {}
    for yr, vals in results.items():
        sugar_mt = vals["sugar_t"] / 10_000_000
        cane_mt  = 0.0  # SugarofCane es ratio (kg azucar / tonne cana), no cana molida
        if _TH_PROD_MIN <= sugar_mt <= _TH_PROD_MAX:
            out[yr] = {"sugar_mt": round(sugar_mt, 3), "cane_mt": round(cane_mt, 3)}

    return out


def _find_col(fieldnames: list[str], keywords: list[str]) -> Optional[str]:
    """Encuentra el primer campo que contenga alguna de las keywords."""
    for kw in keywords:
        for f in fieldnames:
            if kw in f:
                return f
    return None


def _safe_float(s) -> Optional[float]:
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _download_csv(url: str) -> Optional[str]:
    try:
        import httpx
        r = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code == 200:
            return r.text
    except Exception as e:
        logger.debug("OCSB CSV download %s: %s", url, e)
    return None


# ── Fuentes alternativas ───────────────────────────────────────────────────────

def _fetch_tsmc_production() -> Optional[OcsbData]:
    """
    Intenta obtener datos de Thai Sugar Millers Corporation (TSMC).
    URL: www.thaisugarmillers.com (publicaciones mensuales Nov-Abr)
    """
    urls = [
        "https://www.thaisugarmillers.com/news.html",
        "https://www.thaisugarmillers.com/sugar-statistics",
    ]
    for url in urls:
        try:
            import httpx
            r = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
            if r.status_code != 200:
                continue

            text = r.text
            # Buscar "million tonnes" o "Mt" en contexto de produccion
            mt_re = re.compile(
                r"(\d+(?:\.\d+)?)\s*(?:million\s+(?:tonne|ton)|Mt|MMT)",
                re.IGNORECASE
            )
            matches = [float(m.group(1)) for m in mt_re.finditer(text)]
            matches = [v for v in matches if _TH_PROD_MIN <= v <= _TH_PROD_MAX]
            if matches:
                prod_mt = max(matches)
                logger.info("TSMC: produccion Tailandia %.2f Mt (fuente %s)", prod_mt, url)
                return OcsbData(
                    total_production_mt=prod_mt,
                    cane_crushed_mt=None,
                    season_year=date.today().year if date.today().month >= 10 else date.today().year - 1,
                    data_date=date.today(),
                    source_url=url,
                    confidence=0.82,
                )
        except Exception as e:
            logger.debug("TSMC fetch: %s", e)

    return None


# ── Interfaz publica ───────────────────────────────────────────────────────────

def fetch_ocsb_latest(target_ce_year: Optional[int] = None) -> Optional[OcsbData]:
    """
    Obtiene produccion de azucar de Tailandia via OCSB CKAN API.

    Si target_ce_year es None, usa el año de temporada mas reciente
    en los datos descargados.

    Retorna None si no se pueden obtener datos.
    """
    resource = _find_sugar_production_resource()
    if not resource:
        logger.info("OCSB: no se encontro recurso CSV en CKAN API")
        return _fetch_tsmc_production()

    csv_text = _download_csv(resource["url"])
    if not csv_text:
        logger.info("OCSB: CSV descargado vacio o con error")
        return _fetch_tsmc_production()

    prod_by_year = _parse_csv_production(csv_text, target_ce_year)
    if not prod_by_year:
        logger.info("OCSB: CSV parseado pero sin produccion valida")
        return _fetch_tsmc_production()

    # Tomar el año mas reciente
    latest_year = max(prod_by_year.keys())
    data = prod_by_year[latest_year]

    # Confidence: datos OCSB oficiales — alta confianza
    # Reducir si son del año pasado (datos viejos del final de temporada)
    # Temporada tailandesa: Nov-Abr. Latest_year=X significa temporada X/X+1.
    # data_date = fin de temporada aproximado (30 abr del año siguiente)
    season_end = date(latest_year + 1, 4, 30)
    age_years = date.today().year - latest_year - (1 if date.today().month >= 10 else 0)

    if age_years <= 0:
        conf = 0.88   # datos de temporada en curso o recien cerrada
    elif age_years == 1:
        conf = 0.85   # datos finales de temporada anterior
    else:
        conf = 0.70   # datos viejos (>1 temporada atras)

    logger.info(
        "OCSB: Tailandia %d -> %.3f Mt azucar (age=%d yr, conf=%.2f)",
        latest_year, data["sugar_mt"], age_years, conf,
    )

    return OcsbData(
        total_production_mt=data["sugar_mt"],
        cane_crushed_mt=data.get("cane_mt"),
        season_year=latest_year,
        data_date=season_end,        # Fecha real del final de temporada
        source_url=resource["url"],
        confidence=conf,
    )


def thailand_production_estimate(
        target_ce_year: Optional[int] = None,
        session=None,
) -> tuple[Optional[float], str, float]:
    """
    Devuelve estimacion de produccion de azucar de Tailandia.

    Returns:
        (production_mt, data_source_str, confidence)
        production_mt = None si no hay datos → el modelo usa USDA sin override
    """
    data = fetch_ocsb_latest(target_ce_year)
    if data is None:
        return None, "usda_baseline", 0.80

    age_days = (date.today() - data.data_date).days

    if age_days <= 60:
        return data.total_production_mt, "ocsb", data.confidence
    elif age_days <= 120:
        return data.total_production_mt, "ocsb_stale", 0.75
    else:
        return None, "ocsb_too_old", 0.55
