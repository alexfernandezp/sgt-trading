"""
Ingestion de datos de produccion sucroalcooleira de Brasil (MAPA).

Fuente: https://www.gov.br/agricultura/pt-br/assuntos/sustentabilidade/
        agroenergia/acompanhamento-da-producao-sucroalcooleira

Frecuencia: bi-semanal (quincena), temporada abril-marzo.
Cobertura: temporadas 2007-2008 hasta la actual.

Metodo:
  1. Scraping del indice principal → lista de sub-paginas por temporada.
  2. Por cada sub-pagina → lista de hrefs .xls.
  3. Descarga + parseo de XLS (xlrd) → fila "Tot.g" (total nacional).
  4. Upsert en tabla brazil_production.
"""
import io
import logging
import re
import time
from datetime import date, datetime
from typing import Optional

import requests
import xlrd
from bs4 import BeautifulSoup
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from models import BrazilProduction

logger = logging.getLogger(__name__)

BASE_URL   = "https://www.gov.br"
INDEX_URL  = (
    BASE_URL
    + "/agricultura/pt-br/assuntos/sustentabilidade/agroenergia/"
    + "acompanhamento-da-producao-sucroalcooleira"
)
HEADERS    = {"User-Agent": "Mozilla/5.0 (compatible; sgt-trading/1.0)"}
DELAY_S    = 1.2   # entre peticiones — respeto al servidor gubernamental
SHEET_RE   = re.compile(r"gerarRel", re.IGNORECASE)


# ---------------------------------------------------------------------------
# URL discovery
# ---------------------------------------------------------------------------

def _get_soup(url: str, retries: int = 3) -> Optional[BeautifulSoup]:
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return BeautifulSoup(r.content, "html.parser")
        except Exception as e:
            logger.warning("GET %s intento %d: %s", url, attempt + 1, e)
            time.sleep(2 ** attempt)
    return None


