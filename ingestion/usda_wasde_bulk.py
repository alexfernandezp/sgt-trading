"""
USDA FAS PSD — Bulk CSV con revisiones mensuales WASDE.

A diferencia de `ingestion/usda_psd.py` (que solo guarda el snapshot final
de la API por país/año), este módulo descarga el ZIP histórico completo de
USDA FAS y extrae TODAS las revisiones mensuales (campo `Month` = mes WASDE).

URL fuente:
    https://apps.fas.usda.gov/psdonline/downloads/psd_alldata_CSV.zip

El CSV dentro del ZIP tiene estas columnas:
    Country_Code, Country_Name, Commodity_Code, Commodity_Description,
    Market_Year, Month, Attribute_ID, Attribute_Description, Unit_ID,
    Unit_Description, Value

    - Month  = 1-12 (mes de publicación del WASDE en el que se publicó esa revisión)
    - Value  = miles de MT (1000 MT)

Uso típico:
    from ingestion.usda_wasde_bulk import download_bulk_csv, load_wasde_monthly
    csv_path = download_bulk_csv()
    load_wasde_monthly(session)

Para consultar la revisión más reciente de un país/año/atributo:
    val = get_latest_wasde_country(session, "IN", 2025, "production")   # → MT

Para ver la serie histórica de revisiones:
    hist = get_wasde_history(session, "IN", 2025, "production")
    # [{"month": 1, "year": 2025, "value_mt": 28.5}, ...]
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────────────────

_BULK_URL   = "https://apps.fas.usda.gov/psdonline/downloads/psd_alldata_CSV.zip"
_TIMEOUT    = 120          # segundos — el ZIP pesa ~50 MB
_MAX_AGE_D  = 30           # días antes de re-descargar
_HEADERS    = {"User-Agent": "SGTTrading/1.0 (research)"}

# Directorio local de caché
_DATA_DIR   = Path(__file__).resolve().parent.parent / "data" / "usda_bulk"

# Nombre esperado del CSV dentro del ZIP (USDA lo nombra siempre igual)
_CSV_NAME   = "psd_alldata.csv"


# ── Utilidades ────────────────────────────────────────────────────────────────

def _attr_ids() -> dict[int, str]:
    """
    Lee _ATTR_IDS de usda_psd.py en tiempo de ejecución.
    Evita hardcodear IDs; si USDA cambia la asignación basta actualizar un
    solo fichero.
    """
    from ingestion.usda_psd import _ATTR_IDS  # type: ignore[import]
    return _ATTR_IDS


def _relevant_countries() -> set[str]:
    """
    Retorna el conjunto de códigos de país relevantes definido en config.py.
    Incluye el pseudo-país "WB" (World Balance) que USDA publica como "52".
    El CSV de USDA usa "52" para el mundo — lo mapeamos a "WB" al normalizar.
    """
    from config import USDA_COUNTRIES  # type: ignore[import]
    codes = set(USDA_COUNTRIES.keys())
    return codes


# País "World" en el CSV bulk de USDA (distinto al código "WB" que usamos)
_USDA_WORLD_CODE = "52"


# ── 1. Descarga ───────────────────────────────────────────────────────────────

def download_bulk_csv(force: bool = False) -> Path:
    """
    Descarga `psd_alldata_CSV.zip` de USDA FAS y descomprime el CSV interno.

    Cachea en `data/usda_bulk/`. Si el CSV ya existe y tiene menos de
    ``_MAX_AGE_D`` días de antigüedad y ``force=False``, devuelve el path
    sin re-descargar.

    Args:
        force: Si ``True``, re-descarga aunque el caché sea reciente.

    Returns:
        Path absoluta al CSV descomprimido.

    Raises:
        RuntimeError: Si la descarga o descompresión fallan.
    """
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = _DATA_DIR / _CSV_NAME
    zip_path = _DATA_DIR / "psd_alldata_CSV.zip"

    # Verificar caché
    if not force and csv_path.exists():
        age = datetime.utcnow() - datetime.utcfromtimestamp(csv_path.stat().st_mtime)
        if age < timedelta(days=_MAX_AGE_D):
            logger.info(
                "USDA bulk CSV en caché (%.1f días de antigüedad < %d). "
                "Usar force=True para re-descargar. Path: %s",
                age.total_seconds() / 86400,
                _MAX_AGE_D,
                csv_path,
            )
            return csv_path
        else:
            logger.info(
                "USDA bulk CSV caché expirado (%.1f días). Re-descargando…",
                age.total_seconds() / 86400,
            )

    # Descargar ZIP
    logger.info("Descargando USDA PSD bulk ZIP desde %s …", _BULK_URL)
    try:
        with httpx.Client(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True) as client:
            with client.stream("GET", _BULK_URL) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length", 0))
                downloaded = 0
                with zip_path.open("wb") as fh:
                    for chunk in response.iter_bytes(chunk_size=1 << 20):  # 1 MB chunks
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = downloaded / total * 100
                            logger.debug("Descarga: %.1f %% (%d / %d bytes)", pct, downloaded, total)
        logger.info("ZIP descargado: %s (%.1f MB)", zip_path, zip_path.stat().st_size / 1e6)
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"USDA bulk download HTTP error: {exc.response.status_code}") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"USDA bulk download request error: {exc}") from exc

    # Descomprimir
    logger.info("Descomprimiendo ZIP…")
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            logger.debug("Archivos en ZIP: %s", names)
            # Buscar el CSV (puede tener nombre exacto o variación)
            target = None
            for name in names:
                if name.lower().endswith(".csv"):
                    target = name
                    break
            if target is None:
                raise RuntimeError(f"No se encontró CSV dentro del ZIP. Archivos: {names}")
            zf.extract(target, _DATA_DIR)
            extracted = _DATA_DIR / target
            # Renombrar al nombre canónico si difiere
            if extracted != csv_path:
                extracted.rename(csv_path)
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"ZIP corrupto o descarga incompleta: {exc}") from exc

    logger.info("CSV extraído: %s (%.1f MB)", csv_path, csv_path.stat().st_size / 1e6)
    return csv_path


# ── 2. Carga a BD ─────────────────────────────────────────────────────────────

def load_wasde_monthly(session, commodity_code: str = "0612000") -> dict:
    """
    Lee el CSV bulk de USDA FAS, filtra por commodity y hace upsert en `usda_psd`.

    Solo carga los países definidos en ``config.USDA_COUNTRIES`` (más "WB" que
    se mapea desde el código USDA "52").

    Args:
        session:        SQLAlchemy session activa.
        commodity_code: Código USDA del commodity (default: azúcar centrífugo).

    Returns:
        Dict con ``rows_read``, ``rows_filtered``, ``rows_upserted``, ``errors``.
    """
    from database import create_all_tables  # type: ignore[import]
    create_all_tables()

    csv_path = download_bulk_csv()
    attr_ids = _attr_ids()
    relevant = _relevant_countries()

    result: dict = {
        "rows_read":     0,
        "rows_filtered": 0,
        "rows_upserted": 0,
        "errors":        [],
    }

    normalized: list[dict] = []

    logger.info("Leyendo CSV USDA bulk: %s", csv_path)
    try:
        with csv_path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                result["rows_read"] += 1

                # Filtrar por commodity
                cc = row.get("Commodity_Code", "").strip()
                if cc != commodity_code:
                    continue

                # Normalizar país (USDA usa "52" para mundo)
                country_raw = row.get("Country_Code", "").strip()
                country_code_norm = "WB" if country_raw == _USDA_WORLD_CODE else country_raw

                if country_code_norm not in relevant:
                    logger.debug(
                        "País ignorado: %s (%s) — no está en USDA_COUNTRIES",
                        country_raw, row.get("Country_Name", ""),
                    )
                    continue

                # Filtrar por attribute_id
                try:
                    attr_id = int(row.get("Attribute_ID", "").strip())
                except ValueError:
                    continue
                if attr_id not in attr_ids:
                    continue

                # Parsear campos numéricos
                try:
                    mkt_year  = int(row.get("Market_Year", "").strip())
                    pub_month = int(row.get("Month", "0").strip())
                    value_raw = row.get("Value", "").strip()
                    if not value_raw:
                        continue
                    value = float(value_raw)
                except (ValueError, TypeError) as exc:
                    logger.debug("Fila inválida: %s — %s", row, exc)
                    continue

                if mkt_year < 2000:
                    continue

                normalized.append({
                    "commodity_code": cc,
                    "country_code":   country_code_norm,
                    "country_name":   row.get("Country_Name", "").strip(),
                    "marketing_year": mkt_year,
                    "pub_month":      pub_month,
                    "attribute_id":   attr_id,
                    "attribute_name": attr_ids[attr_id],
                    "value_1000mt":   value,
                })
                result["rows_filtered"] += 1

                if result["rows_filtered"] % 50_000 == 0:
                    logger.info(
                        "  Leídas %d filas válidas de %d totales…",
                        result["rows_filtered"], result["rows_read"],
                    )

    except (OSError, csv.Error) as exc:
        msg = f"Error leyendo CSV: {exc}"
        logger.error(msg)
        result["errors"].append(msg)
        return result

    logger.info(
        "CSV procesado: %d filas totales, %d relevantes para upsert.",
        result["rows_read"], result["rows_filtered"],
    )

    if not normalized:
        result["errors"].append("0 filas normalizadas — verificar commodity_code y USDA_COUNTRIES")
        return result

    result["rows_upserted"] = _upsert_bulk(session, normalized)
    logger.info(
        "USDA WASDE bulk: %d filas insertadas/actualizadas en usda_psd.",
        result["rows_upserted"],
    )
    return result


def _upsert_bulk(session, records: list[dict]) -> int:
    """
    Hace upsert de los registros en la tabla `usda_psd`.

    Utiliza `INSERT … ON CONFLICT DO UPDATE` de PostgreSQL en lotes de 1000
    filas para no saturar la conexión en una sola transacción.

    Returns:
        Número de filas procesadas (no necesariamente todas "nuevas").
    """
    from models.market_data import UsdaPsdRecord  # type: ignore[import]
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    now   = datetime.utcnow()
    count = 0
    batch_size = 1000

    for i in range(0, len(records), batch_size):
        batch = records[i : i + batch_size]
        try:
            stmt = pg_insert(UsdaPsdRecord).values(
                [
                    {**rec, "updated_at": now, "created_at": now}
                    for rec in batch
                ]
            ).on_conflict_do_update(
                index_elements=[
                    "commodity_code", "country_code", "marketing_year",
                    "attribute_id", "pub_month",
                ],
                set_={
                    "value_1000mt": pg_insert(UsdaPsdRecord).excluded.value_1000mt,
                    "country_name": pg_insert(UsdaPsdRecord).excluded.country_name,
                    "attribute_name": pg_insert(UsdaPsdRecord).excluded.attribute_name,
                    "updated_at": now,
                },
            )
            session.execute(stmt)
            session.commit()
            count += len(batch)
            logger.debug("Upsert lote %d/%d (%d filas)", i // batch_size + 1,
                         (len(records) - 1) // batch_size + 1, len(batch))
        except Exception as exc:
            logger.warning("Error en upsert lote %d: %s", i // batch_size + 1, exc)
            session.rollback()
            # Intentar fila a fila como fallback
            for rec in batch:
                try:
                    s = pg_insert(UsdaPsdRecord).values(
                        **rec, updated_at=now
                    ).on_conflict_do_update(
                        index_elements=[
                            "commodity_code", "country_code", "marketing_year",
                            "attribute_id", "pub_month",
                        ],
                        set_={
                            "value_1000mt": rec["value_1000mt"],
                            "country_name": rec.get("country_name", ""),
                            "attribute_name": rec.get("attribute_name", ""),
                            "updated_at": now,
                        },
                    )
                    session.execute(s)
                    count += 1
                except Exception as row_exc:
                    logger.debug("Fila ignorada: %s — %s", rec, row_exc)
            session.commit()

    return count


# ── 3. Consulta — snapshot más reciente ───────────────────────────────────────

def get_latest_wasde_country(
    session,
    country_code: str,
    marketing_year: int,
    attribute: str,
) -> Optional[float]:
    """
    Devuelve el valor más reciente (pub_month más alto) publicado por USDA WASDE
    para un (country, marketing_year, attribute).

    Args:
        session:        SQLAlchemy session activa.
        country_code:   Ej. "IN", "BR", "WB", "TH".
        marketing_year: Año inicio de temporada (ej. 2025 → campaña 2025/26).
        attribute:      Nombre interno del atributo: "production", "dom_consumption",
                        "ending_stocks", "beginning_stocks", "exports", "imports",
                        "total_supply".

    Returns:
        Valor en MT (millones de toneladas métricas), o ``None`` si no hay datos.
    """
    from models.market_data import UsdaPsdRecord  # type: ignore[import]
    from sqlalchemy import select

    row = session.execute(
        select(UsdaPsdRecord.value_1000mt, UsdaPsdRecord.pub_month)
        .where(
            UsdaPsdRecord.country_code   == country_code,
            UsdaPsdRecord.marketing_year == marketing_year,
            UsdaPsdRecord.attribute_name == attribute,
        )
        .order_by(UsdaPsdRecord.pub_month.desc())
        .limit(1)
    ).first()

    if row is None or row[0] is None:
        logger.debug(
            "get_latest_wasde_country: sin datos para (%s, %d, %s)",
            country_code, marketing_year, attribute,
        )
        return None

    value_mt = float(row[0]) / 1000.0
    logger.debug(
        "get_latest_wasde_country: %s / %d / %s → %.3f Mt (pub_month=%d)",
        country_code, marketing_year, attribute, value_mt, row[1],
    )
    return value_mt


# ── 4. Consulta — serie de revisiones mensuales ───────────────────────────────

def get_wasde_history(
    session,
    country_code: str,
    marketing_year: int,
    attribute: str,
) -> list[dict]:
    """
    Retorna la serie temporal de revisiones mensuales WASDE para un
    (country, marketing_year, attribute).

    Útil para el WeightDriftTracker (¿cuánto revisó USDA a lo largo del año?)
    y para analizar el patrón de sorpresas WASDE.

    Args:
        session:        SQLAlchemy session activa.
        country_code:   Ej. "IN", "BR", "WB".
        marketing_year: Año inicio de temporada.
        attribute:      Nombre interno: "production", "ending_stocks", etc.

    Returns:
        Lista de dicts ordenada por ``pub_month`` ascendente::

            [
                {"month": 5,  "marketing_year": 2025, "value_mt": 28.500},
                {"month": 6,  "marketing_year": 2025, "value_mt": 28.200},
                {"month": 12, "marketing_year": 2025, "value_mt": 27.900},
            ]

        Retorna lista vacía si no hay datos.
    """
    from models.market_data import UsdaPsdRecord  # type: ignore[import]
    from sqlalchemy import select

    rows = session.execute(
        select(UsdaPsdRecord.pub_month, UsdaPsdRecord.value_1000mt)
        .where(
            UsdaPsdRecord.country_code   == country_code,
            UsdaPsdRecord.marketing_year == marketing_year,
            UsdaPsdRecord.attribute_name == attribute,
            UsdaPsdRecord.pub_month      > 0,   # excluir filas sin mes de publicación
        )
        .order_by(UsdaPsdRecord.pub_month.asc())
    ).fetchall()

    if not rows:
        logger.debug(
            "get_wasde_history: sin datos para (%s, %d, %s)",
            country_code, marketing_year, attribute,
        )
        return []

    history = [
        {
            "month":          int(pub_month),
            "marketing_year": marketing_year,
            "value_mt":       round(float(val) / 1000.0, 3) if val is not None else None,
        }
        for pub_month, val in rows
    ]

    logger.debug(
        "get_wasde_history: %s / %d / %s → %d revisiones (meses %s…%s)",
        country_code, marketing_year, attribute, len(history),
        history[0]["month"], history[-1]["month"],
    )
    return history
