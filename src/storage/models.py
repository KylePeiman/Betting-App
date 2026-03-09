"""SQLAlchemy ORM models."""
from __future__ import annotations
import json
from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Text, Enum
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def _now():
    return datetime.now(timezone.utc)


class Recommendation(Base):
    __tablename__ = "recommendations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=_now)
    period = Column(String(20), nullable=False)        # "week" | "month"
    mode = Column(String(20), nullable=False)           # "agent" | "compute"
    source = Column(String(50), nullable=False)
    category = Column(String(100), nullable=False)
    event_name = Column(String(255), nullable=False)
    selection = Column(String(255), nullable=False)
    odds = Column(Float, nullable=False)
    stake_units = Column(Float, nullable=False, default=1.0)
    confidence = Column(Float, nullable=False, default=0.5)
    rationale = Column(Text, nullable=False, default="")
    status = Column(String(20), default="pending")      # "pending" | "settled"

    outcome = relationship("Outcome", back_populates="recommendation", uselist=False)

    def __repr__(self):
        return f"<Recommendation id={self.id} event='{self.event_name}' sel='{self.selection}' odds={self.odds}>"


class Outcome(Base):
    __tablename__ = "outcomes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    recommendation_id = Column(Integer, ForeignKey("recommendations.id"), nullable=False, unique=True)
    result = Column(String(10), nullable=False)         # "win" | "loss" | "void"
    actual_odds = Column(Float, nullable=True)
    settled_at = Column(DateTime, default=_now)

    recommendation = relationship("Recommendation", back_populates="outcome")


class EvaluationReport(Base):
    __tablename__ = "evaluation_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=_now)
    period_start = Column(DateTime, nullable=False)
    period_end = Column(DateTime, nullable=False)
    roi = Column(Float, nullable=False)
    hit_rate = Column(Float, nullable=False)
    clv_avg = Column(Float, nullable=True)
    units_profit = Column(Float, nullable=False)
    total_bets = Column(Integer, nullable=False)
    _mode_breakdown = Column("mode_breakdown", Text, default="{}")

    @property
    def mode_breakdown(self) -> dict:
        return json.loads(self._mode_breakdown or "{}")

    @mode_breakdown.setter
    def mode_breakdown(self, value: dict):
        self._mode_breakdown = json.dumps(value)
