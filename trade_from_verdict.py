"""Turn a research-desk verdict into an Alpaca PAPER order plan, and submit it only on YES.

Usage:
    python trade_from_verdict.py --ticker NVDA [--date YYYY-MM-DD] [--dry-run]
    python trade_from_verdict.py --file reports/NVDA-2026-09-22.json [--dry-run]

Rules (from the desk brief):
  * Prices come only from levels the desk wrote in its report (verified against
    the report text by research_floor.py), plus today's close for the Buy rule.
  * Limit or stop-limit orders only. Never market orders.
  * Orders are GTC (user decision 2026-09-23) so the attached stop-loss leg,
    which inherits the entry's time-in-force, keeps protecting a filled position
    overnight. Unfilled entries still last about a day: run_daily.sh's morning
    cleanup cancels leftover desk BUY orders and never touches sell-side stops.
  * Sizing (mandate.py, user rules 2026-09-23) is against the desk's own $5,000
    book, not the paper balance: hitting the stop loses at most 2% of the book,
    and no ticker exceeds 20% of the book across positions, open and proposed
    orders. (Replaces the brief's "10% of cash per order".)
  * Hold or Overweight (Overweight added by user decision 2026-09-23) + breakout
    trigger + pullback zone: stop-limit buy at the breakout (limit 0.4% above it)
    plus a limit buy in the lower half of the pullback zone. Each leg is skipped
    if it is on the wrong side of the latest close (it would fill immediately).
  * Buy with medium or high conviction: limit buy at today's close.
  * Every buy needs a desk target at least MIN_REWARD_RISK (1.5) x its risk
    (entry - stop); otherwise it is dropped (mandate 2026-09-23).
  * Every buy carries a stop-loss leg (Alpaca OTO). The DESK chooses the stop
    (user rule 2026-09-23, replacing the earlier 2xATR formula); the desk sees its
    own stop-loss track record each run (trade_journal.py via research_floor.py).
    Only a safety range is enforced: 0.5x-3x ATR(14) below the entry; outside it
    the stop is moved to the nearest edge and the table says so. No desk stop
    below the entry: no order. Crypto cannot carry an attached stop on Alpaca,
    so crypto buys are not placed.
  * Sell with no position: no action.
  * Every other case (Underweight, Sell with a position, REVIEW, missing
    levels) takes no action and says why.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from alpaca_paper import PAPER_URL, PaperClient, alpaca_symbol

DESK_DIR = Path(__file__).resolve().parent
REPORTS_DIR = DESK_DIR / "reports"

BREAKOUT_LIMIT_BUFFER = 0.004
STOP_MIN_ATR = 0.5
STOP_MAX_ATR = 3.0
ATR_DAYS = 14


def load_verdict(args) -> dict:
    path = Path(args.file) if args.file else REPORTS_DIR / f"{args.ticker.upper()}-{args.date}.json"
    if not path.exists():
        sys.exit(f"No verdict file at {path}. Run research_floor.py first.")
    return json.loads(path.read_text(encoding="utf-8"))


def todays_close(ticker: str, day: str) -> tuple[float | None, str]:
    """Last daily close on or before ``day`` from Yahoo Finance (the desk's own data path)."""
    import yfinance as yf

    end = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    start = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
    # Yahoo sometimes returns the latest bar with no close yet (NaN); drop it
    # so it can never become a limit price or a comparison value.
    hist = yf.Ticker(ticker).history(start=start, end=end).dropna(subset=["Close"])
    if hist.empty:
        return None, "no close available"
    bar = hist.index[-1].strftime("%Y-%m-%d")
    close = round(float(hist["Close"].iloc[-1]), 2)
    return close, f"market data: Yahoo Finance daily close for {bar} = {close}"


def atr(ticker: str, day: str, n: int = ATR_DAYS) -> tuple[float | None, str]:
    """Average True Range over the last ``n`` daily bars up to ``day`` (Yahoo Finance)."""
    import pandas as pd
    import yfinance as yf

    end = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    start = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=n * 3)).strftime("%Y-%m-%d")
    h = yf.Ticker(ticker).history(start=start, end=end).dropna(subset=["High", "Low", "Close"])
    if len(h) <= n:
        return None, "not enough price history for ATR"
    prev = h["Close"].shift()
    tr = pd.concat([h["High"] - h["Low"], (h["High"] - prev).abs(), (h["Low"] - prev).abs()],
                   axis=1).max(axis=1)
    value = round(float(tr.iloc[-n:].mean()), 2)
    return value, f"ATR({n}) = {value} through {h.index[-1]:%Y-%m-%d} (Yahoo Finance)"


