"""The desk's mandate: the goal, the rules, and how success and failure are judged.

Single source of truth, read by research_floor.py (shown to the Trader, Risk Team
and Portfolio Manager every run), trade_from_verdict.py (sizing), score_calls.py
and review_desk.py. Set by the user on 2026-09-23. Changing anything here is a
variable change: log it in reports/experiments.md with the date, because calls
made before and after are no longer the same experiment.
"""

from __future__ import annotations

# The desk's own book: a $5,000 starting balance inside the $100k paper account.
DESK_CAPITAL = 5000.00
MANDATE_START = "2026-09-24"          # trades entered on/after this date belong to the book

RISK_PER_TRADE = 0.02                 # at most 2% of desk equity lost if the stop is hit
TICKER_CAP = 0.20                     # default cap per ticker (medium conviction)
# The desk chooses its own position size (user rule 2026-09-23); how much of the
# book one ticker may use depends on the desk's conviction. RISK_PER_TRADE still binds.
SIZE_CAPS = {"low": 0.10, "medium": 0.20, "high": 0.50}
HORIZON = "1-2 weeks"
BENCHMARK = "SPY"
SCORE_DAYS = 5                        # calls are judged on the next 5 trading days
END_GOAL_MONTHLY = 5000.00
MIN_REWARD_RISK = 1.5                 # target must be >= 1.5x the risk (entry-stop); enforced

MANDATE_TEXT = f"""DESK MANDATE (set by the owner; applies to every decision)
- Book: ${DESK_CAPITAL:,.0f} starting capital (a sub-account of the paper account). Size and
  risk everything against this book, not the full paper balance.
- Horizon: {HORIZON} swing trades. Plans, entries, stops and targets must fit that window.
- How each call is judged: its return over the next {SCORE_DAYS} trading days versus {BENCHMARK}.
  Toward the goal = a bullish call that beats {BENCHMARK}, a bearish call that trails it, a stop
  that limited a loss as planned. Away from the goal = the opposite, a stop-out that the price
  then recovered from (stop too tight), or a loss larger than planned (stop too wide).
- Risk: at most {RISK_PER_TRADE:.0%} of the book lost on any one trade if its stop is hit. The
  execution layer sizes positions from your stop, so a wider stop means a smaller position.
- Position size is the desk's choice: the Trader states it as a percent of the book (e.g.
  "Position Sizing: 25% of book"). The ceiling depends on the desk's conviction: low up to
  {SIZE_CAPS["low"]:.0%}, medium up to {SIZE_CAPS["medium"]:.0%}, high up to {SIZE_CAPS["high"]:.0%}.
  Reserve large size for trades the evidence strongly supports; the {RISK_PER_TRADE:.0%} loss limit
  above always applies, so a large position needs a correspondingly tight stop.
- Entry discipline (adapted from Chris Creamer's order-flow framework, Robbins World Cup Micro
  Day Trading champion, for {HORIZON} swings; added 2026-09-23):
  1. Location: buy at a discount, don't chase. Prefer entries on deep pullbacks into support -
     the 0.705-0.886 retracement of the most recent swing up, or below the recent value area
     (where most recent trading happened) - over buying strength or extended moves.
  2. Stop: just beyond the level that proves the trade wrong - past the low where sellers
     already failed - not an arbitrary distance.
  3. Reward:risk: every trade needs a stated target (e.g. the prior swing high) at least
     {MIN_REWARD_RISK}x the risk from entry to stop. The execution layer rejects trades without a
     target or below {MIN_REWARD_RISK}R. The Trader must state entry, stop AND target.
- Accuracy over activity: a well-reasoned Hold beats a forced trade. Say plainly when the
  evidence is thin.
- Learn from the record: the trade journal and past reflections below show where earlier
  calls and stops went right or wrong. Name which past lesson applies to this call, if any.
- End goal: ${END_GOAL_MONTHLY:,.0f}/month. It is reached by first proving a repeatable edge
  on this small book and then scaling capital, NOT by taking bigger risks now. Never increase
  size, loosen stops or force trades to chase it."""
