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
    Produccion sucroalcooleira de Brasil (MAPA) por quincena.
    Fuente: MAPA — Acompanhamento da Producao Sucroalcooleira.
    Fila nacional: "Tot.g" del XLS.
    """
    __tablename__ = "brazil_production"
    id                  = Column(Integer, primary_key=True)
    report_date         = Column(Date, nullable=False)    # fecha aproximada de referencia
    harvest_year        = Column(String(10), nullable=False)   # ej. "2025-2026"
    fortnight_seq       = Column(Integer)                 # quincena acumulada del año cosecha (1,2,…26)
    cane_crushed_t      = Column(Numeric(18, 0))
    sugar_t             = Column(Numeric(18, 0))
    ethanol_anhydrous_m3 = Column(Numeric(18, 0))
    ethanol_hydrated_m3  = Column(Numeric(18, 0))
    ethanol_total_m3    = Column(Numeric(18, 0))
    sugar_mix_pct       = Column(Numeric(6, 3))           # azucar / (azucar+etanol equivalente)
    source_url          = Column(String(500))
    created_at          = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("harvest_year", "fortnight_seq", name="uq_brazil_harvest_fortnight"),
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
