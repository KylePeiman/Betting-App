"""ORM models for the streaming ARB engine — isolated from src/storage/models.py."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase


class ArbStreamBase(DeclarativeBase):
    pass


def _now():
    return datetime.now(timezone.utc)


class ArbStreamSession(ArbStreamBase):
    __tablename__ = "arb_stream_sessions"

    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime, default=_now)
    ended_at = Column(DateTime, nullable=True)
    bankroll_usd = Column(Float)
    current_bankroll_usd = Column(Float)
    simulate = Column(Integer, default=1)   # 1 = paper, 0 = live


class ArbStreamPosition(ArbStreamBase):
    """One entered arb opportunity within a session."""
    __tablename__ = "arb_stream_positions"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("arb_stream_sessions.id"))
    created_at = Column(DateTime, default=_now)
    detected_at = Column(DateTime)           # when arb was first spotted by WS event
    settled_at = Column(DateTime, nullable=True)
    arb_type = Column(String)               # "series" | "binary"
    event_ticker = Column(String)
    cost_cents = Column(Float)              # total cost at simulated fill prices
    detection_cost_cents = Column(Float)    # total cost at detection (pre-entry) prices
    pnl_cents = Column(Float, nullable=True)
    status = Column(String, default="open")  # open | won | lost | voided

    # JSON array of per-leg details — see engine.py for schema
    _legs_raw = Column("legs", Text)

    @property
    def legs(self) -> list[dict]:
        if self._legs_raw:
            return json.loads(self._legs_raw)
        return []

    @legs.setter
    def legs(self, value: list[dict]) -> None:
        self._legs_raw = json.dumps(value)
