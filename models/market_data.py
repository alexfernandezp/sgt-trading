from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Numeric, BigInteger, Float, Boolean,
    Date, DateTime, Text, UniqueConstraint, JSON,
)
from .base import Base


class PriceHistory(Base):
    __tablename__ = "price_history"
    id         = Column(Integer, primary_key=True)
    date       = Column(Date, nullable=False)
    instrument = Column(String(20), nullable=False)
    open       = Column(Numeric(12, 4))
    high       = Column(Numeric(12, 4))
    low        = Column(Numeric(12, 4))
    close      = Column(Numeric(12, 4), nullable=False)
    volume     = Column(BigInteger)
    source     = Column(String(50), default="yfinance")
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("date", "instrument", name="uq_price_date_instrument"),)


class PriceBars(Base):
    __tablename__ = "price_bars"
    id         = Column(Integer, primary_key=True)
    datetime   = Column(DateTime, nullable=False)
    instrument = Column(String(20), nullable=False)
    interval   = Column(String(10), nullable=False)
    open       = Column(Numeric(12, 4))
    high       = Column(Numeric(12, 4))
    low        = Column(Numeric(12, 4))
    close      = Column(Numeric(12, 4), nullable=False)
    volume     = Column(BigInteger)
    __table_args__ = (UniqueConstraint("instrument", "interval", "datetime", name="uq_bars_instr_interval_dt"),)


class CotData(Base):
    __tablename__ = "cot_data"
    id          = Column(Integer, primary_key=True)
    report_date = Column(Date, nullable=False)
    instrument  = Column(String(50), nullable=False, default="SUGAR_NO11_ICE")
    ncomm_long   = Column(BigInteger)
    ncomm_short  = Column(BigInteger)
    ncomm_spread = Column(BigInteger)
    comm_long    = Column(BigInteger)
    comm_short   = Column(BigInteger)
    cit_long     = Column(BigInteger)
    cit_short    = Column(BigInteger)
    nonrept_long  = Column(BigInteger)
    nonrept_short = Column(BigInteger)
    total_open_interest = Column(BigInteger)
    change_oi           = Column(Integer)
    change_ncomm_long   = Column(Integer)
    change_ncomm_short  = Column(Integer)
    change_comm_long    = Column(Integer)
    change_comm_short   = Column(Integer)
    change_cit_long     = Column(Integer)
    change_cit_short    = Column(Integer)
    ncomm_net      = Column(Integer)
    comm_net       = Column(Integer)
    cit_net        = Column(Integer)
    speculator_net = Column(Integer)
    # Disaggregated COT (CFTC resource 72hh-3qpy)
    mm_long        = Column(BigInteger)   # Managed Money long
    mm_short       = Column(BigInteger)   # Managed Money short
    mm_spread      = Column(BigInteger)
    mm_net         = Column(Integer)      # mm_long - mm_short
    prodmerc_long  = Column(BigInteger)   # Producer/Merchant long
    prodmerc_short = Column(BigInteger)
    prodmerc_net   = Column(Integer)
    swap_long      = Column(BigInteger)   # Swap Dealer long
    swap_short     = Column(BigInteger)
    swap_net       = Column(Integer)
    traders_mm_long  = Column(Integer)    # number of MM traders long
    traders_mm_short = Column(Integer)
    change_mm_long   = Column(Integer)    # weekly change
    change_mm_short  = Column(Integer)
    total_oi         = Column(BigInteger) # open interest (alias sin conflicto)
    source     = Column(String(50), default="cftc_api")
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("report_date", "instrument", name="uq_cot_date_instrument"),)


class OptionsData(Base):
    """
    Cadena de opciones diaria descargada de Barchart.
    Una fila por strike/tipo/fecha.
    """
    __tablename__ = "options_data"
    id           = Column(Integer, primary_key=True)
    trade_date   = Column(Date, nullable=False)
    instrument   = Column(String(20), nullable=False)
    expiry       = Column(Date, nullable=False)
    strike       = Column(Numeric(8, 2), nullable=False)
    option_type  = Column(String(4), nullable=False)   # 'call' | 'put'
    last_price   = Column(Numeric(10, 4))
    volume       = Column(Integer)
    open_interest = Column(Integer)
    premium      = Column(Numeric(12, 2))
    bid          = Column(Numeric(10, 4))
    ask          = Column(Numeric(10, 4))
    iv           = Column(Numeric(8, 4))   # implied vol 0-1
    delta        = Column(Numeric(10, 6))
    gamma        = Column(Numeric(10, 6))
    theta        = Column(Numeric(10, 6))
    vega         = Column(Numeric(10, 6))
    iv_skew      = Column(Numeric(8, 4))
    source       = Column(String(20), default="barchart")
    created_at   = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("trade_date", "instrument", "expiry", "strike", "option_type",
                         name="uq_options_date_instr_expiry_strike_type"),
    )


