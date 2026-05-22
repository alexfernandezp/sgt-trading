from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, Date, DateTime, Text, JSON, UniqueConstraint
from .base import Base


class DailyScoring(Base):
    """
    12 criterios binarios en 4 bloques. Scoring por direccion (LONG/SHORT).
    Veto absoluto si D1=0 OR D2=0.
    <=5 NO_TRADE | 6-7 REDUCED (5L) | 8-9 STANDARD (10L) | 10-12 MAX_CONVICTION (20L)
    """
    __tablename__ = "daily_scoring"

    id        = Column(Integer, primary_key=True)
    date      = Column(Date, nullable=False)
    direction = Column(String(10), nullable=False)

    a1_spec_vs_mean  = Column(Integer)
    a2_spec_change   = Column(Integer)
    a3_comm_vs_mean  = Column(Integer)
    b1_spread        = Column(Integer)
    b2_price_vs_ma20 = Column(Integer)
    b3_vwap          = Column(Integer)
    c1_key_level     = Column(Integer)
    c2_open_volume   = Column(Integer)
    c3_options       = Column(Integer)
    d1_event_risk    = Column(Integer)
    d2_liquidity     = Column(Integer)
    d3_drawdown      = Column(Integer)

    total_score = Column(Integer)
    veto        = Column(Boolean, default=False)
    decision    = Column(String(20))
    max_lots    = Column(Integer)
    inputs      = Column(JSON)
    notes       = Column(Text)
    created_at  = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("date", "direction", name="uq_scoring_date_direction"),)

    CRITERIA = [
        "a1_spec_vs_mean", "a2_spec_change",   "a3_comm_vs_mean",
        "b1_spread",       "b2_price_vs_ma20", "b3_vwap",
        "c1_key_level",    "c2_open_volume",   "c3_options",
        "d1_event_risk",   "d2_liquidity",     "d3_drawdown",
    ]
    LOT_MAP = {"NO_TRADE": 0, "REDUCED": 5, "STANDARD": 10, "MAX_CONVICTION": 20}

    def compute(self):
        scores = [getattr(self, c) or 0 for c in self.CRITERIA]
        self.total_score = sum(scores)
        self.veto = (self.d1_event_risk == 0 or self.d2_liquidity == 0)
        if self.veto or self.total_score <= 5:
            self.decision = "NO_TRADE"
        elif self.total_score <= 7:
            self.decision = "REDUCED"
        elif self.total_score <= 9:
            self.decision = "STANDARD"
        else:
            self.decision = "MAX_CONVICTION"
        self.max_lots = self.LOT_MAP[self.decision]
        return self
