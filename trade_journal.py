"""Journal of the desk's filled trades and how their stop-losses worked out.

Rebuilt from the Alpaca PAPER order history each time (no separate state to go
stale): every filled desk entry (client_order_id "desk-..."), the stop that
protected it, whether that stop was hit, and - for stops that were hit - what
the price did in the 5 trading days afterwards. That last part is the feedback
for stop placement: a stop that fired and then the price recovered above the
entry was probably too tight; a large loss that kept falling means the stop did
its job.

The journal is shown to the desk's decision agents through the repo's portfolio
context (see research_floor.py), so each new stop decision can draw on it.

Usage:
    python trade_journal.py        # print the journal and stop statistics
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from alpaca_paper import PaperClient
from mandate import DESK_CAPITAL, MANDATE_START

LOOKBACK_DAYS = 120
RECOVERY_BARS = 5


def _orders(client: PaperClient) -> list[dict]:
    after = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return client._req("GET", "/v2/orders", params={"status": "all", "nested": "true",
                                                    "limit": 500, "after": after,
                                                    "direction": "asc"})


def _atr(ticker: str, day: str, n: int = 14) -> float | None:
    from trade_from_verdict import atr
    value, _ = atr(ticker, day, n)
    return value


def _after_stop(ticker: str, day: str, entry: float) -> tuple[float | None, bool | None]:
    """Highest close in the RECOVERY_BARS after the stop day, and whether it got back above entry."""
    import yfinance as yf

    start = datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)
    end = start + timedelta(days=RECOVERY_BARS * 2 + 5)
    h = yf.Ticker(ticker).history(start=start.strftime("%Y-%m-%d"),
                                  end=end.strftime("%Y-%m-%d")).dropna(subset=["Close"])
    if len(h) < RECOVERY_BARS:
        return None, None
    best = float(h["Close"].iloc[:RECOVERY_BARS].max())
    return round(best, 2), best > entry


def build(client: PaperClient) -> list[dict]:
    orders = _orders(client)
    protects = [o for o in orders if (o.get("client_order_id") or "").startswith("desk-protect-")]
    trades = []
    for o in orders:
        cid = o.get("client_order_id") or ""
        if not cid.startswith("desk-") or cid.startswith("desk-protect-") or o["side"] != "buy":
            continue
        if not o.get("filled_at") or float(o.get("filled_qty") or 0) <= 0:
            continue
        sym, entry = o["symbol"], float(o["filled_avg_price"])
        entry_day = o["filled_at"][:10]
        leg = next((leg for leg in (o.get("legs") or []) if leg.get("stop_price")), None)
        stop_order = leg
        # If the DAY stop leg was swapped for a GTC stop by protect_positions.py, follow that.
        if leg and leg["status"] == "canceled":
            stop_order = next((p for p in protects if p["symbol"] == sym
                               and p["submitted_at"] >= leg.get("canceled_at", "")), leg)
        stop = float(stop_order["stop_price"]) if stop_order else None
        a = _atr(sym, entry_day)
        t = {"symbol": sym, "entry_day": entry_day, "entry": round(entry, 2),
             "qty": float(o["filled_qty"]), "entry_type": o["type"], "stop": stop,
             "stop_atr": round((entry - stop) / a, 2) if (stop and a) else None,
             "status": "open", "in_book": entry_day >= MANDATE_START}
        if stop_order and stop_order.get("filled_at"):
            exit_px = float(stop_order["filled_avg_price"])
            t.update(status="stopped out", exit_day=stop_order["filled_at"][:10],
                     exit=round(exit_px, 2), pnl=round((exit_px - entry) * t["qty"], 2))
            best, recovered = _after_stop(sym, t["exit_day"], entry)
            t.update(best_close_after=best, recovered_above_entry=recovered)
        elif stop_order and stop_order["status"] in ("canceled", "expired") and stop_order is leg:
            t["status"] = "open, stop expired"
        trades.append(t)
    return trades


def book(client: PaperClient, trades: list[dict]) -> dict:
    """The desk's own book: DESK_CAPITAL plus P&L of trades entered since MANDATE_START.

    Trades before the mandate (e.g. the 2026-09-23 JPM position, sized on the full
    paper balance) are "legacy": still real positions, but outside the book.
    """
    prices = {p["symbol"]: float(p["current_price"]) for p in client.positions()}
    mine = [t for t in trades if t.get("in_book")]
    realized = sum(t["pnl"] for t in mine if t["status"] == "stopped out")
    open_ = [t for t in mine if t["status"] != "stopped out"]
    cost = sum(t["entry"] * t["qty"] for t in open_)
    unrealized = sum((prices.get(t["symbol"], t["entry"]) - t["entry"]) * t["qty"] for t in open_)
    equity = DESK_CAPITAL + realized + unrealized
    return {"capital": DESK_CAPITAL, "realized": round(realized, 2),
            "unrealized": round(unrealized, 2), "equity": round(equity, 2),
            "cash": round(DESK_CAPITAL + realized - cost, 2),
            "open": {t["symbol"]: t["entry"] * t["qty"] for t in open_},
            "legacy": [t for t in trades if not t.get("in_book") and t["status"] != "stopped out"]}


def summary_lines(trades: list[dict], limit: int = 12) -> list[str]:
    """Plain-language lines for the desk's prompt."""
    if not trades:
        return ["No filled desk trades yet, so there is no stop-loss track record."]
    lines = []
    for t in trades[-limit:]:
        base = (("" if t.get("in_book") else "[legacy, pre-mandate] ")
                + f"{t['entry_day']} {t['symbol']} bought {t['qty']:g} @ {t['entry']} "
                f"({t['entry_type']}), stop {t['stop']}"
                + (f" = {t['stop_atr']} x ATR(14) below entry" if t.get("stop_atr") is not None else ""))
        if t["status"] == "stopped out":
            rec = t.get("recovered_above_entry")
            after = ("price recovered above the entry within 5 days (stop likely too tight)"
                     if rec else "price did not recover above the entry within 5 days"
                     if rec is False else "5-day aftermath not known yet")
            base += f": STOPPED OUT {t['exit_day']} @ {t['exit']}, P&L {t['pnl']:+.2f}; {after}"
        else:
            base += f": {t['status']}"
        lines.append(base)
    stopped = [t for t in trades if t["status"] == "stopped out"]
    if stopped:
        known = [t for t in stopped if t.get("recovered_above_entry") is not None]
        too_tight = sum(1 for t in known if t["recovered_above_entry"])
        lines.append(f"Stops hit: {len(stopped)} of {len(trades)} trades; of those with a known "
                     f"aftermath, {too_tight}/{len(known)} recovered above entry within 5 days. "
                     f"Total stopped-out P&L {sum(t['pnl'] for t in stopped):+.2f}.")
    return lines


def main() -> int:
    client = PaperClient()
    if "--equity" in sys.argv:          # desk book equity only (for daily_log.csv)
        print(book(client, build(client))["equity"])
        return 0
    trades = build(client)
    print("\n".join(summary_lines(trades, limit=1000)))
    b = book(client, trades)
    print(f"\nDesk book: capital ${b['capital']:,.2f} | equity ${b['equity']:,.2f} | "
          f"cash ${b['cash']:,.2f} | realized {b['realized']:+.2f} | "
          f"unrealized {b['unrealized']:+.2f} | legacy positions outside the book: "
          f"{len(b['legacy'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
