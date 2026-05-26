"""
CONAB — Boletim da Safra de Cana-de-Açúcar.

Fuente: gov.br/conab — Acompanhamento da Safra Brasileira.
Frecuencia: 4-6 levantamentos por temporada (abr–mar).
Ejecutar manualmente: py scripts/fetch_conab.py

URL pattern (Plone, PDF binary via @@display-file/file):
  BASE/{N}o-levantamento-safra-{YYYY}-{YY}/{file}.pdf/@@display-file/file
"""
import re
import logging
import requests
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

_BASE = (
    "https://www.gov.br/conab/pt-br/atuacao/informacoes-agropecuarias"
    "/safras/safra-de-cana-de-acucar/arquivos-boletins"
)
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SGTTrading/1.0)"}
_TIMEOUT = 45


# ── URL discovery ─────────────────────────────────────────────────────────────

def _candidate_urls(season_start: int, lev: int) -> list[str]:
    """
    Genera candidatos para el PDF de un levantamento dado.
    season_start=2025 → season 2025/26, short=26
    """
    short = str(season_start + 1)[2:]          # 2025 → "26"
    season_str = f"{season_start}-{short}"     # "2025-26"
    n = f"{lev}o"

    folder_variants = [
        f"{n}-levantamento-safra-{season_str}",
        f"{n}-levantamento-safra-{season_str}-1",
        f"{n}-levantamento-safra-{season_str}-2",
    ]
    file_variants = [
        f"e-book_boletim-de-safras-cana_{n}-lev-{season_str}.pdf",
        f"e-book_boletim-de-safras-cana_{n}-lev-{season_start}.pdf",
    ]

    urls = []
    for folder in folder_variants:
        for fname in file_variants:
            urls.append(f"{_BASE}/{folder}/{fname}/@@display-file/file")
    return urls


def _download_pdf(season_start: int, lev: int) -> tuple[bytes, str]:
    """
    Intenta descargar el PDF probando variantes de URL.
    Retorna (bytes, url_usada) o lanza RuntimeError.
    """
    for url in _candidate_urls(season_start, lev):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                logger.info("CONAB PDF: %d bytes desde %s", len(r.content), url)
                return r.content, url
        except requests.RequestException:
            pass
    raise RuntimeError(
        f"CONAB PDF no encontrado: season={season_start}/{season_start+1} lev={lev}"
    )


# ── PDF parser ────────────────────────────────────────────────────────────────

def _num(s: str) -> Optional[float]:
    """Convierte '44.283,7' o '(6,9)' a float. None si falla."""
    if not s:
        return None
    s = s.strip()
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    s = s.replace(".", "").replace(",", ".")
    try:
        v = float(s)
        return -v if negative else v
    except ValueError:
        return None


def _find_brasil_row(text: str, ncols: int = 10) -> Optional[list[str]]:
    """
    Busca la fila BRASIL en el texto de una tabla.
    Retorna lista de tokens numéricos o None.
    """
    for line in text.splitlines():
        if re.match(r"\s*BRASIL\b", line, re.IGNORECASE):
            tokens = re.findall(r"[\d.]+,\d+|\(\d[\d.,]*\)", line)
            if len(tokens) >= 4:
                return tokens
    return None


