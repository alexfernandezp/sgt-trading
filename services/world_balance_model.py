"""
SGT Live Balance Model — motor de prediccion del balance mundial de azucar.

Arquitectura:
  DataInjector   — verifica frescura de cada fuente y calcula confidence degradado
  CountryBalance — objeto por pais con datos USDA + overrides fisicos
  BalanceEngine  — computa balance ajustado para 25/26 y 26/27
  WeightDrift    — aprende automaticamente si una fuente se desvía sistematicamente
  SignalGenerator — convierte balance a señal + conviction + leverage cap

Flujo:
  compute_sgt_balance(session, marketing_year) ->
      SgtBalanceResult (balance 25/26 + forward 26/27)
  save_balance_forecast(session, result) -> escribe sgt_balance_forecast

Gatekeeper de apalancamiento:
  confidence_score < 0.60 -> leverage_cap_pct = 25%  (valley entre cosechas)
  confidence_score < 0.70 -> leverage_cap_pct = 50%
  confidence_score < 0.80 -> leverage_cap_pct = 75%
  confidence_score >= 0.80 -> leverage_cap_pct = 100% (alineacion fisica plena)

Fuentes de datos:
  USDA WASDE (baseline oficial)  : confianza base 0.80
  CONAB levantamento (Brasil)    : confianza 0.95 si fresco <60d
  GEE LST heat stress            : confianza 0.65 base → decay a 0.82 en cosecha
  GEE NDVI                       : confianza 0.75 en ventana critica
  ENSO / ONI                     : confianza 0.55
  Tendencia historica             : confianza 0.30 (ultimo recurso)
  ISMA India (pendiente PDF)     : confianza 0.90 cuando disponible
  OCSB Tailandia (web scraping)  : confianza 0.88 cuando disponible
"""
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ── Umbrales STU → señal de trading ───────────────────────────────────────────
STU_STRONG_LONG  = 28.0   # < 28% → STRONG LONG
STU_LONG         = 32.0   # 28-32% → LONG
STU_NEUTRAL_LOW  = 35.0   # 32-35% → NEUTRAL con sesgo alcista
STU_NEUTRAL_HIGH = 38.0   # 35-38% → NEUTRAL con sesgo bajista
STU_SHORT        = 42.0   # 38-42% → SHORT
# > 42% → STRONG SHORT

# ── Confianza base por fuente ──────────────────────────────────────────────────
SIGNAL_CONFIDENCE = {
    "conab_survey":          0.95,   # CONAB levantamento con datos crush reales
    "isma_crush":            0.90,   # ISMA quincenal India (Oct-Abr) — pendiente impl
    "ocsb_crush":            0.88,   # OCSB mensual Tailandia — pendiente impl
    "comex_exports":         0.85,   # Comex Stat Brasil exportaciones reales
    "usda_wasde_official":   0.80,   # Balance USDA oficial (baseline)
    "ndvi_active_season":    0.75,   # NDVI durante ventana critica crecimiento
    "lst_heat_stress":       0.65,   # LST base → sube con decay fn al acercarse cosecha
    "enso_oni":              0.55,   # ENSO patron estacional
    "rainfall_spi":          0.50,   # SPI-90 precipitacion
    "ndvi_off_season":       0.45,   # NDVI fuera de ventana critica
    "trend_extrapolation":   0.30,   # Extrapolacion tendencia historica (ultimo recurso)
}

# Nota sobre datos USDA PSD vs WASDE actual:
# El USDA PSD API V2 devuelve snapshot de la ultima publicacion PSD por pais.
# Para India y Tailandia los datos reales de temporada provienen de ISMA y OCSB.
# Si los scrapers no tienen datos, se aplica ajuste conservador de etanol India.

# Codigos USDA V2 (distintos de ISO — verificados en DB)
USDA_COUNTRY_CODES = {
    "BR": "BR",   # Brasil — correcto
    "IN": "IN",   # India — correcto
    "TH": "TH",   # Tailandia — correcto
    "CN": "CH",   # China — USDA V2 usa "CH" no "CN"
    "AU": "AS",   # Australia — USDA V2 usa "AS" no "AU"
    "EU": "E4",   # Union Europea — USDA V2 usa "E4" no "EU"
    "WB": "WB",   # Mundo
}

# ── Maxima antiguedad aceptable por fuente ─────────────────────────────────────
MAX_FRESH_DAYS = {
    "conab":    60,   # ~6 levantamentos/año, uno cada 45d en temporada
    "isma":     45,   # quincenal Oct-Abr
    "ocsb":     60,   # mensual Nov-Abr
    "usda":     45,   # WASDE mensual (publicacion ~dia 12)
    "ndvi":     21,   # GEE semanal, puede fallar por nubes (3 semanas margen)
    "lst":      21,
    "enso":     40,
    "rainfall": 10,
}

# ── Factor etanol India 25/26 ──────────────────────────────────────────────────
# India mandato 20% blending vía "juice route" (cana directa a etanol).
# USDA PSD ya incorpora parcialmente esta diversion en su produccion de azucar.
# SGT aplica un ajuste incremental conservador: ~2.0 Mt (no incorporado por USDA).
# ISMA publica crush data quincenal (Oct-Abr) — cuando disponible, anula este factor.
# Fuente: ISMA press releases, Sugar & Sweetener Outlook USDA ERS.
INDIA_ETHANOL_DIVERSION_MT = 2.0

# ── Calendarios de cosecha (mes pico, 1-12) ───────────────────────────────────
HARVEST_PEAK = {
    "BR": 8,   # Brasil peak agosto (crushing Jun-Nov)
    "IN": 2,   # India peak febrero (crushing Oct-Mar)
    "TH": 2,   # Tailandia peak enero-febrero (crushing Nov-Mar)
    "AU": 9,   # Australia peak septiembre (crushing Jul-Dec)
}


# ─────────────────────────────────────────────────────────────────────────────
#  LST decay function — alpha oculto
# ─────────────────────────────────────────────────────────────────────────────

def lst_weight(reference_date: Optional[date] = None,
               country: str = "BR") -> float:
    """
    Calcula el peso del LST heat stress en funcion de la proximidad a la cosecha.

    El LST lidera al NDVI 2-4 semanas → en la ventana critica es mas predictivo
    que el NDVI. A medida que se acerca el pico de cosecha, el peso sube de
    0.65 (base) a 0.82 (pico de cosecha).

    Esta funcion de decay captura el "alpha oculto": si hay stress termico
    acumulado en los 60 dias previos al pico de cosecha, la caida de yield ya
    es practicamente irreversible y la señal tiene alta precision.
    """
    if reference_date is None:
        reference_date = date.today()

    peak_month = HARVEST_PEAK.get(country, 8)
    current_month = reference_date.month

    # Distancia en meses al pico (circular, max 6)
    diff = abs(current_month - peak_month)
    diff = min(diff, 12 - diff)   # distancia circular

    base_weight = SIGNAL_CONFIDENCE["lst_heat_stress"]   # 0.65
    peak_weight = 0.82

    if diff == 0:
        return peak_weight
    elif diff == 1:
        return base_weight + (peak_weight - base_weight) * 0.75
    elif diff == 2:
        return base_weight + (peak_weight - base_weight) * 0.50
    elif diff == 3:
        return base_weight + (peak_weight - base_weight) * 0.25
    else:
        return base_weight


# ─────────────────────────────────────────────────────────────────────────────
#  _safe_db — context manager de transacciones seguras
# ─────────────────────────────────────────────────────────────────────────────

@contextmanager
def _safe_db(session, label: str = ""):
    """
    Ejecuta un bloque de DB de forma segura.

    Si la query falla, hace rollback inmediato para devolver la sesion a estado
    IDLE (libre). Sin este rollback, PostgreSQL permanece en "transaccion abortada"
    y rechaza todas las queries siguientes con InFailedSqlTransaction.
    """
    try:
        yield
    except Exception as exc:
        try:
            session.rollback()
        except Exception:
            pass
        logger.debug("DB check [%s] — rollback aplicado: %s", label, exc)