class BrazilProduction(Base):
    """
    Produccion sucroalcooleira de Brasil (MAPA) por quincena — Point-in-Time.

    Tras la migracion PIT (P3.E.1, commit 4b57e0a) la tabla almacena dos series
    paralelas:
      - *_cumulative : acumulado de safra a cierre de quincena (materia prima MAPA)
      - *_net        : delta quincenal derivado (cumulative[N] - cumulative[N-1])

    PIT semantics: cada quincena puede tener MULTIPLES filas (una por revision
    MAPA) distinguidas por report_issue_date. UNIQUE (harvest_year,
    fortnight_seq, report_issue_date) garantiza append-only sin overwrite.

    Ver BUSINESS_LOGIC §3.2 (rangos cumulative/net) y §4.1 (read-side PIT).
    """
    __tablename__ = "brazil_production"
    id                  = Column(Integer, primary_key=True)
    report_date         = Column(Date, nullable=False)         # fecha de la QUINCENA
    harvest_year        = Column(String(10), nullable=False)   # ej. "2025-2026"
    fortnight_seq       = Column(Integer)                      # quincena del ano cosecha (1..26)

    # PIT timestamps (P3.E.1)
    report_issue_date   = Column(Date, nullable=False)         # fecha de EMISION del reporte
    report_revision_seq = Column(Integer, default=1)           # _N del filename, default 1

    # Cumulative columns (materia prima MAPA, write-side de _parse_xls)
    cane_crushed_t_cumulative       = Column(Numeric(18, 0))
    sugar_t_cumulative              = Column(Numeric(18, 0))
    ethanol_anhydrous_m3_cumulative = Column(Numeric(18, 0))
    ethanol_hydrated_m3_cumulative  = Column(Numeric(18, 0))
    ethanol_total_m3_cumulative     = Column(Numeric(18, 0))

    # Net columns (delta quincenal, rellenado por P3.E.7)
    cane_crushed_t_net       = Column(Numeric(18, 0))
    sugar_t_net              = Column(Numeric(18, 0))
    ethanol_anhydrous_m3_net = Column(Numeric(18, 0))
    ethanol_hydrated_m3_net  = Column(Numeric(18, 0))
    ethanol_total_m3_net     = Column(Numeric(18, 0))

    sugar_mix_pct       = Column(Numeric(6, 3))                # azucar / (azucar + etanol_equiv)
    source_url          = Column(String(500))
    created_at          = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint(
            "harvest_year", "fortnight_seq", "report_issue_date",
            name="uq_brazil_pit",
        ),
    )


class CepeaPrice(Base):
    """
    Precios CEPEA/ESALQ de etanol y azúcar físicos brasileños.
    Fuente: cepea.org.br/en/indicator/ethanol.aspx y /sugar.aspx
    Series clave:
      hydrous_paulinia_usd_m3   : hidratado Paulínia (SP) — benchmark diario producción
      hydrous_fuel_usd_liter    : hidratado combustible SP — semanal
      anhydrous_usd_liter       : anhidratado SP — semanal
      crystal_sugar_usd_bag50kg : azúcar cristal físico SP — diario
    """
    __tablename__ = "cepea_prices"
    id              = Column(Integer, primary_key=True)
    price_date      = Column(Date, nullable=False)
    series_name     = Column(String(60), nullable=False)
    price_usd       = Column(Numeric(10, 4))
    unit            = Column(String(30))
    pct_daily       = Column(Numeric(8, 4))
    pct_weekly      = Column(Numeric(8, 4))
    pct_monthly     = Column(Numeric(8, 4))
    source_page     = Column(String(10))     # 'ethanol' | 'sugar'
    created_at      = Column(DateTime, default=datetime.utcnow)
    __table_args__  = (
        UniqueConstraint("price_date", "series_name", name="uq_cepea_date_series"),
    )