def _parse_pdf(pdf_bytes: bytes, season: str, lev: int, pub_date: date) -> dict:
    """
    Extrae métricas clave del PDF CONAB.
    Estrategia:
      1. Texto del resumen ejecutivo (pags 7-12) → totales via regex
      2. TABELA 1 (cana) → BRASIL row
      3. TABELA 2 (azúcar) → BRASIL row + revisión intra-season
      4. TABELA 3 (etanol) → BRASIL row + revisión
    """
    import io
    import pdfplumber

    result = {
        "season": season,
        "levantamento": lev,
        "pub_date": pub_date,
        # totals
        "cane_total_mt": None,
        "area_mha": None,
        "yield_kg_ha": None,
        "sugar_total_mt": None,
        "ethanol_cana_blt": None,
        "ethanol_total_blt": None,
        "ethanol_hydrous_blt": None,
        "ethanol_anhydrous_blt": None,
        # yoy
        "yoy_cane_pct": None,
        "yoy_sugar_pct": None,
        "yoy_ethanol_cana_pct": None,
        # revision vs prev lev
        "revision_sugar_pct": None,
        "revision_ethanol_pct": None,
        # state SP
        "sp_sugar_mt": None,
        "sp_cane_mt": None,
    }

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        full_text = "\n".join(
            (p.extract_text() or "") for p in pdf.pages
        )

        # ── 1. Resumen ejecutivo: totales via regex ────────────────────────
        # Caña total: "cana-de-açúcar estimada em 673,2 milhões de toneladas"
        # Patrón preciso: "cana-de-açúcar" seguido de estimativa/produção + número
        m = re.search(
            r"cana-de-a[cç][uú]car.*?estimad[ao]\s*em\s*([\d]+[.,][\d]+)\s*milh[oõ]es?\s*de\s*toneladas",
            full_text, re.IGNORECASE | re.DOTALL
        )
        if not m:
            # Fallback: número grande (>=500 Mt → solo puede ser cana) cerca de "produção"
            m = re.search(
                r"produ[cç][aã]o\s*de\s*((?:[5-9]\d{2}|[1-9]\d{3})[.,]\d+)\s*milh[oõ]es?\s*de\s*toneladas",
                full_text, re.IGNORECASE
            )
        if m:
            result["cane_total_mt"] = _num(m.group(1))

        # YoY caña: "redução de 0,5%" / "aumento de X,X%"
        m_red = re.search(
            r"cana-de-a[cç][uú]car.*?redu[cç][aã]o\s*de\s*([\d]+[.,]\d+)%",
            full_text, re.IGNORECASE | re.DOTALL
        )
        m_aum = re.search(
            r"cana-de-a[cç][uú]car.*?aumento\s*de\s*([\d]+[.,]\d+)%",
            full_text, re.IGNORECASE | re.DOTALL
        )
        if m_red:
            result["yoy_cane_pct"] = -abs(_num(m_red.group(1)) or 0)
        elif m_aum:
            result["yoy_cane_pct"] = abs(_num(m_aum.group(1)) or 0)

        # Area: "8.954,6 mil ha" → mha
        m = re.search(r"([\d]+\.[\d]+,\d)\s*mil\s*ha\b", full_text)
        if m:
            v = _num(m.group(1))
            if v:
                result["area_mha"] = round(v / 1000, 3)

        # Yield: "75.184 kg/ha" o "75.188 kg/ha"
        m = re.search(r"([\d]{2}\.[\d]{3})\s*kg/ha", full_text)
        if m:
            result["yield_kg_ha"] = _num(m.group(1).replace(".", ""))

        # Azúcar total: buscar "produção nacional de XX,XX milhões de toneladas de açúcar"
        # (el número viene ANTES de "de açúcar", no después)
        m = re.search(
            r"produ[cç][aã]o\s*nacional\s*de\s*([\d]+[.,][\d]+)\s*milh[oõ]es?\s*de\s*toneladas\s*de\s*a[cç][uú]car",
            full_text, re.IGNORECASE
        )
        if not m:
            # Alternativa: "produção de açúcar foi estimada em XX,XX milhões"
            m = re.search(
                r"produ[cç][aã]o\s*de\s*a[cç][uú]car\s*foi\s*estimad[ao]\s*em\s*([\d]+[.,][\d]+)\s*milh[oõ]es?",
                full_text, re.IGNORECASE
            )
        if not m:
            # Fallback genérico: "produção de X,X milhões de toneladas de açúcar"
            m = re.search(
                r"produ[cç][aã]o\s*de\s*([\d]+[.,][\d]+)\s*milh[oõ]es?\s*de\s*toneladas\s*de\s*a[cç][uú]car",
                full_text, re.IGNORECASE
            )
        if m:
            result["sugar_total_mt"] = _num(m.group(1))

        # YoY azúcar: "acréscimo de 0,1%" / "redução de X,X%"
        m = re.search(
            r"a[cç][uú]car.*?acr[eé]scimo\s*de\s*([\d]+[.,]\d+)%",
            full_text, re.IGNORECASE | re.DOTALL
        )
        if m:
            result["yoy_sugar_pct"] = abs(_num(m.group(1)) or 0)
        else:
            m = re.search(
                r"produ[cç][aã]o.*?a[cç][uú]car.*?redu[cç][aã]o\s*de\s*([\d]+[.,]\d+)%",
                full_text, re.IGNORECASE | re.DOTALL
            )
            if m:
                result["yoy_sugar_pct"] = -abs(_num(m.group(1)) or 0)

        # SP azúcar: "estimativa de produção paulista...é de 26,27" Mt
        m = re.search(
            r"(?:estimativa.*?paulista|S[aã]o\s*Paulo.*?chegou\s*a\s*produzir|estimativa.*?paulista.*?é\s*de)\s*([\d]+[.,][\d]+)\s*milh[oõ]es?",
            full_text, re.IGNORECASE | re.DOTALL
        )
        if m:
            result["sp_sugar_mt"] = _num(m.group(1))

        # Etanol de caña: "A estimativa é de 27,33 bilhões de litros, redução de 6,9%"
        # IMPORTANTE: esto aparece en contexto de "etanol derivado da cana-de-açúcar"
        m = re.search(
            r"estimativa\s*[eé]\s*de\s*([\d]+[.,][\d]+)\s*bilh[oõ]es?\s*de\s*litros,?\s*redu[cç][aã]o\s*de\s*([\d]+[.,]\d+)%",
            full_text, re.IGNORECASE
        )
        if m:
            result["ethanol_cana_blt"] = _num(m.group(1))
            result["yoy_ethanol_cana_pct"] = -abs(_num(m.group(2)) or 0)
        else:
            # Alternativa: buscar en contexto "etanol oriundo da cana"
            m = re.search(
                r"etanol\s*oriundo\s*da\s*cana.*?([\d]+[.,][\d]+)\s*bilh[oõ]es?",
                full_text, re.IGNORECASE | re.DOTALL
            )
            if m:
                result["ethanol_cana_blt"] = _num(m.group(1))

        # Etanol total (caña+maíz): "deve atingir 37,5 bilhões de litros, aumento de 0,8%"
        m = re.search(
            r"deve\s*atingir\s*([\d]+[.,][\d]+)\s*bilh[oõ]es?\s*de\s*litros",
            full_text, re.IGNORECASE
        )
        if m:
            result["ethanol_total_blt"] = _num(m.group(1))

        # Etanol hidratado: "17,21 bilhões de litros, redução de 9,8%"
        m = re.search(
            r"etanol\s*hidratado.*?([\d]+[.,][\d]+)\s*bilh[oõ]es?\s*de\s*litros",
            full_text, re.IGNORECASE | re.DOTALL
        )
        if m:
            result["ethanol_hydrous_blt"] = _num(m.group(1))

        # Etanol anidro: "10,12 bilhões de litros"
        m = re.search(
            r"etanol\s*anidro.*?([\d]+[.,][\d]+)\s*bilh[oõ]es?\s*de\s*litros",
            full_text, re.IGNORECASE | re.DOTALL
        )
        if m:
            result["ethanol_anhydrous_blt"] = _num(m.group(1))

        # ── 2. TABELA 2 — revisión azúcar (Lev.Anterior vs Lev.Actual) ────
        # Buscar página con TABELA 2
        for page in pdf.pages:
            ptext = page.extract_text() or ""
            if "TABELA 2" in ptext and "PRODU" in ptext and "A" in ptext:
                # Buscar fila BRASIL en el texto de la página
                brasil_tokens = _find_brasil_row(ptext)
                if brasil_tokens and len(brasil_tokens) >= 7:
                    # Columnas: (a)prev, (b)lev_ant, (c)lev_act, abs(c-a), %(c/a), abs(c-b), %(c/b)
                    try:
                        result["yoy_sugar_pct"] = _num(brasil_tokens[4])
                        result["revision_sugar_pct"] = _num(brasil_tokens[6])
                    except IndexError:
                        pass
                break

        # ── 3. TABELA 3 — revisión etanol (Lev.Anterior vs Lev.Actual) ────
        for page in pdf.pages:
            ptext = page.extract_text() or ""
            if "TABELA 3" in ptext and "ETANOL" in ptext:
                brasil_tokens = _find_brasil_row(ptext)
                if brasil_tokens and len(brasil_tokens) >= 7:
                    try:
                        result["yoy_ethanol_cana_pct"] = _num(brasil_tokens[4])
                        result["revision_ethanol_pct"] = _num(brasil_tokens[6])
                    except IndexError:
                        pass
                break

        # ── 4. TABELA 1 — SP cana ─────────────────────────────────────────
        for page in pdf.pages:
            ptext = page.extract_text() or ""
            if "TABELA 1" in ptext and "CANA" in ptext:
                for line in ptext.splitlines():
                    if re.match(r"\s*SP\b", line):
                        tokens = re.findall(r"[\d.]+,\d+", line)
                        if tokens:
                            v = _num(tokens[1]) if len(tokens) > 1 else _num(tokens[0])
                            if v:
                                result["sp_cane_mt"] = round(v / 1000, 2)
                        break

    return result


