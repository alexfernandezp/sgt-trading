from datetime import datetime
from sqlalchemy import Column, Integer, String, Date, DateTime, Text, UniqueConstraint
from .base import Base


class MarketBible(Base):
    """Vista actual del mercado en 4 horizontes temporales."""
    __tablename__ = "market_bible"

    id        = Column(Integer, primary_key=True)
    date      = Column(Date, nullable=False)
    horizon   = Column(String(20), nullable=False)  # STRUCTURAL | TACTICAL | OPERATIONAL | INTRADAY
    bias      = Column(String(10))                  # BULLISH | BEARISH | NEUTRAL
    summary   = Column(Text, nullable=False)
    key_levels = Column(Text)
    catalysts  = Column(Text)
    risks      = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (UniqueConstraint("date", "horizon", name="uq_bible_date_horizon"),)

    HORIZONS = ["STRUCTURAL", "TACTICAL", "OPERATIONAL", "INTRADAY"]


class MarketBibleLog(Base):
    """Historial de cambios en la Market Bible."""
    __tablename__ = "market_bible_log"

    id         = Column(Integer, primary_key=True)
    date       = Column(Date, nullable=False)
    horizon    = Column(String(20), nullable=False)
    old_bias   = Column(String(10))
    new_bias   = Column(String(10))
    old_summary = Column(Text)
    new_summary = Column(Text)
    reason     = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
