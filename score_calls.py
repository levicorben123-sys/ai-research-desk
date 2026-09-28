"""Score every research-desk call against what the price did next.

Usage:
    python score_calls.py [--reports-dir reports] [--days 5]

Reads every <ticker>-<date>.json in reports/, settles each call over the same
window TradingAgents uses for its own reflections (holding_period_days = 5 bars
after the call date, benchmark SPY for US / crypto tickers), writes
reports/scorecard.csv and prints the hit rate next to the baselines it has to beat.

A call counts as right when it beat SPY in its direction (desk mandate 2026-09-23):
  Buy / Overweight   -> right if it outperformed SPY over the 5 days (alpha > 0)
  Sell / Underweight -> right if it underperformed SPY (alpha < 0)
  Hold               -> not scored for direction (reported separately)

Caveat: crypto trades every day, so its 5 bars are 5 calendar days while SPY's
are 5 trading days (the repo's own reflection has the same quirk).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from datetime import datetime, timedelta
from pathlib import Path

import yfinance as yf

DESK_DIR = Path(__file__).resolve().parent
DIRECTION = {"Buy": 1, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -1}
MIN_CALLS = 50
_price_cache: dict[tuple[str, str], object] = {}


def forward_return(ticker: str, day: str, bars: int) -> tuple[float | None, str | None]:
    """Close-to-close return from ``day`` to ``bars`` bars later; None until that bar exists."""
    key = (ticker, day)
    if key not in _price_cache:
        start = datetime.strptime(day, "%Y-%m-%d")
        end = start + timedelta(days=round(bars * 7 / 5) + 10)
        _price_cache[key] = (yf.Ticker(ticker).history(start=day, end=end.strftime("%Y-%m-%d"))
                             .dropna(subset=["Close"]))  # Yahoo can leave the latest close blank
    hist = _price_cache[key]
    if len(hist) <= bars:
        return None, None
    c = hist["Close"]
    return float((c.iloc[bars] - c.iloc[0]) / c.iloc[0]), hist.index[bars].strftime("%Y-%m-%d")


def wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = hits / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--reports-dir", default=str(DESK_DIR / "reports"))
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--count", action="store_true",
                    help="print only the number of settled directional calls")
    args = ap.parse_args()
    reports = Path(args.reports_dir)

    rows = []
    for path in sorted(reports.glob("*.json")):
        if not re.match(r".+-\d{4}-\d{2}-\d{2}\.json$", path.name):
            continue
        v = json.loads(path.read_text(encoding="utf-8"))
        verdict = v.get("verdict")
        row = {"date": v["date"], "ticker": v["ticker"], "verdict": verdict,
               "conviction": v.get("conviction", ""), "settled_on": "", "return": "",
               "spy_return": "", "alpha": "", "correct": ""}
        raw, settled = forward_return(v["ticker"], v["date"], args.days)
        spy, _ = forward_return("SPY", v["date"], args.days)
        if raw is not None and spy is not None:
            row.update(settled_on=settled, **{"return": round(raw, 5)},
                       spy_return=round(spy, 5), alpha=round(raw - spy, 5))
            d = DIRECTION.get(verdict)
            if d:  # Hold (0) and REVIEW (None) are not directional calls
                row["correct"] = int(d * (raw - spy) > 0)
        rows.append(row)

    out = reports / "scorecard.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["date"])
        w.writeheader()
        w.writerows(rows)

    settled = [r for r in rows if r["settled_on"]]
    directional = [r for r in settled if r["correct"] != ""]
    hits = sum(r["correct"] for r in directional)
    n = len(directional)
    if args.count:
        print(n)
        return
    ups = sum(1 for r in settled if r["alpha"] > 0)

    print(f"Calls: {len(rows)} total, {len(settled)} settled, "
          f"{len(rows) - len(settled)} pending (need {args.days} bars after the call date)")
    if n:
        lo, hi = wilson(hits, n)
        print(f"Directional hit rate: {hits}/{n} = {hits / n:.0%}  (95% range {lo:.0%}-{hi:.0%})")
    else:
        print("Directional hit rate: no settled Buy/Overweight/Sell/Underweight calls yet")
    if settled:
        print(f"Baseline 'always bullish': {ups}/{len(settled)} = {ups / len(settled):.0%} "
              f"(share of settled calls that beat SPY)")
        print("Baseline coin flip: 50%")
        by = {}
        for r in settled:
            by.setdefault(r["verdict"], []).append(r["alpha"])
        for verdict in ("Buy", "Overweight", "Hold", "Underweight", "Sell"):
            if verdict in by:
                a = by[verdict]
                print(f"  {verdict:<11} n={len(a):<3} mean {args.days}-day alpha vs SPY "
                      f"{sum(a) / len(a):+.2%}")
        all_alpha = [r["alpha"] for r in settled]
        print(f"  {'All calls':<11} n={len(all_alpha):<3} mean alpha {sum(all_alpha) / len(all_alpha):+.2%}"
              "  (what buying every ticker regardless of verdict would have earned)")
    if n < MIN_CALLS:
        print(f"\nNOT ENOUGH DATA: {n} directional calls settled; with fewer than "
              f"{MIN_CALLS} any hit rate is noise. The desk has not shown an edge yet.")
    elif lo <= max(0.5, ups / len(settled)):
        print("\nNo demonstrated edge: the 95% range still includes the better baseline.")
    else:
        print("\nHit rate beats both baselines at 95% - worth a closer look (check it holds "
              "across tickers and months before trusting it).")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
