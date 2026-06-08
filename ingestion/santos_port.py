"""
Scraper del ship tracker del Puerto de Santos.

Fuente real (REST/JSON, WCF self-hosted):
  http://aplicacoes.portodesantos.com.br:9104/siap/servicos/atracacao/siteweb/

Endpoints activos:
  listarnaviosatracados   → barcos atracados (page='berthed')
  listarnaviosprogramados → barcos programados a atracar (page='scheduled')

Páginas anteriores en HTML (portodesantos.com.br/en/ship-tracker/) quedaron
inservibles: el WordPress devuelve cascarón vacío — el navegador rellenaba
las tablas vía AJAX al endpoint REST. Migración a JSON directo.

Endpoint "expected" (barcos en camino sin slot asignado) no está expuesto:
todas las variantes razonables devuelven 405. Se omite — page='expected'
no se escribirá. Downstream (santos_signal A5) degrada limpio porque la
ventana histórica seguirá teniendo expected legacy hasta agotarse, y
después usará solo berthed + scheduled (pesos rebalancean implícitamente
por z-score sobre serie nueva).

Filtro: solo barcos con Mercadoria que contenga ACUCAR o SUGAR.
"""
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests
import urllib3
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from models import SantosPortSnapshot
from services.data_quality import (
    check_or_log,
    parse_log_warning,
    validate_freshness,
    validate_range,
)

logger = logging.getLogger(__name__)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# BUSINESS_LOGIC §4 — line-up del puerto se publica en near-real-time; gaps >48h
# implican scraping roto, no rezago natural.
SANTOS_MAX_AGE_DAYS = 2

API_BASE = "http://aplicacoes.portodesantos.com.br:9104/siap/servicos/atracacao/siteweb"
ENDPOINTS = {
    "berthed":   f"{API_BASE}/listarnaviosatracados",
    "scheduled": f"{API_BASE}/listarnaviosprogramados",
}

SUGAR_RE = re.compile(r"AC[UÚ]CAR|SUGAR", re.IGNORECASE)
HEADERS  = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# Patrón de fecha .NET WCF: /Date(1780455600000-0300)/
DOTNET_DATE_RE = re.compile(r"/Date\((-?\d+)([+-]\d{4})?\)/")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_json(url: str, retries: int = 3) -> Optional[dict]:
    """GET JSON con reintentos + backoff exponencial. Devuelve dict o None."""
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30, verify=False)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.warning("GET %s intento %d: %s", url, attempt + 1, e)
            time.sleep(2 ** attempt)
    return None


def _validate_santos_tonnage(load_qty_t, weight_t, ship_name: str) -> bool:
    """
    Valida load_qty_t y weight_t contra rango §3.6 [0, 200_000] toneladas.

    Permite None. Solo rechaza valores explícitos fuera de rango. Returns
    False + WARNING si alguno se sale; True si ambos son válidos o None.
    """
    _, load_ok = check_or_log(
        lambda: validate_range(
            load_qty_t, min_value=0, max_value=200_000,
            source="santos_port", field=f"load_qty_t[{ship_name}]",
            allow_none=True,
        ),
        on_error="warn",
    )
    _, weight_ok = check_or_log(
        lambda: validate_range(
            weight_t, min_value=0, max_value=200_000,
            source="santos_port", field=f"weight_t[{ship_name}]",
            allow_none=True,
        ),
        on_error="warn",
    )
    return load_ok and weight_ok


def _parse_int(s) -> Optional[int]:
    """Parsea valor numérico a int. Mantiene compatibilidad con el código legacy
    (acepta strings tipo '12,345 t'). Devuelve None si vacío / no numérico.
    Log WARNING si excepción inesperada — cambio de schema → alerta sin NULLs masivos.
    """
    if s is None:
        return None
    if isinstance(s, (int, float)):
        try:
            return int(s)
        except (ValueError, TypeError) as e:
            parse_log_warning("santos_port._parse_int", s, e)
            return None
    try:
        cleaned = re.sub(r"[^\d]", "", str(s).split()[0])
        return int(cleaned) if cleaned else None
    except (ValueError, AttributeError, TypeError, IndexError) as e:
        parse_log_warning("santos_port._parse_int", s, e)
        return None


def _parse_arrival_dt(s: str) -> Optional[datetime]:
    """Parsea 'DD/MM/YYYY HH:MM:SS' o 'DD/MM/YYYY'."""
    if not s:
        return None
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def _parse_dotnet_date(s: str) -> Optional[datetime]:
    """Parsea formato WCF '/Date(epoch_ms[+/-HHMM])/' a datetime naive local Santos.

    El epoch viene en UTC; el offset indica la zona del servidor (Santos = -0300).
    Devolvemos datetime naive en hora local Santos para coherencia con el resto
    del modelo (que usa naive DateTime).
    """
    if not s:
        return None
    m = DOTNET_DATE_RE.search(str(s))
    if not m:
        return None
    try:
        epoch_ms = int(m.group(1))
        offset_str = m.group(2) or "+0000"
        sign = 1 if offset_str[0] == "+" else -1
        off_h = int(offset_str[1:3])
        off_m = int(offset_str[3:5])
        offset = timedelta(hours=off_h, minutes=off_m) * sign
        dt_utc = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)
        return (dt_utc + offset).replace(tzinfo=None)
    except (ValueError, IndexError, OverflowError) as e:
        parse_log_warning("santos_port._parse_dotnet_date", s, e)
        return None