class SantosPortSnapshot(Base):
    """
    Snapshot diario del ship tracker del Puerto de Santos.
    Filtrado: solo barcos con cargo ACUCAR/SUGAR.
    Tres páginas: expected_arrivals, scheduled_arrivals, berthed_ships.
    """
    __tablename__ = "santos_port_snapshot"
    id            = Column(Integer, primary_key=True)
    snapshot_date = Column(Date, nullable=False)
    page          = Column(String(20), nullable=False)   # expected|scheduled|berthed
    ship_name     = Column(String(100), nullable=False)
    cargo         = Column(String(100))
    terminal      = Column(String(100))
    nav_type      = Column(String(10))                   # Long|Cabo|None
    arrival_dt    = Column(DateTime)                     # expected/scheduled
    load_qty_t    = Column(Integer)                      # berthed: tonelaje cargando
    weight_t      = Column(Integer)                      # expected: peso declarado
    evento        = Column(String(50))                   # scheduled: ATRACACAO etc.
    voyage        = Column(String(50))
    duv           = Column(String(20))                   # ID Porto Santos
    created_at    = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("snapshot_date", "page", "ship_name", "terminal",
                         name="uq_santos_date_page_ship_terminal"),
    )


class CalendarEvent(Base):
    __tablename__ = "calendar_events"
    id             = Column(Integer, primary_key=True)
    event_date     = Column(Date, nullable=False)
    event_time     = Column(String(10))
    event_type     = Column(String(50), nullable=False)
    title          = Column(String(200), nullable=False)
    description    = Column(Text)
    impact         = Column(String(10))
    actual_value   = Column(String(50))
    forecast_value = Column(String(50))
    previous_value = Column(String(50))
    is_confirmed   = Column(String(5), default="true")
    created_at     = Column(DateTime, default=datetime.utcnow)


class OniIndex(Base):
    """
    Índice ONI (Oceanic Niño Index) de NOAA/CPC.
    Media móvil 3 meses de anomalía SST en región Niño 3.4.
    Fuente: https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt
    """
    __tablename__ = "oni_index"
    id             = Column(Integer, primary_key=True)
    obs_date       = Column(Date, nullable=False)      # 1er día del mes central
    season         = Column(String(3), nullable=False)  # DJF, JFM … NDJ
    year           = Column(Integer, nullable=False)
    month          = Column(Integer, nullable=False)    # mes central (1-12)
    oni_value      = Column(Numeric(5, 2))              # anomalía temperatura (°C)
    classification = Column(String(30))                 # VERY_STRONG_NINO, STRONG_NINO, etc.
    source         = Column(String(50), default="noaa_cpc")
    created_at     = Column(DateTime, default=datetime.utcnow)
    __table_args__  = (UniqueConstraint("year", "month", name="uq_oni_year_month"),)


class ClimateDaily(Base):
    """
    Datos climáticos diarios por estación — Open-Meteo ERA5.
    Estaciones clave: ribeirão preto, piracicaba (cinturón azucarero SP).
    Fuente: archive-api.open-meteo.com (ERA5 reanalysis, lag ~5 días)
    """
    __tablename__  = "climate_daily"
    id             = Column(Integer, primary_key=True)
    obs_date       = Column(Date, nullable=False)
    station_name   = Column(String(50), nullable=False)  # 'ribeirao_preto' | 'piracicaba'
    latitude       = Column(Numeric(8, 4))
    longitude      = Column(Numeric(8, 4))
    precip_mm      = Column(Numeric(8, 2))    # precipitación diaria (mm)
    et0_mm         = Column(Numeric(8, 2))    # ET0 FAO-56 Penman-Monteith (mm)
    temp_max_c     = Column(Numeric(6, 2))    # temperatura máxima (°C)
    temp_min_c     = Column(Numeric(6, 2))    # temperatura mínima (°C)
    soil_moisture  = Column(Numeric(8, 5))    # humedad suelo 0-7 cm (m³/m³)
    source         = Column(String(30), default="open_meteo_era5")
    created_at     = Column(DateTime, default=datetime.utcnow)
    __table_args__  = (UniqueConstraint("obs_date", "station_name", name="uq_climate_date_station"),)


