"""Agent mode — Claude AI orchestration for bet recommendations."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Any

import anthropic

from src.fetchers.base import Market
from config.settings import settings


SYSTEM_PROMPT = """You are an expert sports and events betting analyst. Your role is to:
1. Fetch available markets and odds from data sources
2. Analyse the data for positive expected value (EV) opportunities
3. Apply Kelly Criterion reasoning for stake sizing
4. Identify any arbitrage opportunities across bookmakers
5. Produce clear, data-driven bet recommendations with confidence levels and rationale

You have access to tools to fetch markets and store your recommendations.
Be thorough but concise. Focus on value, not volume of bets."""


def _make_tools() -> list[dict]:
    return [
        {
            "name": "fetch_markets",
            "description": "Fetch available betting markets from a specified source.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": ["odds_api", "betfair", "sportsdata"],
                        "description": "The data source to fetch markets from.",
                    },
                    "filters": {
                        "type": "object",
                        "description": "Optional filters (e.g. sport, category).",
                    },
                },
                "required": ["source"],
            },
        },
        {
            "name": "fetch_odds",
            "description": "Fetch current odds for a specific market by ID.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "market_id": {"type": "string"},
                },
                "required": ["source", "market_id"],
            },
        },
        {
            "name": "get_historical_performance",
            "description": "Get historical performance metrics for recommendations.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Look-back period in days."},
                },
            },
        },
        {
            "name": "store_recommendation",
            "description": "Store a bet recommendation to the database.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "event_name": {"type": "string"},
                    "selection": {"type": "string"},
                    "odds": {"type": "number"},
                    "stake_units": {"type": "number"},
                    "confidence": {"type": "number", "description": "0–1 confidence score."},
                    "rationale": {"type": "string"},
                    "category": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": ["event_name", "selection", "odds", "stake_units", "confidence", "rationale"],
            },
        },
    ]


def _handle_tool(
    tool_name: str,
    tool_input: dict,
    fetchers: dict,
    stored_recommendations: list,
    period: str,
    db_session=None,
) -> str:
    """Execute a tool call and return result as string."""
    if tool_name == "fetch_markets":
        source = tool_input.get("source", "odds_api")
        fetcher = fetchers.get(source)
        if fetcher is None:
            return json.dumps({"error": f"Fetcher '{source}' not available."})
        filters = tool_input.get("filters", {})
        try:
            markets = fetcher.get_markets(**filters)
            return json.dumps([
                {
                    "id": m.id,
                    "category": m.category,
                    "event_name": m.event_name,
                    "starts_at": m.starts_at.isoformat() if m.starts_at else None,
                    "selections": [{"name": s.name, "odds": s.odds} for s in m.selections[:6]],
                    "source": m.source,
                }
                for m in markets[:20]
            ])
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    elif tool_name == "fetch_odds":
        source = tool_input.get("source", "odds_api")
        market_id = tool_input.get("market_id", "")
        fetcher = fetchers.get(source)
        if fetcher is None:
            return json.dumps({"error": f"Fetcher '{source}' not available."})
        try:
            market = fetcher.get_odds(market_id)
            return json.dumps({
                "id": market.id,
                "event_name": market.event_name,
                "selections": [{"name": s.name, "odds": s.odds} for s in market.selections],
            })
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    elif tool_name == "get_historical_performance":
        if db_session is None:
            return json.dumps({"message": "No historical data available yet."})
        try:
            from src.evaluator.performance import evaluate
            from datetime import timedelta
            days = tool_input.get("days", 30)
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=days)
            report = evaluate(db_session, start, end)
            return json.dumps({
                "roi": report.roi,
                "hit_rate": report.hit_rate,
                "units_profit": report.units_profit,
                "total_bets": report.total_bets,
            })
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    elif tool_name == "store_recommendation":
        stored_recommendations.append(tool_input)
        return json.dumps({"status": "stored", "recommendation": tool_input})

    return json.dumps({"error": f"Unknown tool: {tool_name}"})


def run_agent(
    fetchers: dict,
    period: str = "week",
    db_session=None,
    verbose: bool = True,
) -> list[dict]:
    """
    Run the agent mode recommendation loop.
    Returns list of stored recommendation dicts.
    """
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    tools = _make_tools()
    messages: list[dict] = [
        {
            "role": "user",
            "content": (
                f"Please analyse available betting markets for the next {period} "
                f"and identify the best value opportunities. "
                f"Fetch markets from available sources, analyse the odds, "
                f"and store your top recommendations with clear rationale. "
                f"Available sources: {list(fetchers.keys())}."
            ),
        }
    ]

    stored_recommendations: list[dict] = []
    max_turns = 10

    for turn in range(max_turns):
        if verbose:
            print(f"  [Agent turn {turn + 1}]")

        response = client.messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )

        # Add assistant response to history
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            if verbose:
                for block in response.content:
                    if hasattr(block, "text"):
                        print(f"  Agent: {block.text}")
            break

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    if verbose:
                        print(f"  -> Tool: {block.name}({json.dumps(block.input)[:120]})")
                    result = _handle_tool(
                        block.name,
                        block.input,
                        fetchers,
                        stored_recommendations,
                        period,
                        db_session,
                    )
                    if verbose:
                        print(f"  <- Result: {result[:120]}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            messages.append({"role": "user", "content": tool_results})

    return stored_recommendations
