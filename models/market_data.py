from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Numeric, BigInteger,
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
