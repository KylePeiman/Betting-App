# Strategy Overhaul + Polymarket Integration

## Context

Live trading on Kalshi has produced real losses from **partial series arbs** — entering 2-3 bucket positions out of 75, which is mathematically zero-EV and caused two full-stake losses in a single session ($1.75 lost). Research confirms:
- True guaranteed series arbs (all buckets liquid, sum < 100¢) don't exist in practice on Kalshi
- Binary arbs (YES + NO < 100¢ on same market) exist briefly but are extremely rare
- **Cross-platform arb between Kalshi and Polymarket** is the most documented real edge: 1.5–4.5% spreads reported by practitioners on the same events priced differently across platforms
- The last-second strategy is working (2/2 wins in sim) and should continue

The user has gained access to Polymarket. This plan disables partial arbs and adds Polymarket as a second data source for cross-platform arb scanning.

---

## Track 1: Disable Partial Series Arbs (2 lines)

**Problem:** In `src/engine/live_sim.py` and `src/cli.py`, `min_leg_cost_cents` defaults to `90.0`. Since partial arbs always have `total_cost < 100`, they always pass the `>= 90` threshold. Only `guaranteed=True` arbs should be entered.

**Fix:** Change default from `90.0` → `101.0` in two places:
- `src/engine/live_sim.py` — `run_live_simulation()` signature, `min_leg_cost_cents: float = 101.0`
- `src/cli.py` — `--min-leg-cost` Click option default + update help text

`guaranteed=True` logic in `arbitrage.py` is already correct (requires all legs covered + exhaustive prefix). No changes needed there.

---

## Track 2: Polymarket Fetcher

**File to create:** `src/fetchers/polymarket.py`

Implements `BaseFetcher`. No auth needed for read-only market data.

### APIs
- `GET https://gamma-api.polymarket.com/markets?closed=false&limit=100&offset=N` — market metadata (offset pagination; stop when page < limit)
- `GET https://clob.polymarket.com/book?token_id=<token_id>` — order book per token (best ask = min of asks list; prices are 0.0–1.0, multiply × 100 for cents)

### Key parsing rules
- Binary markets only: skip if `tokens` list doesn't have exactly one "Yes" and one "No" entry
- Skip if `active=False` or `accepting_orders=False`
- `id` = `condition_id` (hex string)
- `event_name` = `question` field
- `starts_at` = parsed `end_date_iso` as UTC datetime
- `source` = `"polymarket"`
- `Selection.metadata` must store `yes_ask` and `no_ask` as **integer cents** (same as Kalshi) so `scan_binary_arb()` works on Polymarket markets directly
- `metadata` dict: `{"condition_id", "yes_token_id", "no_token_id", "yes_ask", "no_ask", "yes_bid", "no_bid", "volume_24hr"}`

### Performance optimization (critical)
Two-pass approach inside `get_markets()`:
1. Fetch all market metadata from Gamma API (no CLOB calls yet)
2. Filter candidates by category
3. Fetch CLOB book only for filtered markets
4. Add `time.sleep(0.05)` between CLOB calls to avoid hammering

This avoids fetching order books for thousands of irrelevant markets.

### Category mapping
Map Polymarket tags (list of strings) → internal category strings matching Kalshi's convention (`"crypto"`, `"politics"`, `"economics"`, etc.)

---

## Track 3: Cross-Platform Arb Scanner

**File to create:** `src/engine/cross_arb.py`

### Data structures
```python
@dataclass
class MatchedPair:
    kalshi_market: Market
    poly_market: Market
    match_score: float    # 0.0–1.0
    match_reason: str

@dataclass
class CrossArbOpportunity:
    direction: str        # "kalshi_yes" | "poly_yes"
    kalshi_market: Market
    poly_market: Market
    kalshi_leg: dict      # {source, side, price_cents}
    poly_leg: dict
    total_cost_cents: float
    profit_cents: float
    profit_pct: float
    closes_at: datetime | None
    match_score: float
    settlement_risk: str  # "low" | "medium" | "high"
```

### `match_markets(kalshi_markets, poly_markets, min_score=0.85) -> list[MatchedPair]`

Composite similarity score (weighted):
- **Question text (0.60):** `difflib.SequenceMatcher` ratio on normalized strings (lowercase, strip punctuation, remove filler words: will/the/a/by/on/in)
- **Expiry proximity (0.30):** `max(0, 1 - hours_diff / 48)` — same hour = 1.0, 48h apart = 0.0
- **Category match (0.10):** 1.0 if same, 0.5 if related, 0.0 if different

One-to-one matching: each Kalshi market maps to at most one Polymarket market (highest score wins).

`settlement_risk` classification:
- `match_score >= 0.90` → `"low"`
- `match_score >= 0.80` → `"medium"`
- below → `"high"` (never enter, even in sim)

### `scan_cross_arb(pairs, min_profit_cents=2.0) -> list[CrossArbOpportunity]`

