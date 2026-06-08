"""
UNICA Quinzenal — ingestion de reportes quincenales Centro-Sul.

UNICA publica cada 15 dias un PDF con datos de moagem, producao de acucar
y etanol del Centro-Sul. Es la fuente mas granular y fresca para Brasil CS.

URL scraping: https://unicadata.com.br/listagem.php?idMn=63
Descarga PDF: https://unicadata.com.br/download_media.php?idM={idm}

Unidades en el PDF:
  Cana / Acucar : mil toneladas (nota ¹) → dividir por 1000 para obtener Mt
  Etanol        : milhoes de litros (nota ²)
  ATR           : kg de ATR / tonelada de cana (nota ³)

Jerarquia en adjust_brazil():
  1. UNICA quinzenal fresco (<30d): sugar_cumulative_mt proyectado a fin de safra
  2. CONAB levantamento
  3. GEE NDVI integral
  4. USDA sin cambios
"""
import io
import logging
import re
from datetime import date
from typing import Optional

import httpx
import pdfplumber

logger = logging.getLogger(__name__)

_LISTAGEM_URL = "https://unicadata.com.br/listagem.php?idMn=63"
_DOWNLOAD_URL = "https://unicadata.com.br/download_media.php?idM={idm}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124"}
_TIMEOUT = 60

_MONTHS_PT = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "março": 3,
    "abril": 4, "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
    "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}

# Progreso tipico de safra Centro-Sul por quincena (% del total anual cosechado)
# Fuente: histórico UNICA 2015-2024, media multi-año
_SEASON_PROGRESS_PCT = {
    (4, 1): 2.5,   (4, 2): 6.5,
    (5, 1): 11.0,  (5, 2): 16.5,
    (6, 1): 22.5,  (6, 2): 29.0,
    (7, 1): 36.0,  (7, 2): 43.5,
    (8, 1): 51.0,  (8, 2): 58.5,
    (9, 1): 65.5,  (9, 2): 72.0,
    (10, 1): 78.0, (10, 2): 83.5,
    (11, 1): 88.0, (11, 2): 92.5,
    (12, 1): 96.0, (12, 2): 98.5,
}


def _pt_float(s: str) -> float:
    """Convierte numero formato PT '1.234,56' a float."""
    return float(s.replace(".", "").replace(",", "."))


def scrape_latest_idm() -> Optional[int]:
    """Obtiene el ID del documento mas reciente del reporte quinzenal.
    Toma el idM MAS ALTO de la pagina (no el primero) porque el ID numerico
    es incremental — el mayor siempre es el mas reciente."""
    try:
        r = httpx.get(_LISTAGEM_URL, headers=_HEADERS, timeout=20, follow_redirects=True)
        matches = re.findall(r"download_media\.php\?idM=(\d+)", r.text)
        if matches:
            return max(int(m) for m in matches)
        logger.warning("UNICA: no se encontro idM en %s", _LISTAGEM_URL)
    except Exception as e:
        logger.warning("UNICA scrape idM: %s", e)
    return None


def download_pdf(idm: int) -> Optional[bytes]:
    """Descarga el PDF del reporte quinzenal dado su idM."""
    url = _DOWNLOAD_URL.format(idm=idm)
    try:
        r = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
        if r.status_code == 200 and r.content[:4] == b"%PDF":
            logger.info("UNICA PDF descargado: idM=%d (%d bytes)", idm, len(r.content))
            return r.content
        logger.warning("UNICA PDF: status=%d idM=%d", r.status_code, idm)
    except Exception as e:
        logger.warning("UNICA PDF download: %s", e)
    return None