class NdviSentinel(Base):
    """
    NDVI medio semanal del cinturón azucarero São Paulo.
    Calculado sobre imágenes Sentinel-2 via Google Earth Engine.
    NDVI = (B8 - B4) / (B8 + B4) — resolución 10m, ventana semanal.
    """
    __tablename__   = "ndvi_sentinel"
    id              = Column(Integer, primary_key=True)
    obs_date        = Column(Date, nullable=False)        # inicio semana
    region_name     = Column(String(50), nullable=False)  # 'sp_sugarcane_belt'
    mean_ndvi       = Column(Numeric(6, 4))               # NDVI medio (0-1)
    std_ndvi        = Column(Numeric(6, 4))               # desviación
    cloud_cover_pct = Column(Numeric(5, 1))               # cobertura nubosa (%)
    pixel_count     = Column(Integer)                      # píxeles válidos usados
    scene_count     = Column(Integer)                      # imágenes S2 compuestas
    source          = Column(String(30), default="sentinel2_gee")
    created_at      = Column(DateTime, default=datetime.utcnow)
    __table_args__   = (UniqueConstraint("obs_date", "region_name", name="uq_ndvi_date_region"),)


class ComexStatExport(Base):
    """
    Exportaciones mensuales de azúcar de Brasil — Comex Stat MDIC.
    NCM 17011400 = Açúcar de cana, em bruto (equiv. ICE Sugar No.11)
    NCM 17019900 = Outros açúcares (VHP, cristal, refinado)
    Fuente: https://api-comexstat.mdic.gov.br — lag ~25 días.
    """
    __tablename__  = "comex_stat_export"
    id             = Column(Integer, primary_key=True)
    ref_date       = Column(Date, nullable=False)          # primer día del mes
    ncm_code       = Column(String(10), nullable=False)
    ncm_desc       = Column(String(200))
    total_kg       = Column(BigInteger)                    # kg neto exportado
    total_usd_fob  = Column(BigInteger)                    # USD FOB
    source         = Column(String(30), default="comexstat_mdic")
    created_at     = Column(DateTime, default=datetime.utcnow)
    __table_args__  = (UniqueConstraint("ref_date", "ncm_code", name="uq_comex_date_ncm"),)


class InpeFire(Base):
    """
    Focos de incendio diarios — INPE/Terrabrasilis AMS.
    Fuente: ams1h:active-fire-today WFS (multi-satélite).
    Región: SP+PR (São Paulo + Paraná, cinturón azucarero) via BBOX.
    """
    __tablename__ = "inpe_fire"
    id            = Column(Integer, primary_key=True)
    obs_date      = Column(Date, nullable=False)
    state         = Column(String(10), nullable=False)      # SP, PR, SP+PR, etc.
    fire_count    = Column(Integer)
    satellite     = Column(String(20), default="MULTI")
    source        = Column(String(30), default="ams1h_terrabrasilis")
    created_at    = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("obs_date", "state", "satellite",
                                       name="uq_inpe_date_state_sat"),)


class ParanaguaPortSnapshot(Base):
    """
    Snapshot diario del line-up del Puerto de Paranaguá (APPA).
    Filtrado: solo barcos con Mercadoria ACUCAR/SUGAR.
    Secciones: atracados, programados, ao_largo, esperados, despachados.
    DESPACHADOS incluye chegada + desatracacao → dwell time directo.
    Fuente: https://www.appaweb.appa.pr.gov.br/appaweb/pesquisa.aspx?WCI=relLineUpRetroativo
    """
    __tablename__  = "paranagua_port_snapshot"
    id             = Column(Integer, primary_key=True)
    snapshot_date  = Column(Date, nullable=False)
    page           = Column(String(20), nullable=False)    # atracados|programados|ao_largo|esperados|despachados
    ship_name      = Column(String(100), nullable=False)
    imo            = Column(String(20))
    dwt            = Column(Numeric(12, 2))                # tonelaje muerto
    cargo          = Column(String(150))                   # Mercadoria
    terminal       = Column(String(20))                    # Berço (berth number)
    sentido        = Column(String(20))                    # Exp|Imp|Imp/Exp
    arrival_dt     = Column(DateTime)                      # Chegada (real)
    departure_dt   = Column(DateTime)                      # Desatracacao (despachados)
    eta_dt         = Column(DateTime)                      # ETA / Cal.Cheg (esperados)
    tonnage_prev   = Column(Numeric(14, 3))                # Previsto (tons)
    tonnage_real   = Column(Numeric(14, 3))                # Realizado (tons)
    source         = Column(String(30), default="appa_paranagua")
    created_at     = Column(DateTime, default=datetime.utcnow)
    __table_args__  = (UniqueConstraint("snapshot_date", "page", "ship_name", "terminal",
                                        name="uq_paranagua_date_page_ship"),)