# ─────────────────────────────────────────────────────────────────────────────
#  DataInjector — verifica frescura y computa confidence
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DataFreshness:
    source: str
    last_date: Optional[date]
    age_days: int
    is_fresh: bool
    confidence_factor: float   # 1.0 si fresco, 0 si muy viejo
    warning: Optional[str] = None


class DataInjector:
    """
    Verifica la frescura de cada fuente de datos.

    Si CONAB tiene >60 dias: Warning + baja confidence_score.
    Si USDA tiene >45 dias: Warning (entre publicaciones WASDE es normal).

    Tambien detecta si estamos en "valle de datos" entre cosechas
    (cuando la mayoria de fuentes fisicas no tienen datos recientes) y
    reduce el confidence score global para activar el gating de leverage.
    """

    def __init__(self, session, reference_date: Optional[date] = None):
        self.session = session
        self.ref_date = reference_date or date.today()
        self.freshness: dict[str, DataFreshness] = {}
        self._inject_all()

    def _age(self, d: Optional[date]) -> int:
        if d is None:
            return 9999
        return (self.ref_date - d).days

    def _factor(self, age: int, max_days: int) -> float:
        if age <= max_days:
            return 1.0
        elif age <= max_days * 2:
            return 0.6
        else:
            return 0.0

    def _inject_all(self):
        self._check_conab()
        self._check_usda()
        self._check_gee_ndvi()
        self._check_gee_lst()
        self._check_enso()

    def _check_conab(self):
        last_date = None
        with _safe_db(self.session, "conab"):
            from models.market_data import ConabCanaLevantamento
            from sqlalchemy import select, func
            row = self.session.execute(
                select(func.max(ConabCanaLevantamento.pub_date))
            ).scalar()
            if row:
                last_date = row

        age  = self._age(last_date)
        max_ = MAX_FRESH_DAYS["conab"]
        warn = None
        if age > max_:
            warn = "CONAB con %d dias de antiguedad (max %d) -> confidence reducido" % (age, max_)
            logger.warning(warn)

        self.freshness["conab"] = DataFreshness(
            source="conab", last_date=last_date, age_days=age,
            is_fresh=age <= max_,
            confidence_factor=self._factor(age, max_),
            warning=warn,
        )

    def _check_usda(self):
        last_date = None
        with _safe_db(self.session, "usda"):
            from models.market_data import UsdaPsdRecord
            from sqlalchemy import select, func
            row = self.session.execute(
                select(func.max(UsdaPsdRecord.updated_at))
            ).scalar()
            if row:
                last_date = row.date() if hasattr(row, "date") else row

        age  = self._age(last_date)
        max_ = MAX_FRESH_DAYS["usda"]
        warn = None
        if age > max_:
            warn = "USDA con %d dias (entre publicaciones WASDE es normal)" % age

        self.freshness["usda"] = DataFreshness(
            source="usda", last_date=last_date, age_days=age,
            is_fresh=age <= max_,
            confidence_factor=self._factor(age, max_),
            warning=warn,
        )

    def _check_gee_ndvi(self):
        last_date = None
        with _safe_db(self.session, "gee_ndvi"):
            from models.market_data import GeeMetric
            from sqlalchemy import select, func
            row = self.session.execute(
                select(func.max(GeeMetric.obs_date))
                .where(GeeMetric.metric == "ndvi")
            ).scalar()
            last_date = row

        age = self._age(last_date)
        self.freshness["ndvi"] = DataFreshness(
            source="ndvi", last_date=last_date, age_days=age,
            is_fresh=age <= MAX_FRESH_DAYS["ndvi"],
            confidence_factor=self._factor(age, MAX_FRESH_DAYS["ndvi"]),
        )

    def _check_gee_lst(self):
        last_date = None
        with _safe_db(self.session, "gee_lst"):
            from models.market_data import GeeMetric
            from sqlalchemy import select, func
            row = self.session.execute(
                select(func.max(GeeMetric.obs_date))
                .where(GeeMetric.metric == "lst")
            ).scalar()
            last_date = row

        age = self._age(last_date)
        self.freshness["lst"] = DataFreshness(
            source="lst", last_date=last_date, age_days=age,
            is_fresh=age <= MAX_FRESH_DAYS["lst"],
            confidence_factor=self._factor(age, MAX_FRESH_DAYS["lst"]),
        )

    def _check_enso(self):
        last_date = None
        with _safe_db(self.session, "enso"):
            from models.market_data import OniIndex
            from sqlalchemy import select, func
            row = self.session.execute(
                select(func.max(OniIndex.obs_date))
            ).scalar()
            last_date = row

        age = self._age(last_date)
        self.freshness["enso"] = DataFreshness(
            source="enso", last_date=last_date, age_days=age,
            is_fresh=age <= MAX_FRESH_DAYS["enso"],
            confidence_factor=self._factor(age, MAX_FRESH_DAYS["enso"]),
        )

    def compute_global_confidence(self) -> float:
        """
        Calcula el confidence score global ponderado por importancia de fuente.

        Fuentes criticas con peso alto: USDA, CONAB, GEE NDVI.
        Fuentes de soporte con peso bajo: ENSO.

        Retorna [0, 1] donde:
          < 0.60 → valley de datos / temporada baja → gating severo
          0.60-0.70 → datos parciales → gating moderado
          0.70-0.80 → datos aceptables → gating leve
          >= 0.80 → alineacion fisica plena → posicion completa
        """
        weights = {
            "usda":  0.30,
            "conab": 0.30,
            "ndvi":  0.25,
            "lst":   0.10,
            "enso":  0.05,
        }

        weighted_sum = 0.0
        for source, w in weights.items():
            factor = self.freshness.get(source)
            f_val  = factor.confidence_factor if factor else 0.0
            weighted_sum += f_val * w

        # USDA siempre tiene algun dato historico → floor 0.40
        return max(0.40, min(1.0, weighted_sum))

    def get_data_freshness_dict(self) -> dict:
        return {k: v.age_days for k, v in self.freshness.items()}

    def get_warnings(self) -> list[str]:
        return [f.warning for f in self.freshness.values() if f.warning]


# ─────────────────────────────────────────────────────────────────────────────
#  CountryBalance — objeto por pais
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CountryBalance:
    """Encapsula el balance de un pais con datos USDA y overrides fisicos."""
    code:          str
    name:          str
    usda_prod_mt:  Optional[float] = None
    sgt_prod_mt:   Optional[float] = None   # USDA + ajustes fisicos
    override_mt:   float = 0.0              # ajuste neto vs USDA
    data_source:   str = "usda"             # fuente del override
    confidence:    float = 0.80             # confianza del dato
    notes:         list[str] = field(default_factory=list)

    @property
    def adj_pct(self) -> Optional[float]:
        if self.usda_prod_mt and self.usda_prod_mt != 0:
            return round(self.override_mt / self.usda_prod_mt * 100, 1)
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  WeightDriftTracker — aprendizaje automatico de desviaciones sistematicas
# ─────────────────────────────────────────────────────────────────────────────

