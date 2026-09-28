"""Weekly desk review: what happened, why, and the ONE thing to try next.

Reads the scorecard, the trade journal and desk book, the daily log, recent
verdicts and the experiment history, then asks the deep model to:
  1. judge the week's results against the mandate (toward / away from the goal),
  2. form a hypothesis on WHY the results were what they were,
  3. form a second hypothesis on what to do next,
  4. propose at most ONE variable change with a test plan - or "no change" when
     there is not enough data to tell signal from noise.

The proposal is appended to reports/experiments.md as PROPOSED. Nothing is
changed automatically: the owner approves (user rule 2026-09-23), then the change
is applied and the experiment's start date recorded, so before/after calls can be
compared one variable at a time.

Usage:
    python review_desk.py            # run the review (~$0.30-1.00 of API usage)
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

DESK_DIR = Path(__file__).resolve().parent
REPORTS = DESK_DIR / "reports"
EXPERIMENTS = REPORTS / "experiments.md"
load_dotenv(DESK_DIR / ".env")

import anthropic  # noqa: E402

import mandate  # noqa: E402

MODEL = "claude-fable-5-1"
MIN_CALLS_PER_TEST = 20   # settled calls needed since the last change before judging it

# What the review may propose changing. Owner-level items are the mandate itself.
VARIABLES = """Adjustable by an approved experiment (one at a time):
- Debate depth: bull/bear rounds (now 2), risk-team rounds (now 2)
- Stop safety range: now 0.5x-3x ATR(14)
- Size ceilings by conviction: now low 10% / medium 20% / high 50% of book
- Entry rules: Overweight uses the Hold rule (breakout stop-limit and/or pullback limit, each leg
  stands alone since 2026-09-24); every buy needs a desk stop and a target >= 1.5R;
  pullback limit placed 25% up the zone; breakout limit +0.4% above trigger
- Which verdicts trade (e.g. only high conviction, or Buy/Overweight only)
- Ticker list (now NVDA, JPM; XOM removed by owner 2026-09-24)
- Deep model (now claude-fable-5-1) / quick model (now claude-haiku-4-5)
Owner-level (mandate - propose only with strong evidence, owner decides):
- Horizon (1-2 weeks), benchmark (SPY, 5 trading days), 2% max risk per trade, book size."""

SYSTEM = """You are the performance reviewer for a paper-trading research desk made of LLM agents.
Your job is disciplined experimentation, not optimism. Rules:
- Separate signal from noise. With few settled calls, say so and prefer "no change - keep
  collecting data". Never propose a change on fewer than {min_calls} settled calls since the
  last change unless something is clearly broken (a bug, a rule violation, a repeated failure
  mode with an obvious mechanism).
- Change ONE variable at a time. Never bundle changes.
- Every hypothesis must cite specific evidence from the data provided (tickers, dates,
  numbers). If the data cannot support a hypothesis, say what data would.
- A change needs a test plan: the metric, how many settled calls to judge it on, and what
  result would count as better, worse, or inconclusive.
- The long-term goal ($5,000/month) is reached by proving an edge and then scaling capital.
  Never propose taking more risk to chase it."""

FORMAT = """Write the review in exactly this markdown structure:

### Results vs the goal
(settled calls, hit rate vs SPY vs baselines, book P&L, stops hit; toward or away from the goal)

### Hypothesis 1 - why the results were what they were
(specific, evidence-backed; say how confident you are)

### Hypothesis 2 - what to do next
(what you expect to improve results, and why)

### Proposed change
- Variable: <one variable from the list, or "none - keep collecting data">
- From -> To: <current value> -> <new value>
- Test plan: <metric, number of settled calls, what counts as better / worse / inconclusive>
- Why this one first: <one or two sentences>

