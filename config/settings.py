"""Application settings — loaded from environment variables via python-dotenv."""
from __future__ import annotations
import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # API Keys
    ODDS_API_KEY: str = os.getenv("ODDS_API_KEY", "")
    BETFAIR_USERNAME: str = os.getenv("BETFAIR_USERNAME", "")
    BETFAIR_PASSWORD: str = os.getenv("BETFAIR_PASSWORD", "")
    BETFAIR_APP_KEY: str = os.getenv("BETFAIR_APP_KEY", "")
    BETFAIR_CERTS_PATH: str | None = os.getenv("BETFAIR_CERTS_PATH")
    SPORTSDATA_API_KEY: str = os.getenv("SPORTSDATA_API_KEY", "")
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///betting_app.db")

    # Claude model
    CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

    # TheOddsAPI defaults
    ODDS_API_REGIONS: str = os.getenv("ODDS_API_REGIONS", "us,uk,eu,au")
    ODDS_API_MARKETS: str = os.getenv("ODDS_API_MARKETS", "h2h,spreads,totals")

    # Engine defaults
    MIN_EV_THRESHOLD: float = float(os.getenv("MIN_EV_THRESHOLD", "0.03"))
    DEFAULT_SOURCES: list[str] = os.getenv("DEFAULT_SOURCES", "odds_api").split(",")


settings = Settings()