# ── DB upsert ─────────────────────────────────────────────────────────────────

def _upsert(session, data: dict, pdf_url: str) -> bool:
    """Inserta o actualiza el levantamento en la DB. Retorna True si es nuevo."""
    from models.market_data import ConabCanaLevantamento
    from sqlalchemy import select

    stmt = select(ConabCanaLevantamento).where(
        ConabCanaLevantamento.season == data["season"],
        ConabCanaLevantamento.levantamento == data["levantamento"],
    )
    existing = session.execute(stmt).scalar_one_or_none()

    if existing:
        # Actualizar
        for k, v in data.items():
            setattr(existing, k, v)
        existing.pdf_url = pdf_url
        existing.updated_at = datetime.utcnow()
        session.commit()
        return False
    else:
        row = ConabCanaLevantamento(**data, pdf_url=pdf_url)
        session.add(row)
        session.commit()
        return True


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_conab(session, season_start: int, levantamento: int,
                pub_date: Optional[date] = None) -> dict:
    """
    Descarga y parsea un levantamento CONAB.

    Args:
        session:       SQLAlchemy session
        season_start:  año inicio de temporada (ej. 2025 para 2025/26)
        levantamento:  número (1-6)
        pub_date:      fecha publicación (default: hoy)

    Returns dict con los datos parseados + is_new (bool).
    """
    if pub_date is None:
        pub_date = date.today()

    season = f"{season_start}/{str(season_start + 1)[2:]}"
    errors = []

    try:
        pdf_bytes, pdf_url = _download_pdf(season_start, levantamento)
    except RuntimeError as e:
        errors.append(str(e))
        return {"season": season, "levantamento": levantamento,
                "rows_upserted": 0, "errors": errors}

    try:
        data = _parse_pdf(pdf_bytes, season, levantamento, pub_date)
    except Exception as e:
        errors.append(f"Parse error: {e}")
        return {"season": season, "levantamento": levantamento,
                "rows_upserted": 0, "errors": errors, "pdf_url": pdf_url}

    is_new = _upsert(session, data, pdf_url)

    return {
        "season": season,
        "levantamento": levantamento,
        "is_new": is_new,
        "rows_upserted": 1,
        "cane_total_mt": data.get("cane_total_mt"),
        "sugar_total_mt": data.get("sugar_total_mt"),
        "ethanol_cana_blt": data.get("ethanol_cana_blt"),
        "yoy_cane_pct": data.get("yoy_cane_pct"),
        "yoy_sugar_pct": data.get("yoy_sugar_pct"),
        "revision_sugar_pct": data.get("revision_sugar_pct"),
        "revision_ethanol_pct": data.get("revision_ethanol_pct"),
        "pdf_url": pdf_url,
        "errors": errors,
    }


def get_latest_conab(session) -> Optional[dict]:
    """
    Lee el levantamento más reciente de la DB.
    Retorna dict o None.
    """
    from models.market_data import ConabCanaLevantamento
    from sqlalchemy import select

    stmt = (
        select(ConabCanaLevantamento)
        .order_by(
            ConabCanaLevantamento.season.desc(),
            ConabCanaLevantamento.levantamento.desc(),
        )
        .limit(1)
    )
    row = session.execute(stmt).scalar_one_or_none()
    if row is None:
        return None
    return {
        "season": row.season,
        "levantamento": row.levantamento,
        "pub_date": str(row.pub_date),
        "cane_total_mt": row.cane_total_mt,
        "sugar_total_mt": row.sugar_total_mt,
        "ethanol_cana_blt": row.ethanol_cana_blt,
        "yoy_cane_pct": row.yoy_cane_pct,
        "yoy_sugar_pct": row.yoy_sugar_pct,
        "revision_sugar_pct": row.revision_sugar_pct,
        "revision_ethanol_pct": row.revision_ethanol_pct,
        "sp_sugar_mt": row.sp_sugar_mt,
        "sp_cane_mt": row.sp_cane_mt,
    }
