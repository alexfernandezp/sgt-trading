from datetime import datetime
from sqlalchemy import Column, Integer, String, Numeric, DateTime, Text, ForeignKey
from .base import Base


class Position(Base):
    __tablename__ = "positions"
    id               = Column(Integer, primary_key=True)
    instrument       = Column(String(20), nullable=False)
    direction        = Column(String(10), nullable=False)
    entry_date       = Column(DateTime, nullable=False)
    entry_price      = Column(Numeric(10, 4), nullable=False)
    size_lots        = Column(Integer, nullable=False)
    stop_loss        = Column(Numeric(10, 4))
    take_profit      = Column(Numeric(10, 4))
    initial_risk_usd = Column(Numeric(10, 2))
    status           = Column(String(20), default="OPEN")
    scoring_id       = Column(Integer, ForeignKey("daily_scoring.id"), nullable=True)
    notes            = Column(Text)
    created_at       = Column(DateTime, default=datetime.utcnow)
    updated_at       = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def unrealized_pnl(self, current_price: float) -> float:
        sign = 1 if self.direction == "LONG" else -1
        return sign * self.size_lots * 112_000 * (current_price - float(self.entry_price)) / 100


class ClosedTrade(Base):
    __tablename__ = "closed_trades"
    id               = Column(Integer, primary_key=True)
    position_id      = Column(Integer, ForeignKey("positions.id"), nullable=True)
    instrument       = Column(String(20), nullable=False)
    direction        = Column(String(10), nullable=False)
    entry_date       = Column(DateTime, nullable=False)
    entry_price      = Column(Numeric(10, 4), nullable=False)
    exit_date        = Column(DateTime, nullable=False)
    exit_price       = Column(Numeric(10, 4), nullable=False)
    size_lots        = Column(Integer, nullable=False)
    gross_pnl_usd    = Column(Numeric(10, 2), nullable=False)
    commission_usd   = Column(Numeric(10, 2), default=0)
    net_pnl_usd      = Column(Numeric(10, 2), nullable=False)
    initial_risk_usd = Column(Numeric(10, 2))
    r_multiple       = Column(Numeric(6, 2))
    exit_reason      = Column(String(50))
    scoring_id       = Column(Integer, ForeignKey("daily_scoring.id"), nullable=True)
    notes            = Column(Text)
    created_at       = Column(DateTime, default=datetime.utcnow)

    @staticmethod
    def calc_gross_pnl(direction: str, entry: float, exit_: float, lots: int) -> float:
        sign = 1 if direction == "LONG" else -1
        return sign * lots * 112_000 * (exit_ - entry) / 100