def attach_stop(o: dict, v: dict, atr_value: float, atr_src: str) -> bool:
    """The desk chooses the stop; only a safety range is enforced (user rule 2026-09-23).

    The stop is the desk's own stop-loss (from its report), kept between
    STOP_MIN_ATR and STOP_MAX_ATR x ATR(14) below the entry: tighter than that is
    inside normal daily noise, wider is an outsized loss. A stop outside the range
    is moved to the nearest edge and the table says so. No desk stop: no order.
    """
    entry = o["limit"]
    desk, desk_src = level(v, "stop")
    if desk is None or desk >= entry:
        return False
    widest = round(entry - STOP_MAX_ATR * atr_value, 2)
    tightest = round(entry - STOP_MIN_ATR * atr_value, 2)
    dist = (entry - desk) / atr_value
    if desk < widest:
        o["stop"] = widest
        o["sources"].append(f"stop: desk stop {desk} is {dist:.2f}x ATR below entry, beyond the "
                            f"{STOP_MAX_ATR}x safety limit -> moved to {widest} ({atr_src}); "
                            f"desk line: {desk_src}")
    elif desk > tightest:
        o["stop"] = tightest
        o["sources"].append(f"stop: desk stop {desk} is only {dist:.2f}x ATR below entry, inside the "
                            f"{STOP_MIN_ATR}x noise band -> moved to {tightest} ({atr_src}); "
                            f"desk line: {desk_src}")
    else:
        o["stop"] = desk
        o["sources"].append(f"stop: desk's own stop, {dist:.2f}x ATR below entry: {desk_src}")
    return True


def level(v: dict, name: str) -> tuple[float | None, str | None]:
    lv = (v.get("levels") or {}).get(name) or {}
    return lv.get("value"), lv.get("report_line")


def price_fmt(p: float, crypto: bool) -> str:
    # Alpaca rejects sub-penny prices on stocks >= $1.
    return f"{p:.2f}" if not crypto or p >= 1 else f"{p:.6f}"


def plan_orders(v: dict) -> tuple[list[dict], list[str]]:
    """Return (orders, notes). Orders carry price levels and their sources, not size."""
    verdict = v["verdict"]
    conviction = (v.get("conviction") or "").lower()
    crypto = v.get("asset_type") == "crypto"
    orders, notes = [], []

    if verdict in ("Hold", "Overweight"):
        # Overweight uses the Hold rule (user decision, 2026-09-23).
        # Each leg stands on its own (user decision 2026-09-24): a pullback zone alone
        # gives a dip buy, a breakout trigger alone gives a breakout buy, both give both.
        # Replaces the brief's "breakout trigger AND pullback zone" requirement, which
        # blocked dip-only "buy the discount, don't chase" plans.
        brk, brk_src = level(v, "breakout_trigger")
        lo, lo_src = level(v, "pullback_zone_low")
        hi, hi_src = level(v, "pullback_zone_high")
        has_zone = lo is not None and hi is not None
        if brk is None and not has_zone:
            notes.append(f"{verdict}: the desk stated neither a breakout trigger nor a full "
                         "pullback zone, so no orders.")
            return orders, notes
        # A breakout buy only makes sense above the market and a pullback buy
        # only below it; otherwise the stop triggers at once or the limit fills
        # at once, i.e. an immediate buy nobody asked for.
        close, close_src = todays_close(v["ticker"], v["date"])
        if close is None:
            notes.append(f"{verdict}: no close price to sanity-check the levels against; no orders.")
            return orders, notes
        notes.append(f"Reference {close_src}.")
        if brk is not None:
            if brk > close:
                orders.append({
                    "type": "stop_limit", "trigger": brk,
                    "limit": brk * (1 + BREAKOUT_LIMIT_BUFFER), "stop": None,
                    "sources": [f"trigger: {brk_src}", f"limit: trigger +0.4% (rule)"],
                })
            else:
                notes.append(f"Skipped breakout order: trigger {brk} is not above the close {close}.")
        if has_zone:
            lo, hi = min(lo, hi), max(lo, hi)
            # Centre of the zone's lower half: between the zone low and its midpoint.
            pull_limit = lo + (hi - lo) / 4
            if pull_limit < close:
                orders.append({
                    "type": "limit", "trigger": None, "limit": pull_limit, "stop": None,
                    "sources": [f"zone low: {lo_src}", f"zone high: {hi_src}",
                                "limit: 25% up the zone = middle of its lower half (rule)"],
                })
            else:
                notes.append(f"Skipped pullback order: limit {pull_limit:.2f} is not below the "
                             f"close {close}.")
    elif verdict == "Buy":
        if conviction not in ("medium", "high"):
            notes.append(f"Buy with {conviction or 'unknown'} conviction: rule needs medium or "
                         "higher, so no order.")
            return orders, notes
        close, close_src = todays_close(v["ticker"], v["date"])
        if close is None:
            notes.append("Buy: no close price available, so no order.")
            return orders, notes
        orders.append({
            "type": "limit", "trigger": None, "limit": close, "stop": None,
            "sources": [f"limit: {close_src}"],
        })
    elif verdict == "Sell":
        notes.append("SELL_CHECK_POSITION")  # resolved against the account in main()
    elif verdict == "Underweight":
        notes.append("Underweight: no rule defined (nothing to reduce without a position), "
                     "so no action.")
    else:
        notes.append(f"Verdict {verdict!r} is not tradeable (REVIEW = unreadable rating); no action.")

    if orders and crypto:
        notes.append("Crypto: Alpaca cannot attach a stop-loss leg to crypto orders, and every "
                     "buy must carry one, so no orders.")
        return [], notes
    if orders:
        atr_value, atr_src = atr(v["ticker"], v["date"])
        if atr_value is None:
            notes.append(f"No stop could be computed ({atr_src}), so no orders.")
            return [], notes
        from mandate import MIN_REWARD_RISK
        target, target_src = level(v, "target")
        kept = []
        for o in orders:
            if not attach_stop(o, v, atr_value, atr_src):
                notes.append(f"Dropped {o['type']} @ {o['limit']:.2f}: the desk gave no stop-loss "
                             "below this entry, and every buy must carry the desk's own stop.")
                continue
            # Minimum reward:risk (mandate, adapted from Creamer's 1.5-2R targets).
            risk = o["limit"] - o["stop"]
            if target is None:
                notes.append(f"Dropped {o['type']} @ {o['limit']:.2f}: the desk stated no target, "
                             f"so reward:risk cannot be checked (min {MIN_REWARD_RISK}R).")
                continue
            rr = (target - o["limit"]) / risk
            if rr < MIN_REWARD_RISK:
                notes.append(f"Dropped {o['type']} @ {o['limit']:.2f}: target {target} is only "
                             f"{rr:.2f}R (risk {risk:.2f}/sh), below the {MIN_REWARD_RISK}R minimum.")
                continue
            o["sources"].append(f"target: {target} = {rr:.2f}R: {target_src}")
            kept.append(o)
        orders = kept
    return orders, notes