class IndiaEthanolDiversion(Base):
    """
    Desvío de azúcar a etanol en India por temporada de molienda.
    Fuente: ISMA press releases, MoPNG ESY reports.
    season_year = Oct del inicio de la temporada (ej. 2024 → Oct24-Apr25 = ISMA 2024-25).
    """
    __tablename__ = "india_ethanol_diversion"
    id              = Column(Integer, primary_key=True)
    season_year     = Column(Integer, nullable=False)
    esy_year        = Column(Integer)                      # Nov de season_year (ESY start)
    diversion_lmt   = Column(Numeric(8, 2))               # lakh metric tonnes azúcar equiv.
    diversion_mt    = Column(Numeric(8, 3))               # Mt azúcar equivalente
    esy_target_lmt  = Column(Numeric(8, 2))               # target declarado CCEA (si disponible)
    sugar_route_pct = Column(Numeric(5, 2))               # % etanol de ruta azúcar vs total
    data_type       = Column(String(20), default="actual") # 'actual' | 'estimate' | 'formula'
    source          = Column(String(100))
    notes           = Column(Text)
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint("season_year", name="uq_india_ethanol_season"),)


class IndiaSugarPrice(Base):
    """
    Precio ex-mill azúcar India mensual (₹/quintal).
    Fuente: ChiniMandi / ISMA.
    price_date = primer día del mes de referencia.
    """
    __tablename__ = "india_sugar_price"
    id           = Column(Integer, primary_key=True)
    price_date   = Column(Date, nullable=False)
    price_rs_qtl = Column(Numeric(8, 2))                  # ₹ per quintal (100 kg)
    price_rs_kg  = Column(Numeric(8, 4))                  # ₹ per kg
    season_year  = Column(Integer)                        # temporada molienda (Oct start)
    source       = Column(String(50), default="chinimandi")
    created_at   = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("price_date", name="uq_india_sugar_price_date"),)


class GeeMetric(Base):
    """
    Métricas GEE por POI y fecha — harvest pace, crop stress, SPI.
    Un registro por (fecha, poi_id, métrica).
    POIs configurados en config/gee_pois.json.

    Métricas:
      ndvi   — índice vegetación Sentinel-2 (harvest pace)
      ndwi   — índice agua en vegetación Sentinel-2 (estrés hídrico)
      lst    — temperatura superficial terrestre MODIS °C
      spi90  — precipitación acumulada 90d vs baseline (z-score)
    """
    __tablename__   = "gee_metric"
    id              = Column(Integer, primary_key=True)
    obs_date        = Column(Date, nullable=False)
    poi_id          = Column(String(50), nullable=False)
    metric          = Column(String(20), nullable=False)
    value           = Column(Float)
    z_score         = Column(Float)
    anomaly         = Column(Boolean, default=False)
    baseline_mean   = Column(Float)
    baseline_std    = Column(Float)
    n_baseline_yrs  = Column(Integer)
    source          = Column(String(30), default="gee")
    created_at      = Column(DateTime, default=datetime.utcnow)
    __table_args__  = (UniqueConstraint("obs_date", "poi_id", "metric",
                                        name="uq_gee_date_poi_metric"),)


class SignalDailyLog(Base):
    """
    Registro histórico de señales diarias — permite computar IC rolling por señal
    y calibrar pesos dinámicos (IC Weighting) para el macro score.

    Flujo:
      1. score_today.py escribe raw_value + direction tras calcular señales.
      2. daily_pipeline.py rellena fwd_ret_5d/10d/20d retroactivamente cuando
         el precio futuro ya está disponible.
      3. ic_weighting.py lee esta tabla para compute Spearman IC rolling.
    """
    __tablename__   = "signal_daily_log"
    id              = Column(Integer, primary_key=True)
    date            = Column(Date, nullable=False)
    signal_name     = Column(String(60), nullable=False)
    signal_group    = Column(String(30), nullable=False)   # cot|macro|fundamental|spread
    raw_value       = Column(Numeric(14, 6))               # valor continuo (p.ej. corr=0.45, oni=-0.11)
    direction       = Column(Integer)                      # +1=LONG, -1=SHORT, 0=NEUTRAL
    fwd_ret_5d      = Column(Numeric(8, 4))                # retorno % SBN front-month +5 días
    fwd_ret_10d     = Column(Numeric(8, 4))                # retorno % +10 días
    fwd_ret_20d     = Column(Numeric(8, 4))                # retorno % +20 días
    is_carry        = Column(Boolean, default=False)       # True = forward-fill (no hay dato nuevo ese día)
    created_at      = Column(DateTime, default=datetime.utcnow)
    __table_args__  = (UniqueConstraint("date", "signal_name", name="uq_signal_log_date_name"),)