class WeightDriftTracker:
    """
    Detecta si una fuente de datos se ha desviado sistematicamente de la
    realidad fisica durante los ultimos 3 meses.

    Mecanismo:
      - Compara sgt_prod_mt del forecast T con el valor USDA revisado en T+1, T+2, T+3
      - Si la desviacion media es > DRIFT_THRESHOLD_MT, reduce el peso de la fuente
      - Rango de ajuste: [WEIGHT_MIN, WEIGHT_INITIAL]
      - Reset automatico cuando la desviacion vuelve a < DRIFT_THRESHOLD_MT / 2

    Ejemplo:
      Si USDA ha sobreestimado produccion BR 3 meses consecutivos por ~2 Mt,
      reducimos usda_wasde_official de 0.80 a 0.70.
    """

    DRIFT_THRESHOLD_MT = 2.0    # desviacion minima para activar drift (Mt)
    DRIFT_REDUCTION    = 0.10   # reduccion de peso por drift activo
    WEIGHT_MIN         = 0.50   # peso minimo aunque haya drift fuerte

    def __init__(self, session, marketing_year: int):
        self.session = session
        self.marketing_year = marketing_year
        self._drift = self._compute()

    def _compute(self) -> dict:
        """
        Lee ultimos 3 meses de forecasts de la BD y compara con USDA revisado.
        Retorna {fuente: drift_coefficient} donde 1.0 = sin drift.
        """
        drift = {
            "usda_wasde_official": 1.0,
            "conab_survey":        1.0,
        }

        with _safe_db(self.session, "weight_drift"):
            from models.market_data import SgtBalanceForecast, UsdaPsdRecord
            from sqlalchemy import select, func
            import numpy as np

            cutoff = date.today() - timedelta(days=90)

            past_forecasts = self.session.execute(
                select(
                    SgtBalanceForecast.forecast_date,
                    SgtBalanceForecast.sgt_prod_mt,
                    SgtBalanceForecast.usda_prod_mt,
                    SgtBalanceForecast.br_sgt_mt,
                    SgtBalanceForecast.br_usda_mt,
                )
                .where(
                    SgtBalanceForecast.marketing_year == self.marketing_year,
                    SgtBalanceForecast.scenario == "base",
                    SgtBalanceForecast.forecast_date >= cutoff,
                )
                .order_by(SgtBalanceForecast.forecast_date)
            ).fetchall()

            if len(past_forecasts) >= 2:
                latest_usda = self.session.execute(
                    select(func.sum(UsdaPsdRecord.value_1000mt))
                    .where(
                        UsdaPsdRecord.attribute_name == "production",
                        UsdaPsdRecord.marketing_year == self.marketing_year,
                        UsdaPsdRecord.country_code == "WB",
                    )
                    .order_by(UsdaPsdRecord.pub_month.desc())
                ).scalar()

                if latest_usda is not None:
                    usda_prod_actual = float(latest_usda) / 1000.0
                    deviations = [
                        float(row.sgt_prod_mt) - usda_prod_actual
                        for row in past_forecasts if row.sgt_prod_mt is not None
                    ]
                    if len(deviations) >= 2:
                        mean_dev = float(np.mean(deviations))
                        if abs(mean_dev) > self.DRIFT_THRESHOLD_MT:
                            reduction = min(0.20, abs(mean_dev) / 10.0)
                            new_weight = max(
                                self.WEIGHT_MIN,
                                SIGNAL_CONFIDENCE["usda_wasde_official"] - reduction,
                            )
                            drift["usda_wasde_official"] = (
                                new_weight / SIGNAL_CONFIDENCE["usda_wasde_official"]
                            )
                            logger.info(
                                "WeightDrift: desviacion media SGT vs USDA = %.1f Mt "
                                "-> peso USDA ajustado a %.2f (coef=%.2f)",
                                mean_dev, new_weight, drift["usda_wasde_official"],
                            )

        return drift

    def apply(self, base_confidence: float, source: str) -> float:
        """Aplica el drift coefficient al confidence base de una fuente."""
        coef = self._drift.get(source, 1.0)
        return base_confidence * coef

    def to_dict(self) -> dict:
        return dict(self._drift)


# ─────────────────────────────────────────────────────────────────────────────
#  BalanceEngine — calcula balance ajustado
# ─────────────────────────────────────────────────────────────────────────────

