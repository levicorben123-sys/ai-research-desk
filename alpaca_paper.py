"""Minimal Alpaca PAPER trading client. Refuses to talk to any other endpoint.

Usage:
    python alpaca_paper.py account          # cash, equity, open positions, open orders
    python alpaca_paper.py equity           # prints equity only (for daily_log.csv)
    python alpaca_paper.py cancel-stale     # cancel leftover buy entries from previous days
    python alpaca_paper.py is-trading-day   # exit 0 on a market day, 1 otherwise

Keys come from ALPACA_API_KEY / ALPACA_SECRET_KEY (environment or ~/trading-desk/.env).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

DESK_DIR = Path(__file__).resolve().parent
load_dotenv(DESK_DIR / ".env")

# Hard-coded on purpose: this desk never trades live.
PAPER_URL = "https://paper-api.alpaca.markets"

CONDITIONAL_TYPES = {"stop", "stop_limit", "trailing_stop"}
CONDITIONAL_CLASSES = {"bracket", "oto", "oco"}


class PaperClient:
    def __init__(self):
        key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY are not set "
                               "(put them in ~/trading-desk/.env).")
        self.base = PAPER_URL
        self.s = requests.Session()
        self.s.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    def _req(self, method: str, path: str, **kw):
        assert self.base == PAPER_URL, "refusing non-paper endpoint"
        r = self.s.request(method, f"{self.base}{path}", timeout=30, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Alpaca {method} {path} -> {r.status_code}: {r.text}")
        return r.json() if r.text else None

    def account(self) -> dict:
        return self._req("GET", "/v2/account")

    def positions(self) -> list[dict]:
        return self._req("GET", "/v2/positions")

    def open_orders(self) -> list[dict]:
        return self._req("GET", "/v2/orders", params={"status": "open", "nested": "true",
                                                      "limit": 500})

    def submit(self, order: dict) -> dict:
        return self._req("POST", "/v2/orders", json=order)

    def cancel(self, order_id: str):
        return self._req("DELETE", f"/v2/orders/{order_id}")


def alpaca_symbol(ticker: str) -> str:
    """Yahoo-style crypto tickers (BTC-USD) become Alpaca pairs (BTC/USD)."""
    t = ticker.upper()
    for quote in ("USDT", "USDC", "USD"):
        if t.endswith(f"-{quote}"):
            return f"{t[: -len(quote) - 1]}/{quote}"
    return t


def cancel_stale_conditional(client: PaperClient) -> list[str]:
    """Cancel yesterday's leftover BUY entries: conditional ones, plus any desk order.

    Stock desk orders are DAY orders and expire on their own; this mainly clears
    crypto entries (Alpaca crypto has no DAY time-in-force). Sell-side orders are
    never touched, so a stop-loss protecting a filled position stays in place.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    cancelled = []
    for o in client.open_orders():
        if o.get("side") != "buy":
            continue
        conditional = (o.get("type") in CONDITIONAL_TYPES
                       or o.get("order_class") in CONDITIONAL_CLASSES)
        from_desk = (o.get("client_order_id") or "").startswith("desk-")
        if (conditional or from_desk) and (o.get("submitted_at") or "")[:10] < today:
            client.cancel(o["id"])
            cancelled.append(f"{o['symbol']} {o['side']} {o['type']} "
                             f"{o.get('order_class') or 'simple'} id={o['id']}")
    return cancelled


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "account"
    client = PaperClient()
    if cmd == "equity":
        print(client.account()["equity"])
    elif cmd == "is-trading-day":
        # Exit 0 on a market day (per Alpaca's calendar), 1 on weekends/holidays.
        today = datetime.now().date().isoformat()
        days = client._req("GET", "/v2/calendar", params={"start": today, "end": today})
        print("trading day" if days else "market closed today")
        return 0 if days else 1
    elif cmd == "cancel-stale":
        done = cancel_stale_conditional(client)
        print("\n".join(f"Cancelled {d}" for d in done) or "No stale conditional orders.")
    elif cmd == "account":
        a = client.account()
        print(f"Endpoint: {PAPER_URL}  (paper)")
        print(f"Account {a['account_number']}  status={a['status']}")
        print(f"Cash: ${float(a['cash']):,.2f}   Equity: ${float(a['equity']):,.2f}   "
              f"Buying power: ${float(a['buying_power']):,.2f}")
        pos = client.positions()
        print(f"Open positions: {len(pos)}")
        for p in pos:
            print(f"  {p['symbol']}: {p['qty']} @ {p['avg_entry_price']} "
                  f"(market value ${float(p['market_value']):,.2f})")
        orders = client.open_orders()
        print(f"Open orders: {len(orders)}")
        for o in orders:
            print(f"  {o['symbol']} {o['side']} {o['qty']} {o['type']} "
                  f"limit={o.get('limit_price')} stop={o.get('stop_price')} tif={o['time_in_force']}")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