def parse_unica_pdf(pdf_bytes: bytes) -> Optional[dict]:
    """
    Parsea el PDF quinzenal UNICA y extrae datos del Centro-Sul.

    Retorna dict con:
      safra, quinzena_num, ref_month, ref_year, position_date,
      sugar_cumulative_mt, cane_cumulative_mt,
      ethanol_total_ml, ethanol_anidro_ml, ethanol_hidratado_ml,
      atr_kg_t, mix_ethanol_pct,
      sugar_quinzenal_mt, cane_quinzenal_mt,
      yoy_sugar_pct, season_progress_pct, projected_full_year_mt
    """
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception as e:
        logger.error("UNICA parse PDF: %s", e)
        return None

    result = {}

    # 1. Safra — buscar en el encabezado de Tabla 1 (evita matches de texto
    #    comparativo "safra 2025/2026" que aparece en el prose anterior)
    m = re.search(r"Tabela 1\.\s+Safra\s+(\d{4}/\d{4})", full_text, re.IGNORECASE)
    if not m:
        # Fallback: buscar "SAFRA YYYY/YYYY" solo en los primeros 500 chars del PDF
        # para evitar capturar referencias comparativas de paginas posteriores
        m = re.search(r"S\s*AFRA\s+(\d{4}/\d{4})", full_text[:500])
    if not m:
        logger.warning("UNICA parse: safra no encontrada")
        return None
    result["safra"] = m.group(1)
    safra_start_year = int(result["safra"].split("/")[0])

    # 2. Fecha de posicion (ej: "Posição até 01/05/2026")
    m = re.search(r"[Pp]osi[çc][aã]o até (\d{2})/(\d{2})/(\d{4})", full_text)
    if m:
        result["position_date"] = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    else:
        result["position_date"] = date.today()

    # 3. Quinzena y mes de referencia (ej: "segunda quinzena de abril")
    m = re.search(r"(\d)ª\s+quinzena\s+de\s+(\w+)", full_text, re.IGNORECASE)
    if m:
        result["quinzena_num"] = int(m.group(1))
        month_name = m.group(2).lower().strip()
        result["ref_month"] = _MONTHS_PT.get(month_name, result["position_date"].month)
    else:
        result["quinzena_num"] = 1 if result["position_date"].day <= 16 else 2
        result["ref_month"] = result["position_date"].month

    # ref_year: Abr-Dic del año inicio safra; Ene-Mar del año siguiente
    result["ref_year"] = (
        safra_start_year if result["ref_month"] >= 4 else safra_start_year + 1
    )

    # 4. Extraer bloque Tabla 1 (acumulado) y Tabla 2 (quinzenal)
    t1_start = full_text.find("Tabela 1.")
    t2_start = full_text.find("Tabela 2.", t1_start + 1 if t1_start >= 0 else 0)
    t3_start = full_text.find("Tabela 3.", t2_start + 1 if t2_start >= 0 else 0)

    t1_block = full_text[t1_start:t2_start] if t1_start >= 0 and t2_start > t1_start else ""
    t2_block = full_text[t2_start:t3_start] if t2_start >= 0 and t3_start > t2_start else ""

    def _cs_value(block: str, pattern: str) -> Optional[float]:
        """
        Extrae el valor Centro-Sul ACTUAL (segundo numero de la fila).
        Formato: "{produto} prev_cs  curr_cs  var%  prev_sp  curr_sp ..."
        Usa (?m)^ para anclar al inicio de linea y evitar matches parciales
        (ej: "açucar" dentro de "Cana-de-açucar").
        """
        m = re.search(r"(?m)^" + pattern + r"\s+([\d.]+)\s+([\d.]+)\s+[-+\d,]+%",
                      block, re.IGNORECASE)
        return _pt_float(m.group(2)) if m else None

    # Acumulado Centro-Sul (tabla 1)
    if t1_block:
        raw_cane   = _cs_value(t1_block, r"Cana-de-açúcar\s+¹")
        raw_sugar  = _cs_value(t1_block, r"Açúcar\s+¹")
        raw_eth_t  = _cs_value(t1_block, r"Etanol total\s+²")
        raw_eth_a  = _cs_value(t1_block, r"Etanol anidro\s+²")
        raw_eth_h  = _cs_value(t1_block, r"Etanol hidratado\s+²")

        # ATR/tonelada (formato diferente: "106,81  112,58  5,40%")
        m_atr = re.search(
            r"ATR/\s*tonelada de cana\s+³\s+([\d,]+)\s+([\d,]+)", t1_block, re.IGNORECASE
        )

        # Prev year sugar (para YoY) — misma ancla de inicio de linea que _cs_value
        m_sugar_prev = re.search(
            r"(?m)^Açúcar\s+¹\s+([\d.]+)\s+([\d.]+)", t1_block, re.IGNORECASE
        )

        if raw_cane  is not None: result["cane_cumulative_mt"]  = round(raw_cane  / 1000, 4)
        if raw_sugar is not None: result["sugar_cumulative_mt"] = round(raw_sugar / 1000, 4)
        if raw_eth_t is not None: result["ethanol_total_ml"]    = raw_eth_t
        if raw_eth_a is not None: result["ethanol_anidro_ml"]   = raw_eth_a
        if raw_eth_h is not None: result["ethanol_hidratado_ml"]= raw_eth_h
        if m_atr:                 result["atr_kg_t"]            = _pt_float(m_atr.group(2))

        if m_sugar_prev and raw_sugar is not None:
            prev_sugar = _pt_float(m_sugar_prev.group(1))
            if prev_sugar > 0:
                result["yoy_sugar_pct"] = round((raw_sugar / prev_sugar - 1) * 100, 1)

        # Mix etanol acumulado
        m_mix = re.search(r"etanol\s+([\d,]+)%\s+([\d,]+)%", t1_block, re.IGNORECASE)
        if m_mix:
            result["mix_ethanol_pct"] = _pt_float(m_mix.group(2))

    # Quinzenal Centro-Sul (tabla 2)
    if t2_block:
        raw_cane_q  = _cs_value(t2_block, r"Cana-de-açúcar\s+¹")
        raw_sugar_q = _cs_value(t2_block, r"Açúcar\s+¹")
        if raw_cane_q  is not None: result["cane_quinzenal_mt"]  = round(raw_cane_q  / 1000, 4)
        if raw_sugar_q is not None: result["sugar_quinzenal_mt"] = round(raw_sugar_q / 1000, 4)

    # 5. Proyeccion full-year (solo si tenemos acumulado)
    sugar_cum = result.get("sugar_cumulative_mt")
    if sugar_cum is not None:
        prog = _SEASON_PROGRESS_PCT.get(
            (result["ref_month"], result["quinzena_num"]), None
        )
        if prog and prog > 5:
            result["season_progress_pct"] = prog
            result["projected_full_year_mt"] = round(sugar_cum / (prog / 100), 3)
        else:
            result["season_progress_pct"] = prog or 0
            result["projected_full_year_mt"] = None   # demasiado temprano

    logger.info(
        "UNICA %s Q%d/%s: acucar_acum=%.3f Mt  Q_acucar=%.3f Mt  yoy=%+.1f%%  proyeccion=%s Mt",
        result.get("safra", "?"),
        result.get("quinzena_num", 0),
        result.get("ref_month", 0),
        result.get("sugar_cumulative_mt", 0),
        result.get("sugar_quinzenal_mt", 0),
        result.get("yoy_sugar_pct", 0),
        result.get("projected_full_year_mt", "N/D"),
    )
    return result