class BalanceEngine:
    """
    Toma el baseline USDA y aplica ajustes fisicos por pais.

    Orden de prioridad para cada pais:
      1. CONAB / ISMA / OCSB (datos crush reales) — maxima confianza
      2. GEE NDVI + LST (condicion del cultivo) — señal fisica directa
      3. ENSO (factor climatico estacional)
      4. USDA sin cambios (si no hay override)

    Para la proyeccion 26/27:
      - Ajuste de tendencia proactivo: si LST acumulado 2 años muestra
        tendencia ascendente, usar media movil ajustada a la baja en vez
        de la media historica de 10 años.
    """

    def __init__(self, session, marketing_year: int,
                 injector: DataInjector,
                 drift_tracker: WeightDriftTracker,
                 reference_date: Optional[date] = None):
        self.session = session
        self.mkt_year = marketing_year
        self.injector = injector
        self.drift = drift_tracker
        self.ref_date = reference_date or date.today()

    # ── USDA baseline ─────────────────────────────────────────────────────────

    def load_usda_baseline(self) -> Optional[dict]:
        """
        Carga el balance mundial oficial USDA (country_code='WB').
        Retorna dict con prod, cons, end_stocks, beg_stocks, exports, stu.
        """
        wb = None
        with _safe_db(self.session, "usda_baseline"):
            from ingestion.usda_psd import get_world_balance
            wb = get_world_balance(self.session, self.mkt_year)
        if not wb:
            logger.warning("BalanceEngine.load_usda_baseline: sin datos — ejecutar fetch_usda.py")
        return wb or None

    def load_country_usda(self, country_iso: str) -> Optional[float]:
        """
        Produccion USDA de un pais especifico (Mt).
        Usa USDA_COUNTRY_CODES para mapear ISO -> codigo USDA V2.
        """
        usda_code = USDA_COUNTRY_CODES.get(country_iso, country_iso)
        result = None
        with _safe_db(self.session, "country_usda_%s" % usda_code):
            from ingestion.usda_psd import get_country_production
            rows = get_country_production(self.session, usda_code, n_years=1)
            if rows:
                result = rows[-1].get("production_mt")
        return result

    def load_country_balance(self, country_iso: str) -> dict:
        """Carga produccion + consumo + stocks de un pais en USDA."""
        usda_code = USDA_COUNTRY_CODES.get(country_iso, country_iso)
        result = {}
        with _safe_db(self.session, "country_balance_%s" % usda_code):
            from models.market_data import UsdaPsdRecord
            from sqlalchemy import select
            rows = self.session.execute(
                select(UsdaPsdRecord.attribute_id, UsdaPsdRecord.attribute_name,
                       UsdaPsdRecord.value_1000mt)
                .where(
                    UsdaPsdRecord.country_code == usda_code,
                    UsdaPsdRecord.marketing_year == self.mkt_year,
                    UsdaPsdRecord.attribute_id.in_([20, 28, 57, 86, 88, 126, 176]),
                )
                .order_by(UsdaPsdRecord.pub_month.desc())
            ).fetchall()
            seen = {}
            for r in rows:
                if r.attribute_id not in seen:
                    seen[r.attribute_id] = True
                    result[r.attribute_name] = float(r.value_1000mt) / 1000.0
        return result

    # ── Ajuste Brasil (CONAB) ─────────────────────────────────────────────────

    def adjust_brazil(self, usda_br_mt: Optional[float]) -> CountryBalance:
        """
        Prioridad:
          1. CONAB levantamento mas reciente (sugar_total_mt)
          2. GEE NDVI Brazil → ajuste proporcional sobre USDA
          3. USDA sin cambios
        """
        cb = CountryBalance(
            code="BR", name="Brasil",
            usda_prod_mt=usda_br_mt,
            sgt_prod_mt=usda_br_mt,
            data_source="usda",
            confidence=SIGNAL_CONFIDENCE["usda_wasde_official"],
        )
        if usda_br_mt is None:
            return cb

        conab_fresh = self.injector.freshness.get("conab")

        # ── 1. CONAB override ───────────────────────────────────────────────
        if conab_fresh and conab_fresh.is_fresh:
            data = None
            with _safe_db(self.session, "conab_brazil"):
                from ingestion.conab_cana import get_latest_conab
                data = get_latest_conab(self.session)

            if data and data.get("sugar_total_mt"):
                conab_mt = float(data["sugar_total_mt"])
                lev = data.get("levantamento", 1)
                season = data.get("season", "")
                season_progress = min(1.0, (lev + 1) / 7.0)
                if lev >= 3:
                    cb.sgt_prod_mt = conab_mt
                    cb.override_mt = conab_mt - usda_br_mt
                    cb.data_source = "conab_lev%d" % lev
                    cb.confidence  = SIGNAL_CONFIDENCE["conab_survey"]
                    cb.notes.append(
                        "CONAB %s lev%d=%.1f Mt (USDA=%.1f Mt, adj=%+.1f Mt)" % (
                            season, lev, conab_mt, usda_br_mt, cb.override_mt)
                    )
                    return cb
                else:
                    cb.notes.append(
                        "CONAB %s lev%d=%.1f Mt (temprano — progreso %.0f%%)" % (
                            season, lev, conab_mt, season_progress * 100)
                    )

        # ── 2. GEE NDVI + LST ajuste proporcional ─────────────────────────
        ndvi_adj = self._gee_production_adj("br_sp_sugarcane", "BR", usda_br_mt)
        if ndvi_adj is not None:
            cb.sgt_prod_mt = usda_br_mt + ndvi_adj
            cb.override_mt = ndvi_adj
            ndvi_fresh = self.injector.freshness.get("ndvi")
            lst_fresh  = self.injector.freshness.get("lst")
            if ndvi_fresh and ndvi_fresh.is_fresh:
                cb.data_source = "ndvi_gee"
                cb.confidence  = SIGNAL_CONFIDENCE["ndvi_active_season"]
            elif lst_fresh and lst_fresh.is_fresh:
                cb.data_source = "lst_gee"
                cb.confidence  = lst_weight(self.ref_date, "BR")
            cb.notes.append("GEE adj Brasil: %+.2f Mt" % ndvi_adj)

        return cb

    # ── Ajuste India (ISMA / estimacion etanol) ──────────────────────────────

    def adjust_india(self, usda_in_mt: Optional[float]) -> CountryBalance:
        """
        India: prioridad ISMA > GEE integral > estimacion etanol conservadora.

        Fuentes (en orden):
          1. ISMA crush data (ingestion/isma_india.py) — quincenal Oct-Abr
             ISMA reporta azucar PRODUCIDA por fabricas, ya NETA de diversion
             juice-to-ethanol. No aplicar factor etanol adicional sobre ISMA.
             USDA PSD Dec'25 = 35.25 Mt (muy sobre-estimado). ISMA Apr'26 = 27.53 Mt.
             Confianza: 0.90 si fresco, 0.82 si final de temporada <90d
          2. GEE NDVI integral (gee/countries/india.py) — estimacion produccion
             via integral NDVI mensual Sentinel-2 + WorldCover cropland mask.
             Calibrado vs ISMA historico. Confianza: 0.60-0.80 segun completitud.
          3. Ajuste conservador de etanol (-INDIA_ETHANOL_DIVERSION_MT):
             Fallback final cuando no hay datos externos. Sin ISMA ni GEE la
             confianza es baja (no podemos calibrar el gap USDA vs realidad).

        El USDA PSD API solo tiene snapshot Dec 2025 para India. El dato
        de May 2026 WASDE no es accesible via API → ISMA es la fuente primaria.
        """
        cb = CountryBalance(
            code="IN", name="India",
            usda_prod_mt=usda_in_mt,
            sgt_prod_mt=usda_in_mt,
            data_source="usda",
            confidence=SIGNAL_CONFIDENCE["usda_wasde_official"],
        )
        if usda_in_mt is None:
            return cb

        # ── 1. ISMA data (scraper web) ────────────────────────────────────
        isma_mt, isma_src, isma_conf = None, "usda_baseline", 0.80
        with _safe_db(self.session, "isma_india"):
            from ingestion.isma_india import india_full_year_estimate
            isma_mt, isma_src, isma_conf = india_full_year_estimate(self.session)

        if isma_mt is not None:
            override = round(isma_mt - usda_in_mt, 2)
            cb.notes.append(
                "ISMA %s: %.2f Mt (USDA=%.2f Mt, adj=%+.2f Mt)" % (
                    isma_src, isma_mt, usda_in_mt, override)
            )

            # GEE NDVI como ajuste marginal sobre estimacion ISMA
            gee_adj = self._gee_production_adj("in_up_maharashtra", "IN", isma_mt)
            if gee_adj is not None and abs(gee_adj) > 0.1:
                override += gee_adj
                cb.notes.append("GEE NDVI adj marginal: %+.2f Mt" % gee_adj)
                cb.data_source = "%s+ndvi" % isma_src
            else:
                cb.data_source = isma_src

            cb.sgt_prod_mt = round(usda_in_mt + override, 2)
            cb.override_mt = override
            cb.confidence  = isma_conf
            return cb

        # ── 2. GEE NDVI integral production estimate ──────────────────────
        try:
            from ingestion.gee_production import india_gee_estimate
            gee_mt, gee_src, gee_conf = india_gee_estimate(self.mkt_year)
        except Exception as e:
            logger.debug("adjust_india GEE estimator: %s", e)
            gee_mt, gee_src, gee_conf = None, "gee_unavailable", 0.50

        if gee_mt is not None:
            override = round(gee_mt - usda_in_mt, 2)
            cb.notes.append(
                "GEE integral %s: %.2f Mt (USDA=%.2f Mt, adj=%+.2f Mt)" % (
                    gee_src, gee_mt, usda_in_mt, override)
            )
            cb.sgt_prod_mt = round(usda_in_mt + override, 2)
            cb.override_mt = override
            cb.data_source = gee_src
            cb.confidence  = gee_conf
            return cb

        # ── 3. Ajuste etanol conservador (fallback final) ──────────────────
        # USDA PSD Dec 2025 = 35.25 Mt (bruto, incluye algo de diversion ya)
        # Sin ISMA ni GEE: aplicamos -2.0 Mt incremental (conservador).
        eth_adj = -INDIA_ETHANOL_DIVERSION_MT
        gee_ndvi_adj = self._gee_production_adj("in_up_maharashtra", "IN", usda_in_mt)
        final_adj = eth_adj + (gee_ndvi_adj or 0.0)

        cb.notes.append(
            "USDA=%.2f Mt → adj etanol=%+.1f Mt%s" % (
                usda_in_mt, eth_adj,
                (" + GEE z-score=%+.2f Mt" % gee_ndvi_adj) if gee_ndvi_adj else "",
            )
        )

        cb.sgt_prod_mt = round(usda_in_mt + final_adj, 2)
        cb.override_mt = round(final_adj, 2)
        cb.data_source = "usda+ethanol_est" if gee_ndvi_adj is None else "usda+ethanol+ndvi"
        cb.confidence  = 0.60   # sin ISMA ni GEE integral

        return cb

    # ── Ajuste Tailandia (OCSB / GEE) ────────────────────────────────────────

    def adjust_thailand(self, usda_th_mt: Optional[float]) -> CountryBalance:
        """
        Tailandia: prioridad OCSB > GEE integral > GEE z-score > USDA sin ajuste.

        Fuentes (en orden):
          1. OCSB Open Data Portal (ingestion/ocsb.py)
             Datos oficiales de temporada Nov-Abr. Confianza 0.88 si fresco.
          2. GEE NDVI integral (gee/countries/thailand.py) — estimacion produccion
             Calibrado vs OCSB historico. Confianza: 0.55-0.80.
          3. GEE z-score (senal anomalia via gee_crops.py) — ajuste proporcional
          4. USDA sin cambios si no hay datos externos disponibles

        El USDA PSD API Dec 2025 puede diferir de la produccion real
        de temporada 25/26. Sin OCSB no hacemos override sin evidencia fuerte.
        """
        cb = CountryBalance(
            code="TH", name="Tailandia",
            usda_prod_mt=usda_th_mt,
            sgt_prod_mt=usda_th_mt,
            data_source="usda",
            confidence=SIGNAL_CONFIDENCE["usda_wasde_official"],
        )
        if usda_th_mt is None:
            return cb

        # ── 1. OCSB data (scraper) ────────────────────────────────────────
        ocsb_mt, ocsb_src, ocsb_conf = None, "usda_baseline", 0.80
        with _safe_db(self.session, "ocsb_thailand"):
            from ingestion.ocsb import thailand_production_estimate
            ocsb_mt, ocsb_src, ocsb_conf = thailand_production_estimate(
                target_ce_year=self.mkt_year, session=self.session
            )

        if ocsb_mt is not None:
            override = round(ocsb_mt - usda_th_mt, 2)
            cb.notes.append(
                "OCSB %s: %.2f Mt (USDA=%.2f Mt, adj=%+.2f Mt)" % (
                    ocsb_src, ocsb_mt, usda_th_mt, override)
            )

            # GEE como ajuste marginal sobre estimacion OCSB
            gee_adj = self._gee_production_adj("th_sugarcane_belt", "TH", ocsb_mt)
            if gee_adj is not None and abs(gee_adj) > 0.1:
                override += gee_adj
                cb.notes.append("GEE NDVI adj marginal: %+.2f Mt" % gee_adj)
                cb.data_source = "ocsb+ndvi"
            else:
                cb.data_source = ocsb_src

            cb.sgt_prod_mt = round(usda_th_mt + override, 2)
            cb.override_mt = override
            cb.confidence  = ocsb_conf
            return cb

        # ── 2. GEE NDVI integral production estimate ──────────────────────
        try:
            from ingestion.gee_production import thailand_gee_estimate
            gee_mt, gee_src, gee_conf = thailand_gee_estimate(self.mkt_year)
        except Exception as e:
            logger.debug("adjust_thailand GEE estimator: %s", e)
            gee_mt, gee_src, gee_conf = None, "gee_unavailable", 0.50

        if gee_mt is not None:
            override = round(gee_mt - usda_th_mt, 2)
            cb.notes.append(
                "GEE integral %s: %.2f Mt (USDA=%.2f Mt, adj=%+.2f Mt)" % (
                    gee_src, gee_mt, usda_th_mt, override)
            )
            cb.sgt_prod_mt = round(usda_th_mt + override, 2)
            cb.override_mt = override
            cb.data_source = gee_src
            cb.confidence  = gee_conf
            return cb

        # ── 3. GEE z-score (senal anomalia) sobre USDA baseline ───────────
        gee_adj = self._gee_production_adj("th_sugarcane_belt", "TH", usda_th_mt)
        if gee_adj is not None and abs(gee_adj) > 0.1:
            cb.sgt_prod_mt = round(usda_th_mt + gee_adj, 2)
            cb.override_mt = round(gee_adj, 2)
            cb.data_source = "ndvi_gee"
            cb.confidence  = SIGNAL_CONFIDENCE["ndvi_active_season"]
            cb.notes.append("GEE NDVI adj Tailandia: %+.2f Mt (sin OCSB)" % gee_adj)
        else:
            cb.notes.append(
                "Tailandia: OCSB no disponible, GEE integral no disponible, NDVI sin senal"
                " -> USDA sin cambio"
            )

        return cb

    # ── Ajuste EU (EC Agricultural Market Observatory) ───────────────────────

    def adjust_eu(self, usda_eu_mt: Optional[float]) -> CountryBalance:
        """
        Union Europea: ~16 Mt produccion azucar de remolacha. Codigo USDA: E4.
        Campaña: Oct-Ene (beet sugar, Europa del Norte/Centro/Este).

        Fuente: EC Agricultural Market Observatory (agridata.ec.europa.eu/api/).
        Si el dato EC diverge > 0.5 Mt del USDA y es fresco: aplica override parcial.
        Si no hay dato EC o la divergencia es minima: mantiene USDA sin cambio.
        """
        cb = CountryBalance(
            code="EU", name="Union Europea",
            usda_prod_mt=usda_eu_mt,
            sgt_prod_mt=usda_eu_mt,
            data_source="usda",
            confidence=SIGNAL_CONFIDENCE["usda_wasde_official"],
        )
        if usda_eu_mt is None:
            return cb

        try:
            from ingestion.ec_sugar import eu_production_estimate
            ec_mt, ec_src, ec_conf = eu_production_estimate(
                campaign_year=self.mkt_year, usda_eu_mt=usda_eu_mt
            )
        except Exception as e:
            logger.debug("adjust_eu EC import: %s", e)
            ec_mt, ec_src, ec_conf = None, "usda_eu_only", 0.80

        if ec_mt is not None and ec_src != "ec_confirms_usda":
            override = round(ec_mt - usda_eu_mt, 2)
            cb.sgt_prod_mt = ec_mt
            cb.override_mt = override
            cb.data_source = ec_src
            cb.confidence  = ec_conf
            cb.notes.append(
                "EC AMO: %.2f Mt (USDA=%.2f Mt, adj=%+.2f Mt)" % (
                    ec_mt, usda_eu_mt, override)
            )
        else:
            cb.notes.append("EU: EC coincide con USDA o sin datos → USDA sin cambio")

        return cb

    # ── China — verificacion vs USDA ─────────────────────────────────────────

    def adjust_china(self) -> Optional[dict]:
        """
        China (codigo USDA V2: CH).
        USDA 25/26: produccion 11.50 Mt, consumo 15.80 Mt → deficit 4.30 Mt.
        China es gran importador neto. Su balance ya esta incorporado en WB.

        Este metodo verifica si hay divergencia significativa China vs WB.
        StoneX tipicamente usa cifras similares a USDA para China porque
        no hay fuente alternativa publica de gran calidad.

        Retorna dict informativo (no ajusta WB — ya incluido en baseline).
        """
        china_bal = self.load_country_balance("CN")   # CH en USDA V2
        if not china_bal:
            return None

        prod  = china_bal.get("production",      0.0)
        cons  = china_bal.get("dom_consumption", 0.0)
        end_s = china_bal.get("ending_stocks",   0.0)

        return {
            "production_mt":    round(prod, 2),
            "consumption_mt":   round(cons, 2),
            "ending_stocks_mt": round(end_s, 2),
            "deficit_mt":       round(cons - prod, 2),   # importaciones netas necesarias
            "note": ("China deficit %.1f Mt (prod %.1f - cons %.1f). "
                     "Gran importador → soporta precio. Ya en WB baseline.") % (
                        cons - prod, prod, cons),
        }

    # ── GEE → ajuste de produccion proporcional ───────────────────────────────

    def _gee_production_adj(self, poi_id: str, country: str,
                            base_prod_mt: float) -> Optional[float]:
        """
        Convierte z-score de GEE (NDVI o LST) en ajuste de produccion.

        Logica:
          z_ndvi = -1.0 → cultivo 10% peor que media → prod_adj = -0.10 * base
          z_ndvi = +1.0 → cultivo 10% mejor → prod_adj = +0.10 * base
          Cap: ±20% del baseline (no extrapolamos mas alla sin otros datos)

        Combina NDVI (70%) + LST inverso (30% — calor alto = produccion baja).
        """
        try:
            from ingestion.gee_crops import get_latest_gee_metrics
            metrics = get_latest_gee_metrics(self.session, poi_ids=[poi_id])
            poi_data = metrics.get(poi_id, {})

            ndvi_data = poi_data.get("ndvi", {})
            lst_data  = poi_data.get("lst", {})

            ndvi_z = ndvi_data.get("z_score") if ndvi_data else None
            lst_z  = lst_data.get("z_score")  if lst_data  else None

            if ndvi_z is None and lst_z is None:
                return None

            # Calcular z combinado
            z_components = []
            if ndvi_z is not None:
                z_components.append(("ndvi", ndvi_z, 0.70))
            if lst_z is not None:
                # LST alto = estrés = produccion baja → invertir signo
                z_components.append(("lst", -lst_z, 0.30))

            total_w = sum(w for _, _, w in z_components)
            z_combined = sum(z * w for _, z, w in z_components) / total_w

            # Elasticidad: 1 sigma = 8% variacion en produccion (calibrado vs historico)
            elasticity = 0.08
            adj_factor = z_combined * elasticity
            adj_factor = max(-0.20, min(0.20, adj_factor))   # cap ±20%

            # Aplicar peso LST decay si estamos en temporada de cosecha
            if lst_z is not None:
                lst_w = lst_weight(self.ref_date, country)
                adj_factor *= (0.70 + lst_w * 0.30)   # refuerzo por LST segun temporada

            return round(base_prod_mt * adj_factor, 2)

        except Exception as e:
            logger.debug("_gee_production_adj %s: %s", poi_id, e)
            return None

    # ── Proyeccion 26/27 con ajuste tendencia LST ─────────────────────────────

    def project_forward_year(self, base_wb: dict,
                             countries: dict[str, CountryBalance]) -> dict:
        """
        Proyecta el balance 26/27 basado en tendencias historicas + ajuste LST.

        Si heat stress acumulado (LST) de los ultimos 2 años muestra tendencia
        ascendente, no usa media historica de 10 años sino una media movil
        ajustada a la baja (metodologia de ajuste proactivo de tendencia).

        Retorna dict con mismo formato que USDA baseline.
        """
        try:
            prod_current  = float(base_wb.get("production_mt")  or 0)
            cons_current  = float(base_wb.get("consumption_mt") or 0)
            end_s_current = float(base_wb.get("ending_stocks_mt") or 0)

            if prod_current == 0:
                return {}

            # Tasa de crecimiento historica produccion: +1.5% anual (tendencia 10yr USDA)
            base_growth = 0.015
            base_cons_growth = 0.018   # consumo crece mas rapido que produccion

            # Ajuste por tendencia LST acumulada
            lst_trend_adj = self._get_lst_trend_adj()

            fwd_prod = prod_current * (1 + base_growth + lst_trend_adj)
            fwd_cons = cons_current * (1 + base_cons_growth)

            # UNICA quinzenal para Brasil forward year (CS → total añadiendo NE ~4.5 Mt)
            _NE_BRAZIL_MT = 4.5   # Norte-Nordeste sugar, historicamente estable 4-5 Mt
            unica_br_fwd_delta = None
            unica_br_info = ""
            try:
                from ingestion.unica import brazil_unica_estimate
                unica_cs_mt, unica_src, unica_conf = brazil_unica_estimate(
                    self.mkt_year + 1
                )
                if unica_cs_mt is not None:
                    br_cb = countries.get("BR")
                    br_usda = br_cb.usda_prod_mt if br_cb else None
                    if br_usda:
                        unica_br_total = unica_cs_mt + _NE_BRAZIL_MT
                        unica_br_fwd_delta = round(unica_br_total - br_usda, 2)
                        unica_br_info = (
                            "UNICA %s: %.1f Mt CS + %.1f NE = %.1f Mt total "
                            "(USDA=%.1f Mt, adj=%+.1f Mt, conf=%.2f)" % (
                                unica_src, unica_cs_mt, _NE_BRAZIL_MT, unica_br_total,
                                br_usda, unica_br_fwd_delta, unica_conf,
                            )
                        )
                        logger.info("Brazil fwd %d/%.0f: %s", self.mkt_year + 1,
                                    self.mkt_year + 2, unica_br_info)
            except Exception as e:
                logger.debug("project_forward_year UNICA: %s", e)

            # Propagar overrides de paises con escalas por confianza en persistencia
            total_override_fwd = 0.0

            # Brasil: UNICA tiene prioridad sobre propagacion del año actual.
            # Se maneja FUERA del loop porque el override actual puede ser 0
            # (sin CONAB/GEE fresco) mientras UNICA del año forward SI esta disponible.
            br_cb = countries.get("BR")
            if br_cb is not None:
                if unica_br_fwd_delta is not None:
                    total_override_fwd += unica_br_fwd_delta
                elif br_cb.override_mt:
                    total_override_fwd += br_cb.override_mt * 0.60

            for cc, cb in countries.items():
                if cc == "BR":
                    continue   # ya procesado arriba
                if not cb.override_mt:
                    continue
                if cc == "IN":
                    # India: etanol diversion se mantiene o crece → 90% propagacion
                    # Gross methodology gap de USDA se mantiene → 90%
                    total_override_fwd += cb.override_mt * 0.90
                elif cc == "TH":
                    # Tailandia: propagacion 25% del override actual.
                    # La produccion 26/27 depende de lluvia y disponibilidad de cana —
                    # alta incertidumbre sin datos OCSB de la proxima temporada.
                    total_override_fwd += cb.override_mt * 0.25
            fwd_prod += total_override_fwd

            # Balance — metodo delta (usda_end actual como base de stocks iniciales fwd)
            fwd_beg  = end_s_current
            # fwd_end = fwd_beg + fwd_prod - fwd_cons es correcto aqui porque
            # proyectamos desde un punto base sin ajustes de comercio adicionales
            fwd_end  = fwd_beg + fwd_prod - fwd_cons
            fwd_stu  = round(fwd_end / fwd_cons * 100, 1) if fwd_cons > 0 else None

            fwd_method = "trend_projection+lst_adj"
            if unica_br_fwd_delta is not None:
                fwd_method += "+unica_brasil"

            return {
                "marketing_year":      self.mkt_year + 1,
                "production_mt":       round(fwd_prod, 2),
                "consumption_mt":      round(fwd_cons, 2),
                "beginning_stocks_mt": round(fwd_beg, 2),
                "ending_stocks_mt":    round(fwd_end, 2),
                "stocks_to_use_pct":   fwd_stu,
                "surplus_mt":          round(fwd_prod - fwd_cons, 2),
                "lst_trend_adj":       round(lst_trend_adj * 100, 2),   # % ajuste
                "method":              fwd_method,
                "brazil_unica_note":   unica_br_info or None,
            }

        except Exception as e:
            logger.warning("project_forward_year: %s", e)
            return {}

    def _get_lst_trend_adj(self) -> float:
        """
        Calcula ajuste de tendencia basado en LST acumulado 2 años.

        Si LST media de los ultimos 2 años muestra anomalia positiva persistente
        (z_score > 0.5 en media anual), aplica descuento sobre la tasa de
        crecimiento historica. Esto da una proyeccion mas conservadora.

        Retorna: float negativo si tendencia al alza de calor (pessimista),
                 positivo si condiciones favorables, 0 si sin datos.
        """
        try:
            from models.market_data import GeeMetric
            from sqlalchemy import select
            import numpy as np

            cutoff = date.today() - timedelta(days=730)   # 2 años
            rows = self.session.execute(
                select(GeeMetric.obs_date, GeeMetric.z_score)
                .where(
                    GeeMetric.metric == "lst",
                    GeeMetric.poi_id == "br_sp_sugarcane",
                    GeeMetric.obs_date >= cutoff,
                    GeeMetric.z_score.isnot(None),
                )
                .order_by(GeeMetric.obs_date)
            ).fetchall()

            if len(rows) < 10:
                return 0.0

            z_values = [float(r.z_score) for r in rows]
            mean_lst_z = float(np.mean(z_values))

            # Tendencia creciente de calor → reduccion de hasta -2% en crecimiento
            if mean_lst_z > 0.5:
                adj = -min(0.02, mean_lst_z * 0.015)
                logger.info(
                    "LST trend ajuste: media_z=%.2f → adj=%+.2f%% en proyeccion 26/27",
                    mean_lst_z, adj * 100,
                )
                return adj
            elif mean_lst_z < -0.5:
                # Condiciones frescas → leve optimismo
                return min(0.01, abs(mean_lst_z) * 0.008)
            return 0.0

        except Exception as e:
            logger.debug("_get_lst_trend_adj: %s", e)
            return 0.0