def replaceable(client, v) -> list[dict]:
    """This ticker's open desk BUY entries (the ones --replace cancels)."""
    plain = alpaca_symbol(v["ticker"]).replace("/", "")
    return [o for o in client.open_orders()
            if o["symbol"].replace("/", "") == plain and o["side"] == "buy"
            and (o.get("client_order_id") or "").startswith(f"desk-{v['ticker']}-")]


def size_orders(orders, v, client, notes, replacing=()):
    """Quantity per order from the desk mandate (mandate.py, user rules 2026-09-23).

    Sized against the desk's own book (DESK_CAPITAL plus its P&L, trade_journal.book),
    not the full paper balance:
      * risk: qty x (entry - stop) <= RISK_PER_TRADE x book equity
      * ticker cap: all exposure to the ticker (positions incl. legacy + open buys
        + this plan) <= TICKER_CAP x book equity
      * cash: never more than the book's free cash
    """
    import trade_journal
    from mandate import RISK_PER_TRADE, SIZE_CAPS, TICKER_CAP

    crypto = v.get("asset_type") == "crypto"
    symbol = alpaca_symbol(v["ticker"])
    if client is None:
        for o in orders:
            o["qty"], o["value"] = None, None
        notes.append("Quantities need Alpaca PAPER keys (book / exposure unknown).")
        return orders

    b = trade_journal.book(client, trade_journal.build(client))
    cash, equity = b["cash"], b["equity"]
    plain = symbol.replace("/", "")
    exposure = sum(abs(float(p["market_value"])) for p in client.positions()
                   if p["symbol"].replace("/", "") == plain)
    skip = {o["id"] for o in replacing}
    exposure += sum(float(o["qty"] or 0) * float(o.get("limit_price") or o.get("stop_price") or 0)
                    for o in client.open_orders()
                    if o["symbol"].replace("/", "") == plain and o["side"] == "buy"
                    and o["id"] not in skip)
    # The desk chooses the size (user rule 2026-09-23): its stated % of book, up to
    # the ceiling for its conviction; if it states none, the default 20% cap applies.
    conviction = (v.get("conviction") or "").lower()
    ceiling = SIZE_CAPS.get(conviction, TICKER_CAP)
    desk_pct, desk_pct_src = level(v, "position_size_pct")
    if desk_pct is not None and 0 < desk_pct <= 100:
        target = min(desk_pct / 100, ceiling)
        notes.append(f"Desk-chosen size {desk_pct:g}% of book ({conviction} conviction ceiling "
                     f"{ceiling:.0%}) -> using {target:.0%}. Desk line: {desk_pct_src}")
    else:
        target = min(TICKER_CAP, ceiling)
        notes.append(f"Desk stated no size as % of book -> default {target:.0%} "
                     f"({conviction or 'unknown'} conviction ceiling {ceiling:.0%}).")
    room = target * equity - exposure
    risk_budget = RISK_PER_TRADE * equity
    notes.append(f"Desk book: equity ${equity:,.2f}, free cash ${cash:,.2f}. Max risk per trade "
                 f"${risk_budget:,.2f} ({RISK_PER_TRADE:.0%}); {symbol} exposure ${exposure:,.2f}, "
                 f"room under the {target:.0%} size limit ${max(room, 0):,.2f}.")

    sized = []
    for o in orders:
        price, stop = o["limit"], o["stop"]
        per_share_risk = price - stop
        limits = {"2% risk": risk_budget / per_share_risk, "size limit": room / price,
                  "book cash": cash / price}
        raw = min(limits.values())
        # Whole shares for stocks (fractional stop-limit / OTO orders are not
        # supported); crypto allows fractions.
        qty = (math.floor(raw * 1e6) / 1e6) if crypto else math.floor(raw)
        binding = min(limits, key=limits.get)
        if qty <= 0:
            notes.append(f"Dropped {o['type']} @ {price:.2f}: not even 1 share fits the "
                         f"{binding} limit (${price:,.2f}/share).")
            continue
        o["qty"], o["value"] = qty, qty * price
        o["sources"].append(f"size: {qty} sh, limited by {binding}; loss if stopped "
                            f"${qty * per_share_risk:,.2f} "
                            f"({qty * per_share_risk / equity:.2%} of book)")
        room -= o["value"]
        cash -= o["value"]
        sized.append(o)
    return sized


