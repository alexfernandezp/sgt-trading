"""
Data Quality Gates — validators centralizados para ingestion y scoring.

Diseño:
  - Validators son funciones puras: retornan el valor si OK, raise DataQualityError si no.
  - DataQualityError carga contexto estructurado (source, field, value, expected) para logging.
  - El boundary del sistema (yfinance, scraping, GEE) es donde se aplica la validación.
  - Para parsers row-level que deben skip-and-continue, usar parse_log_warning() para
    estandarizar el logging de strings malformados.

Política Default:
  Validators raise. El caller decide:
    - propagar la excepción (pipeline-level: aborta esta fuente, continúa con la siguiente)
    - degradar con check_or_log() (row-level: skip la fila, sigue con la siguiente)

Compatible con el contrato de Arquitectura (Principio 3 — Data Quality Gates).
"""
import logging
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional, Union

logger = logging.getLogger(__name__)

DateLike = Union[date, datetime]


class DataQualityError(ValueError):
    """
    Excepción controlada para datos que violan un invariante de negocio.

    Atributos estructurados (extra context para logging):
      source   — módulo o fuente ('cepea', 'brazil_mapa', 'yfinance', ...)
      field    — nombre del campo ('price_usd', 'cane_crushed_t', ...)
      value    — valor recibido (puede ser None)
      expected — descripción humana de lo esperado ('[0.01, 2000.0]', '<= 30d old')
    """
    def __init__(self, message: str, *,
                 source: str, field: str,
                 value: Any = None, expected: Any = None):
        self.source = source
        self.field = field
        self.value = value
        self.expected = expected
        super().__init__(
            f"[{source}.{field}] {message} | got={value!r} expected={expected!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Validators
# ─────────────────────────────────────────────────────────────────────────────

def validate_range(
    value: Optional[float], *,
    min_value: float, max_value: float,
    source: str, field: str,
    allow_none: bool = False,
) -> Optional[float]:
    """
    Verifica que value esté en [min_value, max_value]. Retorna value si OK.

    allow_none=True: None se considera válido (pasa sin tocar).
    allow_none=False (default): None dispara DataQualityError.
    """
    if value is None:
        if allow_none:
            return None
        raise DataQualityError(
            "value is None", source=source, field=field,
            value=None, expected=f"[{min_value}, {max_value}]",
        )
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise DataQualityError(
            "value is not numeric", source=source, field=field,
            value=value, expected=f"float in [{min_value}, {max_value}]",
        )
    if not (min_value <= v <= max_value):
        raise DataQualityError(
            "value out of valid range", source=source, field=field,
            value=v, expected=f"[{min_value}, {max_value}]",
        )
    return v


def validate_freshness(
    timestamp: Optional[DateLike], *,
    max_age_days: int,
    source: str, field: str = "timestamp",
    reference: Optional[date] = None,
) -> DateLike:
    """
    Verifica que timestamp no esté más de max_age_days obsoleto vs reference (default: hoy).
    Retorna timestamp si OK. Raise DataQualityError si stale o None.
    """
    if timestamp is None:
        raise DataQualityError(
            "timestamp is None", source=source, field=field,
            value=None, expected=f"<= {max_age_days}d old",
        )
    ref = reference or date.today()
    ts_date = timestamp.date() if isinstance(timestamp, datetime) else timestamp
    age_days = (ref - ts_date).days
    if age_days > max_age_days:
        raise DataQualityError(
            f"data is {age_days}d stale",
            source=source, field=field,
            value=str(ts_date), expected=f"<= {max_age_days}d old (ref={ref})",
        )
    return timestamp


def validate_not_null(value: Any, *, source: str, field: str) -> Any:
    """
    Verifica que value no sea None ni string vacío. Retorna value si OK.
    """
    if value is None:
        raise DataQualityError(
            "value is None", source=source, field=field,
            value=None, expected="non-null",
        )
    if isinstance(value, str) and not value.strip():
        raise DataQualityError(
            "value is empty string", source=source, field=field,
            value=value, expected="non-empty string",
        )
    return value


def validate_count(
    n_valid: int, n_total: int, *,
    min_success_rate: float,
    source: str, field: str = "parse_success_rate",
) -> float:
    """
    Verifica que la tasa de parseo exitoso (n_valid/n_total) esté por encima de min_success_rate.
    Diseñado para detectar cambios estructurales en scraping (ej. MAPA cambia formato →
    parse_success_rate cae de 95% a 5% → DataQualityError).

    Retorna la tasa observada. Raise si por debajo del umbral.
    """
    if n_total == 0:
        raise DataQualityError(
            "no rows to evaluate", source=source, field=field,
            value=f"{n_valid}/{n_total}", expected=f"n_total > 0",
        )
    rate = n_valid / n_total
    if rate < min_success_rate:
        raise DataQualityError(
            f"parse success rate too low ({rate:.1%})",
            source=source, field=field,
            value=f"{n_valid}/{n_total}={rate:.1%}",
            expected=f">= {min_success_rate:.0%}",
        )
    return rate


# ─────────────────────────────────────────────────────────────────────────────
# Graceful degradation wrapper
# ─────────────────────────────────────────────────────────────────────────────

def check_or_log(
    validator: Callable[[], Any], *,
    on_error: str = "warn",
) -> tuple[Any, bool]:
    """
    Ejecuta validator(). Si lanza DataQualityError:
      on_error="raise" : propaga la excepción
      on_error="warn"  : logger.error con contexto, retorna (None, False)
      on_error="silent": retorna (None, False) sin log

    Returns: (resultado_validator, is_valid). Permite degradación elegante row-level
    sin contaminar el caller con try/except.

    Uso:
      value, ok = check_or_log(
          lambda: validate_range(price, min_value=0.01, max_value=2000.0,
                                 source="cepea", field="price_usd"),
          on_error="warn",
      )
      if not ok:
          continue  # skip esta fila, pipeline continúa
    """
    try:
        return validator(), True
    except DataQualityError as e:
        if on_error == "raise":
            raise
        if on_error == "warn":
            logger.error(
                "DataQualityError | source=%s field=%s value=%r expected=%r",
                e.source, e.field, e.value, e.expected,
            )
        return None, False


# ─────────────────────────────────────────────────────────────────────────────
# Parser-level logging helper (reemplaza except: return None silenciosos)
# ─────────────────────────────────────────────────────────────────────────────

def parse_log_warning(
    parser_name: str, raw_value: Any, exception: Exception,
    *, severity: str = "warning",
) -> None:
    """
    Logging estandarizado para fallos de parseo row-level que no deben abortar el pipeline.
    Reemplaza el patrón `except Exception: return None` silencioso.

    Convención:
      parser_name = "module._function" (ej. "cepea._parse_price")
      raw_value   = el string/valor que no se pudo parsear (truncado a 80 chars)
    """
    raw_str = repr(raw_value)[:80]
    log_fn = getattr(logger, severity, logger.warning)
    log_fn("parse_failed | parser=%s raw=%s exc=%s", parser_name, raw_str, exception)