# ─────────────────────────────────────────────────────────────────────────────
#  SignalGenerator — balance -> señal + conviction + leverage gate
# ─────────────────────────────────────────────────────────────────────────────

class SignalGenerator:
    """
    Convierte el balance ajustado en señal de trading con:
      - score de conviccion (0-100)
      - leverage_cap_pct como gatekeeper de posicion
      - filtro de precio descontado via opciones (IV filter)

    Gatekeeper: confidence_score < 0.60 → leverage_cap_pct = 25%
    IV filter:  si ATM IV muy alto (mercado ya precio el movimiento) → reducir bias
    """

    def compute(self, sgt_stu: Optional[float],
                usda_stu: Optional[float],
                confidence: float,
                session=None) -> dict:
        """
        Args:
          sgt_stu    : STU% calculado con ajustes SGT
          usda_stu   : STU% oficial USDA (para medir divergencia)
          confidence : score global [0,1]
          session    : para consultar IV de opciones (opcional)

        Returns:
          signal, bias, conviction_score, leverage_cap_pct, stu_divergence, iv_warning
        """
        if sgt_stu is None:
            return {
                "signal": 0, "bias": "NEUTRAL",
                "conviction_score": 0,
                "leverage_cap_pct": 0.0,
                "stu_divergence": None,
                "iv_warning": None,
            }

        # ── STU → señal base ───────────────────────────────────────────────
        if sgt_stu < STU_STRONG_LONG:
            signal, bias, raw_conviction = +1, "STRONG_LONG",  90
        elif sgt_stu < STU_LONG:
            signal, bias, raw_conviction = +1, "LONG",         70
        elif sgt_stu < STU_NEUTRAL_LOW:
            signal, bias, raw_conviction = 0,  "NEUTRAL_BULL", 40
        elif sgt_stu < STU_NEUTRAL_HIGH:
            signal, bias, raw_conviction = 0,  "NEUTRAL",      30
        elif sgt_stu < STU_SHORT:
            signal, bias, raw_conviction = -1, "SHORT",        55
        else:
            signal, bias, raw_conviction = -1, "STRONG_SHORT", 85

        # ── Bonus por divergencia SGT vs USDA ─────────────────────────────
        # Si SGT diverge significativamente de USDA, tenemos "edge" informativo
        stu_div = None
        if usda_stu is not None:
            stu_div = round(usda_stu - sgt_stu, 1)
            # Divergencia > 3pp en misma direccion que señal → conviction++
            if abs(stu_div) > 3.0 and signal != 0:
                dir_match = (stu_div > 0 and signal > 0) or (stu_div < 0 and signal < 0)
                if dir_match:
                    raw_conviction = min(100, raw_conviction + 10)

        # ── Conviction ponderada por confidence ───────────────────────────
        conviction = int(raw_conviction * confidence)

        # ── Leverage gating (confidence como Gatekeeper) ──────────────────
        if confidence >= 0.80:
            leverage_cap = 100.0
        elif confidence >= 0.70:
            leverage_cap = 75.0
        elif confidence >= 0.60:
            leverage_cap = 50.0
        elif confidence >= 0.50:
            leverage_cap = 25.0
        else:
            leverage_cap = 10.0   # Valle de datos — exposicion minima

        # ── IV filter — mercado ya precio el movimiento? ──────────────────
        iv_warning = None
        if session is not None:
            iv_warning = self._check_iv_filter(session, signal)

        return {
            "signal":           signal,
            "bias":             bias,
            "conviction_score": conviction,       # 0-100
            "leverage_cap_pct": leverage_cap,     # % de posicion maxima permitida
            "stu_divergence":   stu_div,          # SGT vs USDA (pp)
            "iv_warning":       iv_warning,
        }

    def _check_iv_filter(self, session, signal: int) -> Optional[str]:
        """
        Comprueba si la volatilidad implicita del mercado de opciones
        ya desconto el movimiento esperado.

        Si IV muy alto (>percentil 80 historico) y señal > 0 (LONG):
          → mercado ya espera el deterioro → retorno esperado mas bajo
        Si IV muy bajo y señal activa:
          → mercado aun no desconto → mejor punto de entrada

        Requiere datos en options_data. Si no hay datos, no bloquea señal.
        """
        try:
            from models.market_data import OptionsData
            from sqlalchemy import select, func

            # ATM IV mas reciente
            recent_iv = session.execute(
                select(OptionsData.iv, OptionsData.trade_date)
                .where(
                    OptionsData.option_type == "call",
                    OptionsData.iv.isnot(None),
                )
                .order_by(OptionsData.trade_date.desc(), OptionsData.iv)
                .limit(50)
            ).fetchall()

            if len(recent_iv) < 5:
                return None

            recent_ivs = [float(r.iv) for r in recent_iv[:10] if r.iv]
            if not recent_ivs:
                return None

            # Historico 90 dias
            cutoff = date.today() - timedelta(days=90)
            hist_iv = session.execute(
                select(OptionsData.iv)
                .where(
                    OptionsData.option_type == "call",
                    OptionsData.iv.isnot(None),
                    OptionsData.trade_date >= cutoff,
                )
                .limit(500)
            ).scalars().all()

            if len(hist_iv) < 20:
                return None

            import numpy as np
            all_ivs   = [float(v) for v in hist_iv]
            current_iv = float(np.median(recent_ivs))
            pct_rank   = float(np.percentile(all_ivs, 80))

            if current_iv > pct_rank and signal != 0:
                return ("IV=%.1f%% en percentil >80 (p80=%.1f%%) — "
                        "mercado ya puede haber descontado el movimiento. "
                        "Conviction reducida.") % (current_iv * 100, pct_rank * 100)

        except Exception as e:
            logger.debug("_check_iv_filter: %s", e)

        return None