### Data quality notes
(anything missing, inconsistent or suspicious in the inputs)"""


def _read(path: Path, limit: int = 12000) -> str:
    if not path.exists():
        return "(none yet)"
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-limit:]


def _recent_verdicts(n: int = 30) -> str:
    rows = []
    for p in sorted(REPORTS.glob("*-20??-??-??.json"))[-n:]:
        v = json.loads(p.read_text(encoding="utf-8"))
        lv = v.get("levels") or {}
        val = lambda k: (lv.get(k) or {}).get("value")  # noqa: E731
        # Entry, zone and target too (review 2026-09-25: with only the stop, the 1.5R and
        # location rules were unauditable).
        rows.append(f"{v['date']} {v['ticker']} {v['verdict']} conviction={v.get('conviction')} "
                    f"entry={val('entry')} zone={val('pullback_zone_low')}-{val('pullback_zone_high')} "
                    f"breakout={val('breakout_trigger')} stop={val('stop')} "
                    f"(from {(lv.get('stop') or {}).get('source', 'model extraction')}) "
                    f"target={val('target')} size%={val('position_size_pct')} "
                    f"cost=${v.get('approx_cost_usd')}")
    return "\n".join(rows) or "(none yet)"


def _journal() -> str:
    try:
        from alpaca_paper import PaperClient
        import trade_journal
        client = PaperClient()
        trades = trade_journal.build(client)
        b = trade_journal.book(client, trades)
        return ("\n".join(trade_journal.summary_lines(trades, limit=100))
                + f"\nDesk book: equity ${b['equity']:,.2f}, realized {b['realized']:+.2f}, "
                  f"unrealized {b['unrealized']:+.2f}, cash ${b['cash']:,.2f}")
    except Exception as exc:
        return f"(journal unavailable: {exc})"


def _scorecard_summary() -> str:
    import subprocess
    out = subprocess.run([sys.executable, str(DESK_DIR / "score_calls.py")],
                         capture_output=True, text=True, cwd=DESK_DIR)
    return out.stdout.strip() or out.stderr.strip()


def main() -> int:
    today = date.today().isoformat()
    context = f"""Review date: {today}

{mandate.MANDATE_TEXT}

{VARIABLES}

## Scorecard (calls judged vs SPY over 5 trading days)
{_scorecard_summary()}

## Per-call scorecard rows
{_read(REPORTS / "scorecard.csv", 8000)}

## Trade journal and desk book
{_journal()}

## Recent verdicts
{_recent_verdicts()}

## Daily log
{_read(REPORTS / "daily_log.csv", 4000)}

## Experiment history (earlier reviews, approvals and results)
{_read(EXPERIMENTS, 12000)}
"""
    client = anthropic.Anthropic(max_retries=4)
    # Streamed: this network drops HTTP connections silent for ~60s.
    with client.messages.stream(
        model=MODEL, max_tokens=16000,
        system=SYSTEM.format(min_calls=MIN_CALLS_PER_TEST),
        output_config={"effort": "high"},
        messages=[{"role": "user", "content": context + "\n\n" + FORMAT}],
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        print("Review declined by the model (refusal); nothing written.")
        return 1
    review = "".join(b.text for b in msg.content if b.type == "text").strip()
    cost = msg.usage.input_tokens / 1e6 * 10 + msg.usage.output_tokens / 1e6 * 50

    header = "" if EXPERIMENTS.exists() else (
        "# Desk experiments log\n\nOne variable at a time. Each weekly review proposes at most one "
        "change; the owner approves before it is applied. Record the approval date and the change "
        "here, because calls before and after a change are different experiments.\n")
    entry = (f"\n\n---\n\n## Weekly review {today} - status: PROPOSED (awaiting owner approval)\n\n"
             f"{review}\n\n_Model {MODEL}, ~${cost:.2f}._\n")
    with EXPERIMENTS.open("a", encoding="utf-8") as f:
        f.write(header + entry)
    print(review)
    print(f"\nAppended to {EXPERIMENTS} (~${cost:.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