def print_table(v, orders, crypto):
    cols = ["Ticker", "Verdict", "Order type", "Trigger", "Limit", "Quantity",
            "Dollar value", "Attached stop"]
    rows = []
    for o in orders:
        rows.append([v["ticker"], v["verdict"],
                     o["type"].replace("_", "-") + " GTC",
                     price_fmt(o["trigger"], crypto) if o["trigger"] else "-",
                     price_fmt(o["limit"], crypto),
                     str(o["qty"]) if o["qty"] is not None else "n/a",
                     f"${o['value']:,.2f}" if o["value"] is not None else "n/a",
                     price_fmt(o["stop"], crypto) if o["stop"] else "-"])
    widths = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate(cols)]
    line = " | ".join(c.ljust(w) for c, w in zip(cols, widths))
    print(line + "\n" + "-+-".join("-" * w for w in widths))
    for r in rows:
        print(" | ".join(x.ljust(w) for x, w in zip(r, widths)))
    print("\nSource of each level (exact report line):")
    for n, o in enumerate(orders, 1):
        for s in o["sources"]:
            print(f"  [{n}] {s}")


def to_alpaca(o, v) -> dict:
    crypto = v.get("asset_type") == "crypto"
    body = {
        "symbol": alpaca_symbol(v["ticker"]),
        "qty": str(o["qty"]),
        "side": "buy",
        "type": o["type"],
        # GTC so the OTO stop-loss leg (same time-in-force as the entry) survives
        # the close; run_daily.sh cancels unfilled entries the next morning.
        "time_in_force": "gtc",
        "limit_price": price_fmt(o["limit"], crypto),
        "client_order_id": f"desk-{v['ticker']}-{v['date']}-{o['type']}-{datetime.now():%H%M%S}",
    }
    if o["type"] == "stop_limit":
        body["stop_price"] = price_fmt(o["trigger"], crypto)
    if o["stop"]:
        body["order_class"] = "oto"
        body["stop_loss"] = {"stop_price": price_fmt(o["stop"], crypto)}
    assert body["type"] in ("limit", "stop_limit"), "market orders are never allowed"
    return body


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ticker")
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--file", help="explicit path to a <ticker>-<date>.json verdict")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    ap.add_argument("--auto", action="store_true",
                    help="submit without the YES prompt (owner-approved permanent auto-orders, "
                         "2026-09-25); only orders whose stop and target come from the desk's "
                         "structured decisions are placed")
    ap.add_argument("--replace", action="store_true",
                    help="on YES, first cancel this ticker's open desk buy orders, then submit")
    args = ap.parse_args()
    if not args.ticker and not args.file:
        ap.error("give --ticker (with optional --date) or --file")

    v = load_verdict(args)
    crypto = v.get("asset_type") == "crypto"
    try:
        client = PaperClient()
    except RuntimeError as exc:
        if not args.dry_run:
            sys.exit(f"{exc} Cannot place orders without keys.")
        client = None

    orders, notes = plan_orders(v)
    if "SELL_CHECK_POSITION" in notes:
        notes.remove("SELL_CHECK_POSITION")
        if client is None:
            notes.append("Sell: cannot check for an open position without Alpaca keys; no action.")
        else:
            held = [p for p in client.positions()
                    if p["symbol"].replace("/", "") == alpaca_symbol(v["ticker"]).replace("/", "")]
            notes.append("Sell with no position: no action." if not held else
                         "Sell with an open position: the brief defines no rule for this, "
                         "so no action (decide by hand).")
    replacing = replaceable(client, v) if (args.replace and client is not None) else []
    if replacing:
        notes.append(f"--replace: on YES, {len(replacing)} open desk order(s) for {v['ticker']} "
                     "are cancelled first and left out of the 20% cap:")
        for o in replacing:
            notes.append(f"    {o['type']} qty {o['qty']} limit {o.get('limit_price')} "
                         f"stop {o.get('stop_price')} id {o['id']}")
    orders = size_orders(orders, v, client, notes, replacing) if orders else orders

    print(f"Order plan for {v['ticker']} ({v['date']}): verdict {v['verdict']}, "
          f"conviction {v.get('conviction')}. Endpoint: {PAPER_URL} (paper)\n")
    for n in notes:
        print(f"* {n}")
    print()
    if not orders:
        print("Action: none")
        return 0
    print_table(v, orders, crypto)

    if args.dry_run:
        print("\n--dry-run: nothing submitted.")
        return 0
    if any(o["qty"] is None for o in orders):
        sys.exit("Unsized orders; nothing submitted.")

    if args.auto:
        # Automatic orders need clean provenance: the stop and target must come from the
        # desk's structured decisions, not from a model's reading of prose (2026-09-25: the
        # model-based extraction once returned the Trader's stop instead of the PM's).
        stop_src = ((v.get("levels") or {}).get("stop") or {}).get("source", "")
        tgt_src = ((v.get("levels") or {}).get("target") or {}).get("source", "")
        clean = (stop_src in ("Portfolio Manager executive summary", "Trader structured output")
                 and tgt_src == "Portfolio Manager structured output")
        prefix = f"desk-{v['ticker']}-{v['date']}"
        open_desk = [o for o in client.open_orders() if o["side"] == "buy"
                     and (o.get("client_order_id") or "").startswith(prefix)]
        if not clean:
            print(f"\nAUTO: not submitted - stop source {stop_src or 'model extraction'!r}, "
                  f"target source {tgt_src or 'model extraction'!r}; needs structured sources.")
            return 0
        if open_desk:
            print(f"\nAUTO: not submitted - {len(open_desk)} desk order(s) for {prefix} "
                  "already open (no duplicates).")
            return 0
        print("\nAUTO: submitting (owner-approved automatic orders).")
    else:
        answer = input("\nType YES to submit these PAPER orders (anything else cancels): ")
        # Case and surrounding spaces don't matter ("yes", "YES "); any other answer cancels.
        if answer.strip().upper() != "YES":
            print(f"Not submitted (received {answer!r}, which is not YES).")
            return 0
    for o in replacing:
        client.cancel(o["id"])
        print(f"Cancelled old order {o['type']} qty {o['qty']} id={o['id']}")
    failed = 0
    for o in orders:
        try:
            res = client.submit(to_alpaca(o, v))
        except RuntimeError as exc:
            failed += 1
            print(f"REJECTED {o['type']} @ {o['limit']:.2f} with stop {o['stop']}: {exc}")
            continue
        legs = res.get("legs") or []
        stop_leg = next((leg for leg in legs if leg.get("stop_price")), None)
        print(f"Submitted {res['symbol']} {res['type']} qty={res['qty']} id={res['id']} "
              f"status={res['status']} | stop-loss leg: "
              f"{stop_leg['stop_price'] if stop_leg else 'queued, shows after entry fills'}")
    if failed:
        print(f"\n{failed} order(s) rejected by Alpaca; nothing was placed for those, "
              "so no order is sitting there without a stop.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