# ---------------------------------------------------------------------------
# Parsers JSON
# ---------------------------------------------------------------------------

def _parse_berthed(payload: dict) -> list[dict]:
    """
    listarnaviosatracados → schema:
      {"PesquisarNaviosAtracadosResult": {"NaviosAtracados": [{...}, ...]}}

    Campos por ship:
      NomeNavio, Mercadoria, Embarque (toneladas cargando), Descarga,
      Local, TerminalEstatistica, NumeroViagem, AnoViagem, IdRap.
    """
    try:
        ships_raw = payload["PesquisarNaviosAtracadosResult"]["NaviosAtracados"]
    except (KeyError, TypeError) as e:
        parse_log_warning("santos_port._parse_berthed", payload, e)
        return []

    ships = []
    for s in ships_raw:
        cargo = s.get("Mercadoria", "") or ""
        if not SUGAR_RE.search(cargo):
            continue
        ships.append({
            "ship":       (s.get("NomeNavio") or "").strip(),
            "terminal":   (s.get("TerminalEstatistica") or s.get("Local") or "").strip(),
            "cargo":      cargo.strip(),
            "load_qty_t": _parse_int(s.get("Embarque")),
            "desc_qty_t": _parse_int(s.get("Descarga")),
            "voyage":     str(s.get("NumeroViagem") or "").strip(),
            "duv":        str(s.get("IdRap") or "").strip() or None,
        })
    return ships


def _parse_scheduled(payload: dict) -> list[dict]:
    """
    listarnaviosprogramados → schema:
      {"PesquisarNaviosProgramadosResult": {"NaviosProgramados": [{...}, ...]}}

    Campos por ship:
      NomeNavio, Mercadoria, Data, Hora, DataHora (.NET), Local, Manobra,
      NumeroViagem, IdRap, Periodo.
    """
    try:
        ships_raw = payload["PesquisarNaviosProgramadosResult"]["NaviosProgramados"]
    except (KeyError, TypeError) as e:
        parse_log_warning("santos_port._parse_scheduled", payload, e)
        return []

    ships = []
    for s in ships_raw:
        cargo = s.get("Mercadoria", "") or ""
        if not SUGAR_RE.search(cargo):
            continue

        arr = _parse_dotnet_date(s.get("DataHora"))
        if arr is None:
            data_hora = f"{s.get('Data', '')} {s.get('Hora', '')}".strip()
            arr = _parse_arrival_dt(data_hora)

        ships.append({
            "ship":       (s.get("NomeNavio") or "").strip(),
            "terminal":   (s.get("Local") or "").strip(),
            "cargo":      cargo.strip(),
            "arrival_dt": arr,
            "evento":     (s.get("Manobra") or "").strip(),
            "voyage":     str(s.get("NumeroViagem") or "").strip(),
            "duv":        str(s.get("IdRap") or "").strip() or None,
        })
    return ships


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def _upsert_ships(session: Session, page: str, ships: list[dict], today: date):
    for s in ships:
        if not _validate_santos_tonnage(
            s.get("load_qty_t"), s.get("weight_t"), s.get("ship", "?")
        ):
            continue
        record = {
            "snapshot_date": today,
            "page":          page,
            "ship_name":     s.get("ship") or "DESCONOCIDO",
            "cargo":         s.get("cargo"),
            "terminal":      s.get("terminal") or "",
            "nav_type":      s.get("nav_type"),
            "arrival_dt":    s.get("arrival_dt"),
            "load_qty_t":    s.get("load_qty_t"),
            "weight_t":      s.get("weight_t"),
            "evento":        s.get("evento"),
            "voyage":        s.get("voyage"),
            "duv":           s.get("duv"),
        }
        stmt = (
            insert(SantosPortSnapshot)
            .values(**record)
            .on_conflict_do_update(
                constraint="uq_santos_date_page_ship_terminal",
                set_={k: v for k, v in record.items()
                      if k not in ("snapshot_date", "page", "ship_name", "terminal")},
            )
        )
        try:
            session.execute(stmt)
        except Exception as e:
            logger.warning("upsert error %s %s: %s", page, s.get("ship"), e)
            session.rollback()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_santos_port(session: Session) -> dict:
    """
    Descarga endpoints REST del Puerto de Santos y almacena en DB
    los barcos con cargo ACUCAR/SUGAR.

    NOTA: page='expected' no se ingiere (endpoint no expuesto por el
    proveedor). Las claves n_expected/tonnage_expected se mantienen en 0
    para compatibilidad con santos_signal.

    Devuelve:
      {
        "expected": [], "scheduled": [...], "berthed": [...],
        "n_expected": 0, "n_scheduled": int, "n_berthed": int,
        "tonnage_expected": 0, "tonnage_berthed": int,
        "errors": [str],
      }
    """
    today  = date.today()
    result = {
        "expected": [], "scheduled": [], "berthed": [],
        "n_expected": 0, "n_scheduled": 0, "n_berthed": 0,
        "tonnage_expected": 0, "tonnage_berthed": 0,
        "errors": [],
    }

    parsers = {
        "berthed":   _parse_berthed,
        "scheduled": _parse_scheduled,
    }

    for page, parser in parsers.items():
        payload = _get_json(ENDPOINTS[page])
        if payload is None:
            result["errors"].append(f"No se pudo descargar {page}")
            continue

        ships = parser(payload)
        result[page] = ships
        _upsert_ships(session, page, ships, today)
        logger.info("Santos %s: %d barcos ACUCAR", page, len(ships))

    session.commit()

    result["n_scheduled"]     = len(result["scheduled"])
    result["n_berthed"]       = len(result["berthed"])
    result["tonnage_berthed"] = sum((s.get("load_qty_t") or 0) for s in result["berthed"])

    # Detectar partidas automáticamente después de cada scrape
    try:
        from services.santos_exports import process_departures
        new_deps = process_departures(session, reference_date=today)
        if new_deps:
            logger.info("Santos: %d nueva(s) partida(s) registrada(s)", len(new_deps))
        result["new_departures"] = new_deps
    except Exception as _e:
        logger.warning("santos_exports.process_departures: %s", _e)
        result["new_departures"] = []

    return result


