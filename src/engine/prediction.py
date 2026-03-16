"""Headline prediction trading: signal detection + Claude review."""
from __future__ import annotations

import json
import re

BOOST_TERMS = {
    "fed", "rate", "inflation", "cpi", "gdp", "jobs", "tariff",
    "election", "crypto", "bitcoin", "btc", "ethereum", "eth",
    "solana", "sol", "xrp", "ripple", "doge", "dogecoin",
    "recession", "unemployment", "fomc", "interest", "treasury",
    "oil", "gold", "dollar", "yuan", "euro", "yen",
    "war", "ukraine", "russia", "china", "trade",
    "stock", "market", "nasdaq", "dow",
    "earnings", "revenue", "profit", "loss", "bankruptcy",
    "merger", "acquisition", "ipo", "sec", "fda",
}


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"\b[a-z0-9]+\b", text.lower()))


class NewsSignalDetector:
    def find_signals(self, markets: list, headlines: list[dict]) -> list[dict]:
        """
        Match headlines to markets by shared BOOST_TERMS keywords.
        Returns list of {market, headlines, shared_terms}.
        """
        results = []
        for market in markets:
            market_tokens = _tokenize(f"{market.event_name} {market.id}")
            matching_headlines = []
            all_shared: set[str] = set()
            for headline in headlines:
                hl_text = f"{headline.get('title', '')} {headline.get('description', '')}"
                shared = market_tokens & _tokenize(hl_text) & BOOST_TERMS
                if shared:
                    matching_headlines.append(headline)
                    all_shared |= shared
            if matching_headlines:
                results.append({
                    "market": market,
                    "headlines": matching_headlines[:5],
                    "shared_terms": sorted(all_shared),
                })
        return results


class ClaudeReviewer:
    _SYSTEM = (
        "You are a conservative prediction market trading assistant. "
        "You must only approve trades where there is a clear, direct causal link between "
        "the provided headlines and the resolution of the market question. "
        "Respond ONLY with a JSON object: "
        '{"approve": bool, "confidence": int (0-100), "direction": "yes" or "no", '
        '"suggested_size_pct": float (0.01-0.10), "reasoning": str}'
    )

    def __init__(self):
        from config.settings import settings
        import anthropic
        self._client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    def review(self, market, headlines: list[dict]) -> dict | None:
        """Ask Claude whether to approve a trade. Returns None on API error."""
        hl_text = "\n".join(
            f"- [{h['source']}] {h['title']}: {h.get('description', '')}"
            for h in headlines[:5]
        )
        prompt = (
            f"Market question: {market.event_name}\n"
            f"Market ID: {market.id}\n\n"
            f"Recent relevant headlines:\n{hl_text}\n\n"
            "Should I trade this market based on these headlines? Respond with JSON only."
        )
        try:
            msg = self._client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=256,
                system=self._SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw)
            return json.loads(raw)
        except Exception:
            return None


def scan_prediction_opportunities(
    markets: list,
    news_fetcher,
    reviewer: ClaudeReviewer,
    min_confidence: int = 75,
) -> list[dict]:
    """
    1. Fetch headlines (business + general).
    2. Run NewsSignalDetector.find_signals().
    3. Claude-review each signal.
    4. Return approved signals with direction + sizing.
    """
    detector = NewsSignalDetector()
    try:
        headlines = news_fetcher.get_headlines(category="business", page_size=20)
        headlines += news_fetcher.get_headlines(category="general", page_size=20)
    except Exception as exc:
        raise RuntimeError(f"news fetch failed: {exc}") from exc

    signals = detector.find_signals(markets, headlines)
    approved = []
    for signal in signals:
        result = reviewer.review(signal["market"], signal["headlines"])
        if result is None:
            continue
        if result.get("approve") and result.get("confidence", 0) >= min_confidence:
            approved.append({
                "market": signal["market"],
                "direction": result.get("direction", "yes"),
                "confidence": result["confidence"],
                "suggested_size_pct": float(result.get("suggested_size_pct", 0.02)),
                "reasoning": result.get("reasoning", ""),
                "shared_terms": signal["shared_terms"],
            })
    return approved
