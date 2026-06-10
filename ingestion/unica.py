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
# Ene-Mar extendidos a ~99-100% para que proyección fallback funcione al cierre
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
    (1, 1): 99.0,  (1, 2): 99.5,
    (2, 1): 99.7,  (2, 2): 99.9,
    (3, 1): 99.9,  (3, 2): 100.0,
}

# Orden canónico de quincenas de la safra CS (24 quincenas, empieza 16/04)
# seq 1 = 16/abr, seq 24 = 01/abr (año siguiente)
_SEASON_FORTNIGHTS = [
    (4, 16), (5, 1), (5, 16), (6, 1), (6, 16), (7, 1), (7, 16), (8, 1),
    (8, 16), (9, 1), (9, 16), (10, 1), (10, 16), (11, 1), (11, 16), (12, 1),
    (12, 16), (1, 1), (1, 16), (2, 1), (2, 16), (3, 1), (3, 16), (4, 1),
]
_SEQ = {md: i + 1 for i, md in enumerate(_SEASON_FORTNIGHTS)}


def season_fortnight_seq(d) -> Optional[int]:
    """(month, day∈{1,16}) → seq 1..24 dentro de la safra CS. None si no es quincena válida."""
    from datetime import date as _date
    if isinstance(d, _date):
        return _SEQ.get((d.month, d.day))
    return _SEQ.get(tuple(d))


def _pt_float(s: str) -> float:
    """Convierte numero formato PT '1.234,56' a float."""
    return float(s.replace(".", "").replace(",", "."))


def scrape_latest_idm() -> Optional[int]:
    """Obtiene el ID del documento mas reciente del reporte quinzenal.
    La pagina listagem.php?idMn=63 muestra solo el reporte activo — tomamos
    el PRIMER link download_media que aparece, que es el que el sitio presenta
    como documento actual. No usamos max() porque los IDs no son estrictamente
    incrementales en orden de publicacion."""
    try:
        r = httpx.get(_LISTAGEM_URL, headers=_HEADERS, timeout=20, follow_redirects=True)
        matches = re.findall(r"download_media\.php\?idM=(\d+)", r.text)
        if matches:
            return int(matches[0])
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


def _parse_cumulative_table(full_text: str, tabela_num: int) -> dict:
    """
    Parsea Tabela N (3-7) del PDF UNICA — serie acumulada quincena-a-quincena CS.

    Formato de fila: DD/MM  SP_prev SP_cur Var%  CS_prev CS_cur Var%  DEM_prev DEM_cur Var%
    Retorna {seq: {"cs_cur": float, "cs_prev": float}} keyed by season_fortnight_seq.
    Tabela 3=caña(t), 4=azúcar(t), 5/6/7=etanol total/anidro/hidratado(m³).
    """
    t_start = full_text.find(f"Tabela {tabela_num}.")
    if t_start < 0:
        logger.warning("UNICA parse: Tabela %d no encontrada", tabela_num)
        return {}
    t_end_candidates = [
        full_text.find(f"Tabela {tabela_num + 1}.", t_start + 1),
        full_text.find("Nota", t_start + 1),
    ]
    t_end = min((x for x in t_end_candidates if x > t_start), default=len(full_text))
    block = full_text[t_start:t_end]

    # Detectar el safra del encabezado para inferir años
    safra_m = re.search(r"(\d{4}/\d{4})", block)
    safra_cur_year = int(safra_m.group(1).split("/")[0]) if safra_m else None

    result = {}
    # Filas con fecha: "DD/MM" al inicio de línea
    for line in block.splitlines():
        line = line.strip()
        dm = re.match(r"^(\d{1,2})/(\d{2})\s+", line)
        if not dm:
            continue
        day = int(dm.group(1))
        mon = int(dm.group(2))
        if day not in (1, 16):
            continue
        seq = _SEQ.get((mon, day))
        if seq is None:
            continue

        # Extraer los números de la línea (ignorar columna SP, tomar CS)
        # Formato esperado: DD/MM  num num num%  num num num%  num num num%
        nums = re.findall(r"([\d.]+(?:,\d+)?)", line[len(dm.group(0)):])
        # Columnas 0,1,2 = SP_prev, SP_cur, var%; 3,4,5 = CS_prev, CS_cur, var%
        if len(nums) >= 5:
            try:
                cs_prev = _pt_float(nums[3])
                cs_cur  = _pt_float(nums[4])
                result[seq] = {"cs_cur": cs_cur, "cs_prev": cs_prev, "mon": mon, "day": day}
            except Exception:
                pass

    return result


def _decumulate(series: dict) -> dict:
    """
    De-acumula serie {seq: val_acumulado} → {seq: val_neto_quincena}.
    net[primer_seq] = cum[primer_seq]; net[k] = cum[k] − cum[k-1].
    """
    if not series:
        return {}
    seqs = sorted(series.keys())
    net = {}
    for i, seq in enumerate(seqs):
        if i == 0:
            net[seq] = series[seq]
        else:
            net[seq] = series[seq] - series[seqs[i - 1]]
    return net