For each matched pair, check both directions:
- `kalshi_yes_ask + poly_no_ask < 100` → buy YES Kalshi, NO Polymarket
- `poly_yes_ask + kalshi_no_ask < 100` → buy YES Polymarket, NO Kalshi

Filter by `min_profit_cents`. Skip `settlement_risk == "high"` entries.

**IMPORTANT:** Add prominent docstring warning — Kalshi and Polymarket may use different resolution oracles and can settle differently on contested events. Cross-arb is only safe when both resolve mechanically to the same underlying fact (e.g., "Did X happen? Yes/No").

---

## Track 4: CLI — New `cross-arb` Command Group

**File:** `src/cli.py`

Add top-level `@cli.group() def cross_arb()` with one subcommand:

```
python -m src.cli cross-arb scan
  --categories TEXT       (default: "Crypto,Economics,Financials")
  --min-profit FLOAT      (default: 2.0)
  --min-match FLOAT       (default: 0.85)
  --show-unmatched        flag: also show Kalshi markets with no Polymarket match
```

Execution flow:
1. Init both fetchers
2. `kalshi_markets = kalshi_fetcher.get_markets()`
3. `poly_markets = poly_fetcher.get_markets()`
4. `pairs = match_markets(kalshi_markets, poly_markets, min_score=min_match)`
5. `opps = scan_cross_arb(pairs, min_profit_cents=min_profit)`
6. Print results table

Also add `--polymarket` flag to existing `live` command → passes `use_polymarket=True` to `run_live_simulation()`.

---

## Track 5: Live Sim Integration

**File:** `src/engine/live_sim.py`

Add `use_polymarket: bool = False` to `run_live_simulation()` signature.

When enabled:
- Init `PolymarketFetcher` once before the main loop
- In each scan cycle (section B), after existing arb scan, add section B2:
  - Fetch poly markets for matched categories
  - Run `match_markets()` + `scan_cross_arb()`
  - Enter via `_enter_cross_arb()` helper (sim only — if `use_live_orders=True`, log warning and skip)

**`_enter_cross_arb()` helper:**
- Creates `SimPosition` with `arb_type="cross"`
- `ticker` = `f"CROSS_{kalshi_market.id}"`
- Both legs stored in `pos.legs`
- Settlement (in `_settle_open_positions`): use Kalshi leg as canonical oracle for paper-trade purposes (add comment explaining this)
- Min profit threshold for live: raise to 3.0¢ (extra buffer for slippage)

---

## Track 6: Settings + Registration

**`config/settings.py`:** Add `POLYMARKET_API_KEY: str = os.getenv("POLYMARKET_API_KEY", "")` (optional, not required for reads)

**`.env.example`:** Add `POLYMARKET_API_KEY=` with comment

**`src/engine/pipeline.py`:** Add `"polymarket": PolymarketFetcher` to `FETCHER_MAP`

---

## Files Modified
| File | Change |
|---|---|
| `src/engine/live_sim.py` | Default `min_leg_cost_cents=101.0`; add `use_polymarket` param + `_enter_cross_arb()` |
| `src/cli.py` | Default `--min-leg-cost=101.0`; add `cross-arb scan` command; add `--polymarket` to `live` |
| `config/settings.py` | Add `POLYMARKET_API_KEY` |
| `.env.example` | Add `POLYMARKET_API_KEY=` |
| `src/engine/pipeline.py` | Register `PolymarketFetcher` |

## Files Created
| File | Purpose |
|---|---|
| `src/fetchers/polymarket.py` | Polymarket fetcher (BaseFetcher implementation) |
| `src/engine/cross_arb.py` | Cross-platform arb scanner + dataclasses |

---

## Implementation Order
1. Partial arb fix (2 lines, test immediately)
2. `config/settings.py` + `.env.example`
3. `src/fetchers/polymarket.py` (standalone, testable in isolation)
4. `src/engine/cross_arb.py`
5. `src/engine/pipeline.py` registration
6. `src/cli.py` — new `cross-arb scan` command
7. `src/engine/live_sim.py` — `use_polymarket` param + helper

---

## Verification

1. **Partial arb fix:** Start sim, confirm no `PARTIAL` log lines appear
2. **Polymarket fetcher:** Run `python -m src.cli run --sources polymarket` — should fetch markets and print count
3. **Cross-arb scan:** Run `python -m src.cli cross-arb scan --min-profit 1.0` — should complete without error; inspect match quality in output
4. **Unit tests:** Write `test_cross_arb.py` covering: `match_markets()` scoring, `scan_cross_arb()` profit detection, `PolymarketFetcher._fetch_book()` price parsing
5. **Live sim with polymarket:** Run `python -m src.cli live --simulate --last-second --polymarket` — confirm both last-second and cross-arb entries appear in log

---

## Out of Scope (Deferred)
- Polymarket live order placement (requires USDC on Polygon wallet — different infra from Kalshi cash)
- Automated market matching validation (human review of matched pairs recommended before live trading)