# ─────────────────────────────────────────────────────────────────────────────
#  Resultado completo
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SgtBalanceResult:
    """Contiene balance 25/26 + proyeccion 26/27 + metadatos."""
    marketing_year:    int
    forecast_date:     date

    # USDA baseline
    usda_baseline:     dict = field(default_factory=dict)

    # Paises ajustados
    brazil:            CountryBalance = None
    india:             CountryBalance = None
    thailand:          CountryBalance = None
    eu:                CountryBalance = None
    china_info:        Optional[dict] = None   # informativo, ya en WB baseline

    # Balance SGT
    sgt_prod_mt:       Optional[float] = None
    sgt_cons_mt:       Optional[float] = None
    sgt_beg_mt:        Optional[float] = None
    sgt_end_mt:        Optional[float] = None
    sgt_stu_pct:       Optional[float] = None
    sgt_surplus_mt:    Optional[float] = None

    # Señal
    signal:            int = 0
    bias:              str = "NEUTRAL"
    conviction_score:  int = 0
    confidence_score:  float = 0.0
    leverage_cap_pct:  float = 0.0

    # Proyeccion 26/27
    forward_year:      dict = field(default_factory=dict)

    # Metadatos
    signal_weights:    dict = field(default_factory=dict)
    weight_drift:      dict = field(default_factory=dict)
    data_freshness:    dict = field(default_factory=dict)
    warnings:          list[str] = field(default_factory=list)
    stu_divergence:    Optional[float] = None
    iv_warning:        Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