class ConabCanaLevantamento(Base):
    __tablename__   = "conab_cana_levantamento"
    id              = Column(Integer, primary_key=True)
    season          = Column(String(10), nullable=False)   # "2025/26"
    levantamento    = Column(Integer,    nullable=False)   # 1-6
    pub_date        = Column(Date)
    # Producción total
    cane_total_mt   = Column(Numeric(8, 2))   # millones de toneladas
    area_mha        = Column(Numeric(6, 3))   # millones de hectáreas
    yield_kg_ha     = Column(Numeric(8, 1))   # kg/ha
    sugar_total_mt  = Column(Numeric(7, 2))   # millones de toneladas
    ethanol_cana_blt      = Column(Numeric(6, 2))   # billones de litros (caña)
    ethanol_total_blt     = Column(Numeric(6, 2))   # billones de litros (caña+maíz)
    ethanol_hydrous_blt   = Column(Numeric(6, 2))
    ethanol_anhydrous_blt = Column(Numeric(6, 2))
    # YoY
    yoy_cane_pct    = Column(Numeric(6, 2))
    yoy_sugar_pct   = Column(Numeric(6, 2))
    yoy_ethanol_cana_pct  = Column(Numeric(6, 2))
    # Revisión intra-temporada (vs levantamento anterior)
    revision_sugar_pct    = Column(Numeric(6, 2))
    revision_ethanol_pct  = Column(Numeric(6, 2))
    # Estado SP
    sp_sugar_mt     = Column(Numeric(7, 2))
    sp_cane_mt      = Column(Numeric(8, 2))
    pdf_url         = Column(Text)
    updated_at      = Column(DateTime)
    created_at      = Column(DateTime, default=datetime.utcnow)
    __table_args__  = (UniqueConstraint("season", "levantamento",
                                        name="uq_conab_season_lev"),)


class IsmaRelease(Base):
    """
    Datos de producción quincenal publicados por ISMA
    (Indian Sugar & Bio-energy Manufacturers Association).
    Temporada Oct-Abr. Cifras NETAS (diversion juice→etanol ya incluida).
    1 lakh tonne = 0.1 Mt.
    """
    __tablename__ = "isma_release"
    id                    = Column(BigInteger, primary_key=True, autoincrement=True)
    data_date             = Column(Date, nullable=False)          # "as on" date del dato
    pub_date              = Column(Date)                          # fecha del press release
    marketing_year        = Column(Integer, nullable=False)       # año inicio temporada (2025 → 25/26)
    cumulative_lakh_t     = Column(Numeric(8, 2), nullable=False) # producción acumulada en lakh t
    cumulative_mt         = Column(Numeric(6, 3), nullable=False) # producción acumulada en Mt (lakh/10)
    yoy_change_pct        = Column(Numeric(5, 1))                 # variación YoY en %
    season_progress_pct   = Column(Numeric(5, 1))                 # % de temporada completado
    estimated_full_year_mt = Column(Numeric(6, 3))               # proyección full-year calculada
    mills_operating       = Column(Integer)                       # fábricas activas (nullable)
    maharashtra_lakh_t    = Column(Numeric(7, 2))                 # Maharashtra (nullable)
    up_lakh_t             = Column(Numeric(7, 2))                 # Uttar Pradesh (nullable)
    karnataka_lakh_t      = Column(Numeric(7, 2))                 # Karnataka (nullable)
    source                = Column(String(50), default="manual_cli")  # "manual_cli"|"duckduckgo"|"tribune" etc.
    notes                 = Column(Text)
    created_at            = Column(DateTime, default=datetime.utcnow)
    __table_args__        = (
        UniqueConstraint("data_date", "marketing_year", name="uq_isma_date_marketing_year"),
    )


