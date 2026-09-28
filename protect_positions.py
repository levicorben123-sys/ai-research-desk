"""Keep filled positions protected overnight: swap DAY stop-loss legs for GTC stops.

Desk entries are DAY orders, so the stop-loss leg Alpaca attaches (OTO) is DAY
too and expires at the close, leaving a filled position unprotected overnight.
Run this shortly before the close. For each open long position that has a DAY
sell stop, it cancels that stop and places a GTC sell stop at the SAME price and
quantity. No new prices are introduced.

Usage:
    python protect_positions.py            # show the plan, ask YES, then act
    python protect_positions.py --dry-run  # show the plan only
"""

from __future__ import annotations

import argparse
import sys

from alpaca_paper import PAPER_URL, PaperClient


def plan(client: PaperClient) -> list[tuple[dict, dict]]:
    """(position, day stop order) pairs that need a GTC replacement."""
    positions = {p["symbol"]: p for p in client.positions() if float(p["qty"]) > 0}
    out = []
    for o in client.open_orders():
        for leg in [o] + (o.get("legs") or []):
            # Only ACTIVE stops (status new/accepted) protect the position. A
            # "held" leg belongs to a buy that has not filled yet; turning it
            # into a standalone stop would sell the existing shares, possibly
            # immediately if it sits above the market (nearly happened with
            # JPM's 341.65 breakout-leg stop on 2026-09-23).
            if (leg["symbol"] in positions and leg["side"] == "sell"
                    and leg["type"] in ("stop", "stop_limit")
                    and leg["time_in_force"] == "day"
                    and leg["status"] in ("new", "accepted")):
                out.append((positions[leg["symbol"]], leg))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    client = PaperClient()

    todo = plan(client)
    print(f"Endpoint: {PAPER_URL} (paper)")
    if not todo:
        print("Nothing to protect: no filled position with an expiring DAY stop.")
        return 0
    for pos, leg in todo:
        print(f"  {pos['symbol']}: position {pos['qty']} @ {pos['avg_entry_price']} | DAY stop "
              f"{leg['stop_price']} (id {leg['id']}) -> cancel, then GTC sell stop "
              f"{leg['stop_price']} qty {leg['qty']}")
    if args.dry_run:
        print("--dry-run: nothing changed.")
        return 0
    answer = input("\nType YES to swap these stops (anything else cancels): ")
    if answer.strip().upper() != "YES":
        print(f"Not changed (received {answer!r}).")
        return 0
    failed = 0
    for pos, leg in todo:
        client.cancel(leg["id"])
        try:
            res = client.submit({"symbol": leg["symbol"], "qty": leg["qty"], "side": "sell",
                                 "type": "stop", "stop_price": leg["stop_price"],
                                 "time_in_force": "gtc",
                                 "client_order_id": f"desk-protect-{leg['symbol']}-{leg['id'][:8]}"})
            print(f"Protected {res['symbol']}: GTC stop {res['stop_price']} qty {res['qty']} "
                  f"id {res['id']} status {res['status']}")
        except RuntimeError as exc:
            failed += 1
            print(f"FAILED to place GTC stop for {leg['symbol']} after cancelling the DAY stop: "
                  f"{exc}\n  -> {leg['symbol']} is UNPROTECTED; place a stop at "
                  f"{leg['stop_price']} by hand.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
