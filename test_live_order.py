"""
One-shot test: place a single 1-contract limit order on the cheapest
available Kalshi market, then report the result.
Run with: python test_live_order.py
"""
from __future__ import annotations
from src.fetchers.kalshi import KalshiFetcher

fetcher = KalshiFetcher()

# --- 1. Check balance ---
balance_cents = fetcher.get_balance()
print(f"Account balance: ${balance_cents / 100:.2f}")

# --- 2. Fetch markets and find cheapest tradeable side ---
print("\nFetching markets (Crypto, Economics, Financials)...")
fetcher.category_filter = ["Crypto", "Economics", "Financials"]
markets = fetcher.get_markets()
print(f"Found {len(markets)} markets.")

# Build list of (price_cents, ticker, side) for all selections
candidates = []
for market in markets:
    for sel in market.selections:
        side = sel.name.lower()  # "yes" or "no"
        price_key = f"{side}_ask"
        ask = sel.metadata.get(price_key, 0)
        if 1 <= ask <= 5:  # target 1–5 cent asks only
            candidates.append((ask, market.id, side, market.event_name))

candidates.sort()

if not candidates:
    print("\nNo markets with ask price 1–5¢ found. Widening to ≤10¢...")
    for market in markets:
        for sel in market.selections:
            side = sel.name.lower()
            ask = sel.metadata.get(f"{side}_ask", 0)
            if 1 <= ask <= 10:
                candidates.append((ask, market.id, side, market.event_name))
    candidates.sort()

if not candidates:
    print("No cheap markets found. Exiting.")
    raise SystemExit(1)

price_cents, ticker, side, event_name = candidates[0]
print(f"\nTarget: {ticker} ({side.upper()}) @ {price_cents}¢")
print(f"Event:  {event_name}")
print(f"Cost:   {price_cents}¢ for 1 contract")

confirm = input("\nPlace real order? (y/n): ").strip().lower()
if confirm != "y":
    print("Aborted.")
    raise SystemExit(0)

# --- 3. Place order ---
print(f"\nPlacing limit buy: {ticker} {side.upper()} x1 @ {price_cents}¢ ...")
try:
    order = fetcher.place_order(ticker=ticker, side=side, price_cents=price_cents, count=1)
    print(f"\nOrder response:")
    for k, v in order.items():
        print(f"  {k}: {v}")

    order_id = order.get("order_id") or order.get("id", "")
    status = order.get("status", "unknown")
    filled = order.get("filled_count", 0)

    print(f"\nStatus:  {status}")
    print(f"Filled:  {filled}/1 contract")
    print(f"OrderID: {order_id}")

    if status != "filled" and filled < 1:
        cancel = input("\nNot filled — cancel order? (y/n): ").strip().lower()
        if cancel == "y" and order_id:
            result = fetcher.cancel_order(order_id)
            print(f"Cancelled. Status: {result.get('status', result)}")
    else:
        print("\nOrder filled successfully.")

except Exception as exc:
    print(f"\nOrder failed: {exc}")
