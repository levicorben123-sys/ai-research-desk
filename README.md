# AI Research Desk — multi-agent LLM stock research on a paper account

A daily, fully automated stock-research "desk" built on the open-source
[TradingAgents](https://github.com/TauricResearch/TradingAgents) multi-agent framework, running on
Anthropic's Claude models and an **Alpaca paper-trading account (no real money)**.

Built with [Claude Code](https://claude.com/claude-code) as the coding agent; I designed the rules,
made the decisions and ran/tested the system.

**Status (Oct 2026): paused since Oct 2** to focus on other projects. The paper account ended at $100,015 (from $100,000) with no open positions; too short a run to judge the approach.

## What it does
Every weekday morning a scheduled job runs a team of AI agents per ticker:

1. **Analysts** — market/technical, news, social sentiment and fundamentals reports
2. **Bull vs. bear researchers** debate the case; a research manager rules on it
3. **Trader** proposes a plan (entry, stop, target)
4. **Risk team** (aggressive / conservative / neutral) debates it; a **portfolio manager** makes the final call

The verdict is then turned into a paper order only if it passes hard-coded rules.

## Components
| File | Purpose |
|---|---|
| `research_floor.py` | Runs the agent pipeline, streams progress, writes an 11-section report per ticker |
| `trade_from_verdict.py` | Converts the verdict into a bracket order; enforces risk rules (2% risk per trade, ATR-based stop bounds, no duplicate orders) |
| `mandate.py` | The $5,000 paper-book mandate: position sizing by conviction, benchmark vs. SPY |
| `protect_positions.py` | Makes sure every open position has a protective stop that survives overnight |
| `score_calls.py` | Scorecard: grades each call once it settles, against simple baselines (50-call minimum before drawing conclusions) |
| `trade_journal.py` | Feeds stop-loss outcomes back into the agents' context so they can adjust |
| `review_desk.py` | Weekly review that proposes **one** change for human approval |
| `healthcheck.py` | Self-healing health check every 15 min: re-enables tasks, starts a missed run, retries failed tickers, raises alerts |
| `alpaca_paper.py` | Small Alpaca paper-API client (paper URL hard-coded) |
| `run_daily.sh`, `run_hidden.vbs` | Scheduled daily run (hidden window), with a STOP-file kill switch |

## Engineering notes
- **Network fix:** long model calls were being dropped after ~60 s of silence on this network. Switching
  the LLM clients to streaming responses fixed it.
- **Structured-output fallback:** handled a model/API mismatch where forced tool-choice was rejected,
  by enabling adaptive thinking and rebuilding the graph.
- **Human in the loop:** strategy changes are proposed by the weekly review but only applied after approval.

## Status
Running daily on paper since September 2026. Too few settled calls yet to judge whether it adds value —
the scorecard is designed to answer that honestly once 50 calls have settled.

## Setup
Requires Python 3.12, the TradingAgents package, and an `.env` with `ANTHROPIC_API_KEY`,
`ALPACA_API_KEY`/`ALPACA_SECRET_KEY` (paper), and `FRED_API_KEY`. Never commit `.env`.