def save_unica_to_db(session, data: dict) -> bool:
    """
    Persiste datos del PDF quinzenal en unica_biweekly (region='CS').

    Parsea Tabelas 3-7 (serie acumulada), de-acumula a neto por quincena,
    y hace upsert de cada quincena presente con datos per-quincena homogéneos
    al Excel histórico. ATR y mix vienen de Tabela 1 (ya en data).
    Retorna True si se guardó al menos una fila.
    """
    from sqlalchemy import text
    from services.data_quality import validate_range, parse_log_warning

    safra = data.get("safra")
    pdf_bytes = data.get("_pdf_bytes")
    if not safra:
        return False

    safra_norm = safra.replace("/", "-")
    safra_start = int(safra_norm.split("-")[0])

    # ATR y mix de Tabela 1 (última fila, misma para todas las quincenas del PDF)
    atr = data.get("atr_kg_t")
    emix = data.get("mix_ethanol_pct")

    if not pdf_bytes:
        logger.warning("UNICA save_unica_to_db: sin _pdf_bytes, no se puede parsear Tabelas 3-7")
        return False

    try:
        import io
        import pdfplumber as _pdf
        with _pdf.open(io.BytesIO(pdf_bytes)) as pdf:
            full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception as e:
        logger.error("UNICA save_unica_to_db: error leyendo PDF: %s", e)
        return False

    # Parsear Tabelas 3-7 (acumulado) y de-acumular
    t3 = _parse_cumulative_table(full_text, 3)  # caña (mil t)
    t4 = _parse_cumulative_table(full_text, 4)  # azúcar (mil t)
    t5 = _parse_cumulative_table(full_text, 5)  # etanol total (Ml)
    t6 = _parse_cumulative_table(full_text, 6)  # etanol anidro (Ml)
    t7 = _parse_cumulative_table(full_text, 7)  # etanol hidratado (Ml)

    cum_cane  = {seq: v["cs_cur"] for seq, v in t3.items()}
    cum_sugar = {seq: v["cs_cur"] for seq, v in t4.items()}
    cum_eth_t = {seq: v["cs_cur"] for seq, v in t5.items()}
    cum_eth_a = {seq: v["cs_cur"] for seq, v in t6.items()}
    cum_eth_h = {seq: v["cs_cur"] for seq, v in t7.items()}

    net_cane  = _decumulate(cum_cane)
    net_sugar = _decumulate(cum_sugar)
    net_eth_t = _decumulate(cum_eth_t)
    net_eth_a = _decumulate(cum_eth_a)
    net_eth_h = _decumulate(cum_eth_h)

    all_seqs = sorted(set(net_cane) | set(net_sugar))
    if not all_seqs:
        logger.warning("UNICA save_unica_to_db: Tabelas 3-7 vacías para %s", safra_norm)
        return False

    def _to_t(v_kt):
        return int(round(v_kt * 1000)) if v_kt is not None else None

    def _to_m3(v_ml):
        return int(round(v_ml * 1000)) if v_ml is not None else None

    saved = 0
    for seq in all_seqs:
        mon, day = _SEASON_FORTNIGHTS[seq - 1]
        year = safra_start if mon >= 4 else safra_start + 1
        try:
            from datetime import date as _date
            qdate = _date(year, mon, day)
        except ValueError:
            continue

        raw_cane  = net_cane.get(seq)
        raw_sugar = net_sugar.get(seq)

        # Validar rangos (mil t → t después de conversión)
        try:
            if raw_cane is not None:
                validate_range(raw_cane * 1000, min_value=0, max_value=80e6,
                               source="unica_pdf", field="cane_net_t", allow_none=False)
            if raw_sugar is not None:
                validate_range(raw_sugar * 1000, min_value=0, max_value=6e6,
                               source="unica_pdf", field="sugar_net_t", allow_none=False)
            if atr is not None:
                validate_range(atr, min_value=100, max_value=160,
                               source="unica_pdf", field="atr_kg_ton", allow_none=False)
        except Exception as ve:
            parse_log_warning("unica_pdf", f"seq={seq} {qdate}", str(ve))
            continue

        vals = {
            "safra": safra_norm, "qdate": qdate, "reg": "CS",
            "cane":  _to_t(raw_cane),
            "sugar": _to_t(raw_sugar),
            "eth_t": _to_m3(net_eth_t.get(seq)),
            "eth_a": _to_m3(net_eth_a.get(seq)),
            "eth_h": _to_m3(net_eth_h.get(seq)),
            "atr":   atr,
            "smix":  None,
            "emix":  emix,
            "src":   "pdf_unica",
        }

        existing = session.execute(
            text("SELECT id FROM unica_biweekly WHERE safra=:safra AND quinzena_date=:qdate AND region=:reg"),
            {"safra": safra_norm, "qdate": qdate, "reg": "CS"},
        ).fetchone()

        if existing:
            session.execute(
                text("""
                    UPDATE unica_biweekly SET
                        cane_crushed_t=:cane, sugar_t=:sugar,
                        ethanol_anidro_m3=:eth_a, ethanol_hidratado_m3=:eth_h,
                        ethanol_total_m3=:eth_t, atr_kg_ton=:atr,
                        eth_mix_pct=:emix, source=:src
                    WHERE safra=:safra AND quinzena_date=:qdate AND region=:reg
                """),
                vals,
            )
        else:
            session.execute(
                text("""
                    INSERT INTO unica_biweekly (
                        safra, quinzena_date, region,
                        cane_crushed_t, sugar_t,
                        ethanol_anidro_m3, ethanol_hidratado_m3, ethanol_total_m3,
                        atr_kg_ton, sugar_mix_pct, eth_mix_pct, source
                    ) VALUES (
                        :safra, :qdate, :reg,
                        :cane, :sugar,
                        :eth_a, :eth_h, :eth_t,
                        :atr, :smix, :emix, :src
                    )
                """),
                vals,
            )
        saved += 1
        logger.info("UNICA DB: %s CS seq=%d %s cane_net=%.1fkt sugar_net=%.1fkt",
                    safra_norm, seq, qdate,
                    raw_cane or 0, raw_sugar or 0)

    session.commit()
    logger.info("UNICA DB: %d quincenas guardadas para %s CS", saved, safra_norm)
    return saved > 0


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
        data["_pdf_bytes"] = pdf_bytes
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
