"""Database setup — SQLite via SQLAlchemy, upgrade-ready for Postgres."""
from __future__ import annotations
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from src.storage.models import Base
from config.settings import settings

_engine = None
_SessionLocal = None


def _migrate(engine):
    """Add new columns to existing tables without dropping data."""
    with engine.connect() as conn:
        new_columns = [
            ("sim_positions", "live", "INTEGER DEFAULT 0"),
            ("sim_positions", "order_ids", "TEXT"),
        ]
        for table, col, col_type in new_columns:
            try:
                conn.execute(__import__("sqlalchemy").text(
                    f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"
                ))
                conn.commit()
            except Exception:
                pass  # column already exists


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            settings.DATABASE_URL,
            connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {},
        )
        Base.metadata.create_all(_engine)
        _migrate(_engine)
    return _engine


def get_session() -> Session:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=_get_engine(), autocommit=False, autoflush=False)
    return _SessionLocal()
