from datetime import datetime
from sqlalchemy import Column, Integer, Numeric, Date, DateTime
from .base import Base


class DailyPnl(Base):
    __tablename__ = "daily_pnl"
    id                  = Column(Integer, primary_key=True)
    date                = Column(Date, nullable=False, unique=True)
    realized_pnl_usd    = Column(Numeric(12, 2), default=0)
    unrealized_pnl_usd  = Column(Numeric(12, 2), default=0)
    total_pnl_usd       = Column(Numeric(12, 2))
    cumulative_pnl_usd  = Column(Numeric(12, 2))
    starting_equity     = Column(Numeric(14, 2), default=500_000)
    equity              = Column(Numeric(14, 2))
    peak_equity         = Column(Numeric(14, 2))
    drawdown_usd        = Column(Numeric(12, 2))
    drawdown_pct        = Column(Numeric(6, 4))
    open_positions      = Column(Integer, default=0)
    trades_closed       = Column(Integer, default=0)
    created_at          = Column(DateTime, default=datetime.utcnow)
