"""NewsAPI client — fetch top headlines and targeted search."""
from __future__ import annotations

import httpx
from config.settings import settings


class NewsFetcher:
    BASE_URL = "https://newsapi.org/v2"

    def __init__(self):
        if not settings.NEWS_API_KEY:
            raise RuntimeError(
                "NEWS_API_KEY is not set. Get a free key at newsapi.org and add it to .env."
            )
        self._api_key = settings.NEWS_API_KEY

    def get_headlines(self, category: str = "business", page_size: int = 20) -> list[dict]:
        """Fetch top headlines for a given category."""
        resp = httpx.get(
            f"{self.BASE_URL}/top-headlines",
            params={
                "apiKey": self._api_key,
                "category": category,
                "language": "en",
                "pageSize": page_size,
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        return [self._normalize(a) for a in resp.json().get("articles", [])]

    def search(self, query: str, page_size: int = 10) -> list[dict]:
        """Search for news articles matching a query."""
        resp = httpx.get(
            f"{self.BASE_URL}/everything",
            params={
                "apiKey": self._api_key,
                "q": query,
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": page_size,
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        return [self._normalize(a) for a in resp.json().get("articles", [])]

    @staticmethod
    def _normalize(article: dict) -> dict:
        return {
            "title": article.get("title", ""),
            "description": article.get("description", ""),
            "source": (article.get("source") or {}).get("name", ""),
            "published_at": article.get("publishedAt", ""),
            "url": article.get("url", ""),
        }