def get_latest_snapshot(
    session: Session, *, reference: Optional[date] = None,
) -> Optional[dict]:
    """
    Lee el último snapshot de DB con freshness gate (§4.1). Si excede
    SANTOS_MAX_AGE_DAYS (2d), retorna None + WARNING — fuerza al downstream
    a degradar limpiamente sin contaminar señales con line-up "congelado".

    Args:
      session   — SQLAlchemy session.
      reference — fecha de referencia para freshness (default: date.today()).

    Returns:
      Dict con métricas del snapshot, o None si no hay datos o si están stale.
    """
    from sqlalchemy import text
    rows = session.execute(text("""
        SELECT page, ship_name, cargo, terminal, nav_type,
               arrival_dt, load_qty_t, weight_t, evento, voyage, snapshot_date
        FROM santos_port_snapshot
        WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM santos_port_snapshot)
        ORDER BY page, ship_name
    """)).fetchall()

    if not rows:
        return None

    snap_date_raw = rows[0][10]
    _, fresh = check_or_log(
        lambda: validate_freshness(
            snap_date_raw, max_age_days=SANTOS_MAX_AGE_DAYS,
            source="santos_port", field="latest_snapshot_date",
            reference=reference,
        ),
        on_error="warn",
    )
    if not fresh:
        return None

    snap_date = str(snap_date_raw)
    result    = {"expected": [], "scheduled": [], "berthed": [],
                 "n_expected": 0, "n_scheduled": 0, "n_berthed": 0,
                 "tonnage_expected": 0, "tonnage_berthed": 0,
                 "snapshot_date": snap_date, "errors": []}

    for r in rows:
        page, ship, cargo, terminal, nav, arr, load_q, weight, evento, voyage, _ = r
        if page not in result:
            continue
        result[page].append({
            "ship": ship, "cargo": cargo, "terminal": terminal,
            "nav_type": nav, "arrival_dt": arr,
            "load_qty_t": load_q, "weight_t": weight,
            "evento": evento, "voyage": voyage,
        })

    # Expected legacy puede quedar en DB con nav_type='Long' — preservamos esa
    # rama para que el histórico siga sumando hasta que caduque. Datos nuevos
    # llegan con expected=[] (no se ingiere ya) y tonnage_expected=0.
    today    = date.today()
    exp_long = [
        s for s in result["expected"]
        if (s.get("nav_type") or "").strip() == "Long"
        and (s.get("arrival_dt") is None or
             (hasattr(s["arrival_dt"], "date") and s["arrival_dt"].date() >= today))
    ]
    result["n_expected"]       = len(exp_long)
    result["tonnage_expected"] = sum((s.get("weight_t") or 0) for s in exp_long)
    result["n_scheduled"]      = len(result["scheduled"])
    result["n_berthed"]        = len(result["berthed"])
    result["tonnage_berthed"]  = sum((s.get("load_qty_t") or 0) for s in result["berthed"])

    return result