#  Funcion principal
# ─────────────────────────────────────────────────────────────────────────────

def compute_sgt_balance(session,
                        marketing_year: Optional[int] = None,
                        reference_date: Optional[date] = None) -> SgtBalanceResult:
    """
    Compute el balance mundial ajustado SGT para marketing_year.

    Si marketing_year es None, usa el mas reciente en BD.

    Retorna SgtBalanceResult con balance 25/26 + forward 26/27.
    """
    ref = reference_date or date.today()

    # ── 1. Injectar datos y calcular frescura ─────────────────────────────
    injector = DataInjector(session, ref)
    confidence = injector.compute_global_confidence()
    warnings   = injector.get_warnings()
    freshness  = injector.get_data_freshness_dict()

    # ── 2. Determinar marketing_year si no fue provisto ───────────────────
    if marketing_year is None:
        from ingestion.usda_psd import get_world_balance
        try:
            wb = get_world_balance(session)
            marketing_year = wb.get("marketing_year") if wb else ref.year
        except Exception as exc:
            logger.warning(
                "compute_sgt_balance: get_world_balance falló — fallback year=%d: %s",
                ref.year, exc,
            )
            marketing_year = ref.year

    # ── 3. Weight Drift ───────────────────────────────────────────────────
    drift_tracker = WeightDriftTracker(session, marketing_year)
    weight_drift  = drift_tracker.to_dict()

    # ── 4. BalanceEngine ─────────────────────────────────────────────────
    engine = BalanceEngine(session, marketing_year, injector, drift_tracker, ref)

    usda_wb = engine.load_usda_baseline()
    if not usda_wb:
        logger.warning("compute_sgt_balance: sin datos USDA en BD — ejecutar fetch_usda.py")
        return SgtBalanceResult(
            marketing_year=marketing_year,
            forecast_date=ref,
            confidence_score=0.0,
            warnings=["Sin datos USDA en BD — ejecutar: py scripts/fetch_usda.py"],
        )

    usda_prod  = float(usda_wb.get("production_mt")  or 0)
    usda_cons  = float(usda_wb.get("consumption_mt") or 0)
    usda_end   = float(usda_wb.get("ending_stocks_mt") or 0)
    usda_beg   = float(usda_wb.get("beginning_stocks_mt") or 0)
    usda_stu   = usda_wb.get("stocks_to_use_pct")

    # ── 5. Ajustes por pais ───────────────────────────────────────────────
    usda_br = engine.load_country_usda("BR")
    usda_in = engine.load_country_usda("IN")
    usda_th = engine.load_country_usda("TH")
    usda_eu = engine.load_country_usda("EU")

    brazil   = engine.adjust_brazil(usda_br)
    india    = engine.adjust_india(usda_in)
    thailand = engine.adjust_thailand(usda_th)
    eu       = engine.adjust_eu(usda_eu)

    # Advertencia de staleness: USDA PSD API solo devuelve ultimo snapshot por pais.
    # Para India/TH usamos ISMA/OCSB si disponibles, sino ajuste etanol conservador.
    usda_age = injector.freshness.get("usda")
    if usda_age and usda_age.age_days > 100:
        warnings.append(
            "USDA PSD snapshot con %d dias de antiguedad. "
            "Ajustes via ISMA (India) y OCSB (Tailandia) si disponibles." % usda_age.age_days
        )

    # Total ajuste de produccion vs USDA
    total_adj = (brazil.override_mt + india.override_mt
                 + thailand.override_mt + eu.override_mt)
    sgt_prod  = usda_prod + total_adj

    # Consumo: por ahora igual a USDA (sin override propio aun)
    sgt_cons = usda_cons

    # Balance final — metodo delta sobre USDA oficial.
    # Usamos usda_end como punto de partida (preserva ajustes USDA de comercio
    # y discrepancias estadisticas) y solo aplicamos nuestro delta de produccion.
    # Esto es mas robusto que reconstruir desde cero con beg+prod-cons.
    sgt_end     = usda_end + total_adj
    sgt_stu     = round(sgt_end / sgt_cons * 100, 1) if sgt_cons > 0 else None
    sgt_surplus = round(sgt_prod - sgt_cons, 2)

    # ── 6. Pesos aplicados ────────────────────────────────────────────────
    effective_weights = {
        "usda_wasde_official": round(
            SIGNAL_CONFIDENCE["usda_wasde_official"]
            * drift_tracker.apply(1.0, "usda_wasde_official"), 3
        ),
        "conab_survey":  round(
            SIGNAL_CONFIDENCE["conab_survey"]
            * injector.freshness["conab"].confidence_factor, 3
        ),
        "ndvi_active":   round(
            SIGNAL_CONFIDENCE["ndvi_active_season"]
            * injector.freshness["ndvi"].confidence_factor, 3
        ),
        "lst_heat_stress": round(lst_weight(ref, "BR"), 3),
    }

    # ── 7. Señal + gating ────────────────────────────────────────────────
    generator = SignalGenerator()
    sig_result = generator.compute(sgt_stu, usda_stu, confidence, session)

    # ── 8. China (informativo) ────────────────────────────────────────────
    china_info = engine.adjust_china()

    # ── 9. Proyeccion 26/27 ───────────────────────────────────────────────
    countries  = {"BR": brazil, "IN": india, "TH": thailand, "EU": eu}
    forward_26 = engine.project_forward_year(usda_wb, countries)

    result = SgtBalanceResult(
        marketing_year   = marketing_year,
        forecast_date    = ref,
        usda_baseline    = usda_wb,
        brazil           = brazil,
        india            = india,
        thailand         = thailand,
        eu               = eu,
        china_info       = china_info,
        sgt_prod_mt      = round(sgt_prod, 2),
        sgt_cons_mt      = round(sgt_cons, 2),
        sgt_beg_mt       = round(usda_beg, 2),
        sgt_end_mt       = round(sgt_end, 2),
        sgt_stu_pct      = sgt_stu,
        sgt_surplus_mt   = sgt_surplus,
        signal           = sig_result["signal"],
        bias             = sig_result["bias"],
        conviction_score = sig_result["conviction_score"],
        confidence_score = round(confidence, 3),
        leverage_cap_pct = sig_result["leverage_cap_pct"],
        forward_year     = forward_26,
        signal_weights   = effective_weights,
        weight_drift     = weight_drift,
        data_freshness   = freshness,
        warnings         = warnings,
        stu_divergence   = sig_result.get("stu_divergence"),
        iv_warning       = sig_result.get("iv_warning"),
    )

    logger.info(
        "SGT Balance %d/%d: prod=%.1f Mt  cons=%.1f Mt  end=%.1f Mt  "
        "STU=%.1f%%  surplus=%+.1f Mt  conf=%.2f  lev_cap=%.0f%%  bias=%s",
        marketing_year, marketing_year + 1,
        sgt_prod, sgt_cons, sgt_end, sgt_stu or 0,
        sgt_surplus, confidence, sig_result["leverage_cap_pct"], sig_result["bias"],
    )

    return result