def get_latest_unica() -> Optional[dict]:
    """
    Descarga y parsea el reporte quinzenal UNICA mas reciente.

    Retorna dict con los datos, o None si no disponible.
    Incluye idm_source para trazabilidad.
    """
    idm = scrape_latest_idm()
    if not idm:
        return None

    pdf_bytes = download_pdf(idm)
    if not pdf_bytes:
        return None

    data = parse_unica_pdf(pdf_bytes)
    if data:
        data["idm_source"] = idm
    return data


def brazil_unica_estimate(
    current_marketing_year: int,
) -> tuple[Optional[float], str, float]:
    """
    Interfaz para world_balance_model.project_forward_year() y adjust_brazil().

    Descarga UNICA y proyecta produccion Centro-Sul para el safra correspondiente
    a current_marketing_year. Retorna (projected_cs_mt, source_str, confidence).

    Nota: La safra CS (Abr-Mar) se alinea con el marketing year USDA para Brasil
    (USDA usa Abr-Mar para azucar en Brasil). El safra 2026/2027 (inicio año 2026)
    corresponde al marketing year 2026.
    """
    data = get_latest_unica()
    if not data:
        return None, "unica_unavailable", 0.80

    # Validar que el safra del reporte corresponde al año solicitado
    safra = data.get("safra", "")
    if safra:
        try:
            safra_start = int(safra.split("/")[0])
            if safra_start != current_marketing_year:
                logger.info(
                    "UNICA: safra %s no corresponde a mkt_year %d — omitiendo",
                    safra, current_marketing_year,
                )
                return None, "unica_wrong_year", 0.80
        except (ValueError, IndexError):
            pass

    position_date = data.get("position_date", date.today())
    age_days = (date.today() - position_date).days
    if age_days > 30:
        logger.info("UNICA: reporte con %d dias de antiguedad — omitiendo", age_days)
        return None, "unica_stale", 0.80

    projected = data.get("projected_full_year_mt")
    if projected is None:
        logger.info("UNICA: proyeccion no disponible (temporada muy temprana)")
        return None, "unica_too_early", 0.80

    prog = data.get("season_progress_pct", 0)
    # Confidence escala con progreso de temporada:
    # 6%: 0.55 | 30%: 0.72 | 65%: 0.85 | 90%: 0.92
    confidence = round(min(0.93, 0.55 + (prog / 100) * 0.40), 2)

    source = "unica_quinzenal_%s_Q%d" % (
        safra, data.get("quinzena_num", 0)
    )
    return projected, source, confidence