class SgtBalanceForecast(Base):
    """
    Historial versionado de predicciones del SGT Live Balance Model.

    Cada fila es un snapshot del balance en un momento determinado:
      - Permite auditoría retrospectiva (¿cuánto nos desviamos del USDA final?)
      - Alimenta el Weight Drift: si nuestro sgt_prod_mt se aleja del USDA
        revisado sistemáticamente 3 meses, el engine baja el peso de esa fuente.
      - confidence_score actúa como Gatekeeper: leverage_cap_pct limita posición.

    marketing_year = año inicio temporada (2025 para 2025/26).
    scenario       = "base" | "bull" | "bear"
    """
    __tablename__     = "sgt_balance_forecast"
    id                = Column(Integer, primary_key=True)
    forecast_date     = Column(Date,    nullable=False)
    marketing_year    = Column(Integer, nullable=False)
    scenario          = Column(String(10), default="base")

    # Línea base USDA WASDE (punto de partida oficial)
    usda_prod_mt      = Column(Numeric(8, 2))
    usda_cons_mt      = Column(Numeric(8, 2))
    usda_end_mt       = Column(Numeric(8, 2))
    usda_stu_pct      = Column(Numeric(6, 2))

    # Balance ajustado SGT
    sgt_prod_mt       = Column(Numeric(8, 2))
    sgt_cons_mt       = Column(Numeric(8, 2))
    sgt_end_mt        = Column(Numeric(8, 2))
    sgt_stu_pct       = Column(Numeric(6, 2))
    sgt_surplus_mt    = Column(Numeric(8, 2))   # + surplus / - deficit

    # Ajuste Brasil (CONAB override)
    br_usda_mt        = Column(Numeric(7, 2))
    br_sgt_mt         = Column(Numeric(7, 2))
    br_adj_mt         = Column(Numeric(6, 2))   # sgt - usda
    br_data_source    = Column(String(40))       # "conab_lev3" | "ndvi_gee" | "usda"

    # Ajuste India (NDVI + factor etanol)
    in_usda_mt        = Column(Numeric(7, 2))
    in_sgt_mt         = Column(Numeric(7, 2))
    in_adj_mt         = Column(Numeric(6, 2))
    in_data_source    = Column(String(40))

    # Ajuste Tailandia (OCSB / NDVI)
    th_usda_mt        = Column(Numeric(7, 2))
    th_sgt_mt         = Column(Numeric(7, 2))
    th_adj_mt         = Column(Numeric(6, 2))
    th_data_source    = Column(String(40))

    # Confianza & gating de apalancamiento
    confidence_score  = Column(Numeric(5, 3))   # 0-1
    leverage_cap_pct  = Column(Numeric(5, 2))   # % de la posición máxima permitida

    # Señal de trading
    signal            = Column(Integer)          # +1 / 0 / -1
    bias              = Column(String(20))

    # Metadatos de pesos y fuentes (JSON)
    data_freshness    = Column(JSON)             # {fuente: días_de_antigüedad}
    signal_weights    = Column(JSON)             # {fuente: peso_aplicado}
    weight_drift      = Column(JSON)             # {fuente: drift_3m_coef}
    notes             = Column(Text)

    created_at        = Column(DateTime, default=datetime.utcnow)
    __table_args__    = (
        UniqueConstraint("forecast_date", "marketing_year", "scenario",
                         name="uq_sgt_balance_forecast"),
    )


class UsdaPsdRecord(Base):
    """
    USDA FAS PSD (Production, Supply and Distribution) — balance global azúcar.
    Un registro por (commodity, país, marketing_year, atributo, mes_publicación).
    pub_month=0 indica dato de descarga bulk (sin mes de publicación específico).
    Valores en 1000 MT.
    """
    __tablename__    = "usda_psd"
    id               = Column(Integer, primary_key=True)
    commodity_code   = Column(String(10), nullable=False)  # "0612000"
    country_code     = Column(String(5),  nullable=False)  # "WB", "BR", "IN"...
    country_name     = Column(String(60))
    marketing_year   = Column(Integer, nullable=False)     # año inicio temporada
    pub_month        = Column(Integer, nullable=False, default=0)  # 0=bulk, 1-12=WASDE mes
    attribute_id     = Column(Integer, nullable=False)
    attribute_name   = Column(String(100))
    value_1000mt     = Column(Numeric(12, 2))              # miles de MT
    created_at       = Column(DateTime, default=datetime.utcnow)
    updated_at       = Column(DateTime)
    __table_args__   = (
        UniqueConstraint("commodity_code", "country_code", "marketing_year",
                         "attribute_id", "pub_month", name="uq_usda_psd"),
    )