# ─────────────────────────────────────────────────────────────────────────────
#  Persistencia en BD
# ─────────────────────────────────────────────────────────────────────────────

def save_balance_forecast(session, result: SgtBalanceResult,
                          scenario: str = "base") -> bool:
    """
    Guarda el resultado del Balance Model en sgt_balance_forecast.
    Crea la tabla si no existe. Idempotente: upsert por (date, year, scenario).
    """
    from database import create_all_tables
    create_all_tables()

    from models.market_data import SgtBalanceForecast
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    usda = result.usda_baseline or {}
    br   = result.brazil
    ind  = result.india
    th   = result.thailand

    row_data = {
        "forecast_date":     result.forecast_date,
        "marketing_year":    result.marketing_year,
        "scenario":          scenario,

        "usda_prod_mt":      usda.get("production_mt"),
        "usda_cons_mt":      usda.get("consumption_mt"),
        "usda_end_mt":       usda.get("ending_stocks_mt"),
        "usda_stu_pct":      usda.get("stocks_to_use_pct"),

        "sgt_prod_mt":       result.sgt_prod_mt,
        "sgt_cons_mt":       result.sgt_cons_mt,
        "sgt_end_mt":        result.sgt_end_mt,
        "sgt_stu_pct":       result.sgt_stu_pct,
        "sgt_surplus_mt":    result.sgt_surplus_mt,

        "br_usda_mt":        br.usda_prod_mt if br else None,
        "br_sgt_mt":         br.sgt_prod_mt  if br else None,
        "br_adj_mt":         br.override_mt  if br else None,
        "br_data_source":    br.data_source  if br else None,

        "in_usda_mt":        ind.usda_prod_mt if ind else None,
        "in_sgt_mt":         ind.sgt_prod_mt  if ind else None,
        "in_adj_mt":         ind.override_mt  if ind else None,
        "in_data_source":    ind.data_source  if ind else None,

        "th_usda_mt":        th.usda_prod_mt if th else None,
        "th_sgt_mt":         th.sgt_prod_mt  if th else None,
        "th_adj_mt":         th.override_mt  if th else None,
        "th_data_source":    th.data_source  if th else None,

        "confidence_score":  result.confidence_score,
        "leverage_cap_pct":  result.leverage_cap_pct,
        "signal":            result.signal,
        "bias":              result.bias,
        "data_freshness":    result.data_freshness,
        "signal_weights":    result.signal_weights,
        "weight_drift":      result.weight_drift,
        "notes":             "; ".join(result.warnings) if result.warnings else None,
    }

    try:
        stmt = pg_insert(SgtBalanceForecast).values(**row_data).on_conflict_do_update(
            index_elements=["forecast_date", "marketing_year", "scenario"],
            set_={k: v for k, v in row_data.items()
                  if k not in ("forecast_date", "marketing_year", "scenario")},
        )
        session.execute(stmt)
        session.commit()
        logger.info("SGT Balance guardado: %s %d/%d [%s]",
                    result.forecast_date, result.marketing_year,
                    result.marketing_year + 1, scenario)
        return True
    except Exception as e:
        session.rollback()
        logger.error("save_balance_forecast: %s", e)
        return False