def _absolute(href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return BASE_URL + href
    return href


def _discover_season_pages(index_soup: BeautifulSoup) -> list[str]:
    """
    Extrae URLs de sub-paginas de cada temporada desde la pagina indice.
    Patron: anchors que contienen '20XX-20XX' en href o texto.
    """
    season_urls = []
    season_re   = re.compile(r"20\d{2}-20\d{2}")
    seen        = set()

    for a in index_soup.find_all("a", href=True):
        href = a["href"]
        if season_re.search(href) or season_re.search(a.get_text()):
            url = _absolute(href)
            if url not in seen:
                seen.add(url)
                season_urls.append(url)

    # La pagina indice puede tener paginacion — buscar boton "siguiente"
    next_link = index_soup.find("a", class_=re.compile(r"next|proxima", re.I))
    if next_link and next_link.get("href"):
        next_url = _absolute(next_link["href"])
        next_soup = _get_soup(next_url)
        if next_soup:
            time.sleep(DELAY_S)
            season_urls += _discover_season_pages(next_soup)

    return season_urls


def _discover_xls_urls(season_url: str) -> list[str]:
    """
    Extrae hrefs .xls de una sub-pagina de temporada.
    Filtra archivos de 'conhecimento' o similares que no son datos.
    """
    soup = _get_soup(season_url)
    if not soup:
        return []

    urls = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.lower().endswith(".xls") and not href.lower().endswith(".xlsx"):
            continue
        skip_words = ("conhecimento", "manual", "metodologia", "nota")
        if any(w in href.lower() for w in skip_words):
            continue
        urls.append(_absolute(href))

    return urls


# ---------------------------------------------------------------------------
# XLS parsing
# ---------------------------------------------------------------------------

def _parse_date_from_url(url: str, harvest_year: str) -> Optional[date]:
    """
    Infiere la fecha de referencia de la quincena a partir del nombre del archivo.
    Patrones:
      Acompanhamentodaproduo2526_010526.xls       → DDMMYY
      Acompanhamentodaproduo2526_160425_2.xls     → DDMMYY con sufijo revision _N
      Acompanhamentodaproducao_2526_160126.xls    → idem
    """
    fname = url.split("/")[-1].replace(".xls", "").replace(".xlsx", "")

    # Limpiar sufijo de revision: _1, _2, _3, etc. al final del nombre
    fname_clean = re.sub(r"_\d{1,2}$", "", fname)

    def _try_parse(s):
        """Intenta parsear DDMMYYYY o DDMMYY al final de la cadena."""
        m8 = re.search(r"[_.]?(\d{2})(\d{2})(\d{4})$", s)
        if m8:
            try:
                return date(int(m8.group(3)), int(m8.group(2)), int(m8.group(1)))
            except ValueError:
                pass
        m6 = re.search(r"[_.]?(\d{2})(\d{2})(\d{2})$", s)
        if m6:
            try:
                return date(2000 + int(m6.group(3)), int(m6.group(2)), int(m6.group(1)))
            except ValueError:
                pass
        return None

    # Intentar primero el nombre limpio (sin sufijo revision), luego el original
    for candidate in (fname_clean, fname):
        result = _try_parse(candidate)
        if result is not None:
            return result

    # Fallback: 1 de abril del primer año de la temporada
    try:
        first_year = int(harvest_year.split("-")[0])
        return date(first_year, 4, 1)
    except Exception:
        return None


def _fortnight_seq(ref_date: date, harvest_year: str) -> int:
    """
    Quincena secuencial dentro del año cosecha (1 = primera quincena de abril).
    La temporada arranca en abril. Cada mes tiene 2 quincenas (dia 1-15 → #1, 16+ → #2).
    """
    try:
        start_year = int(harvest_year.split("-")[0])
    except Exception:
        start_year = ref_date.year

    april_start = date(start_year, 4, 1)
    delta_days  = (ref_date - april_start).days
    month_offset = max(0, delta_days) // 15   # aproximado
    return max(1, month_offset + 1)


def _col_index(sheet, row_idx: int, candidates: list[str]) -> Optional[int]:
    """Busca el indice de columna cuyo header (fila row_idx) coincide con alguno de candidates."""
    for col in range(sheet.ncols):
        val = str(sheet.cell_value(row_idx, col)).strip().lower()
        for c in candidates:
            if c.lower() in val:
                return col
    return None


def _find_first_header_row(sheet) -> int:
    """Localiza la primera fila que contiene 'UF' en col 0 — inicio de la primera seccion."""
    for row in range(min(30, sheet.nrows)):
        val = str(sheet.cell_value(row, 0)).strip().lower()
        if val in ("uf", "estado", "unidade", "uf "):
            return row
    return 0


def _find_subheader_row(sheet, header_row: int) -> int:
    """
    La sub-cabecera con 'Cana'/'Açucar'/'Etanol' esta una o dos filas bajo el header UF.
    Retorna la fila donde aparece 'cana'.
    """
    for offset in (1, 2):
        r = header_row + offset
        if r >= sheet.nrows:
            break
        for col in range(min(10, sheet.ncols)):
            val = str(sheet.cell_value(r, col)).strip().lower()
            if "cana" in val or "cane" in val:
                return r
    return header_row + 1


def _find_national_total_row(sheet) -> Optional[int]:
    """
    Estrategia:
    1. Buscar 'TOTAL BRASIL' (o variante) → la fila 'Tot.' que le sigue.
    2. Si no, buscar la ultima fila con 'Tot.' o 'Total' antes de notas ('(*)').
    """
    n = sheet.nrows
    total_brasil_row = None
    for row in range(n):
        val = str(sheet.cell_value(row, 0)).strip().upper()
        if "TOTAL BRASIL" in val or "TOTAL GERAL" in val or "TOT.G" in val:
            total_brasil_row = row
            break

    if total_brasil_row is not None:
        # La siguiente fila con "Tot." es el total nacional
        for row in range(total_brasil_row + 1, min(total_brasil_row + 5, n)):
            val = str(sheet.cell_value(row, 0)).strip().lower()
            if val.startswith("tot"):
                return row
        # Fallback: la misma fila TOTAL BRASIL si tiene datos numericos
        for col in range(1, min(5, sheet.ncols)):
            try:
                v = float(sheet.cell_value(total_brasil_row, col))
                if v > 0:
                    return total_brasil_row
            except Exception:
                pass

    # Sin "TOTAL BRASIL": usar la ultima fila "Tot." del archivo
    last_tot = None
    for row in range(n - 1, -1, -1):
        val = str(sheet.cell_value(row, 0)).strip().lower()
        if val.startswith("tot"):
            last_tot = row
            break
    return last_tot


def _parse_xls(content: bytes, url: str, harvest_year: str) -> Optional[dict]:
    """
    Parsea un XLS de MAPA y extrae la fila de total nacional.

    Estructura del XLS (puede variar por año):
      Fila A: Safra / Region header
      Fila B: UF | PRODUCAO TOTAL | ... | ANIDRO | ...
      Fila C: '' | Cana | Açucar | Etanol | Producao | ... (sub-header)
      Fila D: '' | (t) | (t) | (m3) | ...
      ...estados...
      "Tot." = total regional
      ...repetido por region...
      "TOTAL BRASIL" (marcador)
      "Tot." = total nacional

    Devuelve dict con los campos numericos o None si falla.
    """
    try:
        wb = xlrd.open_workbook(file_contents=content)
    except Exception as e:
        logger.warning("xlrd open error %s: %s", url, e)
        return None

    sheet = None
    for sh in wb.sheets():
        if SHEET_RE.search(sh.name):
            sheet = sh
            break
    if sheet is None:
        sheet = wb.sheets()[0]

    header_row  = _find_first_header_row(sheet)
    subhdr_row  = _find_subheader_row(sheet, header_row)

    # Detectar columnas desde la sub-cabecera (donde están "Cana", "Açúcar", "Etanol")
    cane_col   = _col_index(sheet, subhdr_row, ["cana", "cane"])
    sugar_col  = _col_index(sheet, subhdr_row, ["açúcar", "acucar", "sugar", "açucar", "a\xe7ucar"])
    anidro_col = _col_index(sheet, subhdr_row, ["anidro", "anhydrous"])
    hidrat_col = _col_index(sheet, subhdr_row, ["hidrat", "hydrat"])
    etanol_col = _col_index(sheet, subhdr_row, ["etanol", "ethanol"])

    # Si etanol_col apunta a la misma que anidro/hidrat, buscar otra más específica
    if etanol_col == anidro_col:
        etanol_col = None
    if etanol_col is None:
        # Etanol total suele ser la primera columna (m³) en la seccion PRODUCAO TOTAL
        # Buscar en la fila de unidades: primera columna con "(m" después de col 2
        units_row = subhdr_row + 1
        if units_row < sheet.nrows:
            for col in range(3, min(15, sheet.ncols)):
                unit_v = str(sheet.cell_value(units_row, col)).strip().lower()
                if "m" in unit_v and col != anidro_col:
                    etanol_col = col
                    break

    if cane_col is None:
        logger.warning("No encontrada columna Cana en %s (sub-header row=%d)", url, subhdr_row)
        logger.debug("Sub-header row contenido: %s",
                     [str(sheet.cell_value(subhdr_row, c)) for c in range(min(12, sheet.ncols))])
        return None

    tot_row = _find_national_total_row(sheet)
    if tot_row is None:
        logger.warning("No encontrada fila total en %s", url)
        return None

    logger.debug("Columnas: cana=%s sugar=%s etanol=%s anidro=%s hidrat=%s  tot_row=%d",
                 cane_col, sugar_col, etanol_col, anidro_col, hidrat_col, tot_row)

    def _num(col):
        if col is None:
            return None
        try:
            v = sheet.cell_value(tot_row, col)
            if isinstance(v, (int, float)):
                f = float(v)
                return int(f) if f > 0 else None
            cleaned = str(v).replace(".", "").replace(",", ".").strip()
            f = float(cleaned)
            return int(f) if f > 0 else None
        except Exception:
            return None

    cane   = _num(cane_col)
    sugar  = _num(sugar_col)
    anidro = _num(anidro_col)
    hidrat = _num(hidrat_col)
    etanol_total = _num(etanol_col)

    # Si etanol_col no da dato útil, calcular de anidro + hidrat
    if not etanol_total and anidro is not None and hidrat is not None:
        etanol_total = anidro + hidrat

    if cane is None:
        logger.warning("Cana=None en fila %d de %s", tot_row, url)
        return None

    # Sugar mix %: azucar / (azucar + etanol_total_equiv)
    # Factor MAPA/DATAGRO: 1 m³ etanol ≈ 1.2 t azucar equivalente (ATR industry standard)
    sugar_mix = None
    if sugar and etanol_total:
        etanol_equiv_t = etanol_total * 1.2   # m3 → t equivalente azucar
        total_prod     = sugar + etanol_equiv_t
        sugar_mix      = round(sugar / total_prod * 100, 3) if total_prod > 0 else None

    return {
        "cane_crushed_t":       cane,
        "sugar_t":              sugar,
        "ethanol_anhydrous_m3": anidro,
        "ethanol_hydrated_m3":  hidrat,
        "ethanol_total_m3":     etanol_total,
        "sugar_mix_pct":        sugar_mix,
    }


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def _upsert(session: Session, record: dict):
    stmt = (
        insert(BrazilProduction)
        .values(**record)
        .on_conflict_do_update(
            constraint="uq_brazil_harvest_fortnight",
            set_={k: v for k, v in record.items()
                  if k not in ("harvest_year", "fortnight_seq")},
        )
    )
    session.execute(stmt)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_brazil_production(
    session: Session,
    max_seasons: int = 5,
    delay: float = DELAY_S,
) -> dict:
    """
    Descarga y almacena datos MAPA de produccion sucroalcooleira.

    max_seasons: cuantas temporadas recientes procesar (por defecto 5 ≈ 5 años).
    Devuelve {"inserted": N, "errors": M, "seasons": [...]}.
    """
    logger.info("Iniciando scraping MAPA — index: %s", INDEX_URL)

    # Paginacion de la pagina indice (puede tener b_start:int=XX)
    all_season_urls = []
    offset = 0
    while True:
        url   = INDEX_URL if offset == 0 else f"{INDEX_URL}?b_start:int={offset}"
        soup  = _get_soup(url)
        if not soup:
            break
        found = _discover_season_pages(soup)
        if not found:
            break
        all_season_urls += found
        time.sleep(delay)
        # Verificar si hay mas paginas comprobando el link "siguiente"
        next_a = soup.find("a", string=re.compile(r"siguiente|next|próxima|›|»", re.I))
        if next_a:
            offset += 20
        else:
            break

    # Deduplicar y ordenar descendente (mas recientes primero)
    season_re  = re.compile(r"(20\d{2}-20\d{2})")
    seen_urls  = {}
    for u in all_season_urls:
        m = season_re.search(u)
        key = m.group(1) if m else u
        seen_urls[key] = u

    # Filtrar temporadas invalidas (span de 1 año: YYYY-YYYY+1)
    def _valid_season(key):
        parts = key.split("-")
        if len(parts) != 2:
            return False
        try:
            y1, y2 = int(parts[0]), int(parts[1])
            return y2 == y1 + 1   # solo temporadas consecutivas
        except Exception:
            return False

    seen_urls = {k: v for k, v in seen_urls.items() if _valid_season(k)}
    seasons_sorted = sorted(seen_urls.items(), reverse=True)[:max_seasons]
    logger.info("Temporadas a procesar: %s", [s[0] for s in seasons_sorted])

    inserted = 0
    errors   = 0
    processed_seasons = []

    for harvest_year, season_url in seasons_sorted:
        logger.info("  Temporada %s → %s", harvest_year, season_url)
        xls_urls = _discover_xls_urls(season_url)
        time.sleep(delay)

        for xls_url in xls_urls:
            try:
                r = requests.get(xls_url, headers=HEADERS, timeout=60)
                r.raise_for_status()
                content = r.content
                time.sleep(delay)
            except Exception as e:
                logger.warning("    Error descargando %s: %s", xls_url, e)
                errors += 1
                continue

            parsed = _parse_xls(content, xls_url, harvest_year)
            if parsed is None:
                errors += 1
                continue

            ref_date = _parse_date_from_url(xls_url, harvest_year)
            if ref_date is None:
                logger.warning("    No se pudo inferir fecha de %s", xls_url)
                errors += 1
                continue

            seq = _fortnight_seq(ref_date, harvest_year)
            record = {
                "harvest_year":  harvest_year,
                "fortnight_seq": seq,
                "report_date":   ref_date,
                "source_url":    xls_url,
                **parsed,
            }

            try:
                _upsert(session, record)
                inserted += 1
                logger.info("    OK %s seq=%d cana=%s azucar=%s",
                            ref_date, seq, parsed["cane_crushed_t"], parsed["sugar_t"])
            except Exception as e:
                logger.warning("    DB error %s: %s", xls_url, e)
                session.rollback()
                errors += 1

        processed_seasons.append(harvest_year)

    session.commit()
    logger.info("MAPA ingestion completa: %d filas, %d errores", inserted, errors)
    return {"inserted": inserted, "errors": errors, "seasons": processed_seasons}


def get_latest_production(session: Session, n: int = 4) -> list[dict]:
    """
    Devuelve las N ultimas filas de brazil_production ordenadas por report_date DESC.
    Util para calcular senales en tiempo real.
    """
    from sqlalchemy import text
    rows = session.execute(text("""
        SELECT report_date, harvest_year, fortnight_seq,
               cane_crushed_t, sugar_t, ethanol_total_m3, sugar_mix_pct
        FROM brazil_production
        ORDER BY report_date DESC
        LIMIT :n
    """), {"n": n}).fetchall()

    return [
        {
            "report_date":    str(r[0]),
            "harvest_year":   r[1],
            "fortnight_seq":  r[2],
            "cane_crushed_t": float(r[3]) if r[3] else None,
            "sugar_t":        float(r[4]) if r[4] else None,
            "ethanol_total_m3": float(r[5]) if r[5] else None,
            "sugar_mix_pct":  float(r[6]) if r[6] else None,
        }
        for r in rows
    ]
