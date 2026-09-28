"""Run the TradingAgents research desk on one ticker and save a labelled report.

Usage:
    python research_floor.py --ticker NVDA [--date YYYY-MM-DD] [--rounds 3]

Writes ./reports/<ticker>-<date>.md (full desk output) and
./reports/<ticker>-<date>.json (structured verdict for trade_from_verdict.py).

Only public TradingAgentsGraph APIs are used; the streaming loop mirrors the
repo's own CLI (cli/main.py): create_run_state -> get_graph_args ->
begin/end_checkpoint -> graph.stream -> record_decision.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

DESK_DIR = Path(__file__).resolve().parent
REPO_DIR = DESK_DIR / "tradingagents"
REPORTS_DIR = DESK_DIR / "reports"

load_dotenv(REPO_DIR / ".env")
load_dotenv(DESK_DIR / ".env")

from langchain_core.callbacks import BaseCallbackHandler  # noqa: E402
from pydantic import Field, create_model  # noqa: E402

from cli.utils import detect_asset_type  # noqa: E402
from tradingagents.agents.utils.rating import is_review  # noqa: E402
from tradingagents.default_config import DEFAULT_CONFIG  # noqa: E402
from tradingagents.graph.trading_graph import TradingAgentsGraph  # noqa: E402

# Chosen in Step 1 from tradingagents/llm_clients/model_catalog.py (anthropic entries).
DEEP_MODEL = "claude-fable-5-1"   # catalog: "Most capable, demanding long-horizon reasoning"
QUICK_MODEL = "claude-haiku-4-5"  # catalog: "Fastest with near-frontier intelligence"

# USD per million tokens (input, output), Anthropic first-party list prices.
PRICES = {DEEP_MODEL: (10.00, 50.00), QUICK_MODEL: (1.00, 5.00)}


# --------------------------------------------------------------------------
# Token / cost tracking
# --------------------------------------------------------------------------

class CostTracker(BaseCallbackHandler):
    """Counts tokens per model from each LLM response's usage metadata."""

    def __init__(self):
        self._lock = threading.Lock()
        self.usage: dict[str, list[int]] = {}

    def on_llm_end(self, response, **kwargs):
        try:
            message = response.generations[0][0].message
        except (IndexError, TypeError, AttributeError):
            return
        usage = getattr(message, "usage_metadata", None) or {}
        meta = getattr(message, "response_metadata", None) or {}
        model = meta.get("model_name") or meta.get("model") or "unknown"
        with self._lock:
            tin, tout = self.usage.setdefault(model, [0, 0])
            self.usage[model] = [tin + usage.get("input_tokens", 0),
                                 tout + usage.get("output_tokens", 0)]

    def cost(self) -> tuple[float, list[str]]:
        total, lines = 0.0, []
        for model, (tin, tout) in sorted(self.usage.items()):
            # Unknown model names are priced at the deep rate so the estimate errs high.
            pin, pout = next((p for m, p in PRICES.items() if model.startswith(m)),
                             PRICES[DEEP_MODEL])
            c = tin / 1e6 * pin + tout / 1e6 * pout
            total += c
            lines.append(f"{model}: {tin:,} in / {tout:,} out = ${c:.2f}")
        return total, lines


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

class Report:
    """Prints each section as it lands and keeps a copy for the .md file."""

    def __init__(self):
        self.parts: list[str] = []

    def working(self, agent: str):
        print(f"\n>>> {agent} is working...", flush=True)

    def section(self, number: int, title: str, body: str):
        heading = f"## {number}. {title}"
        text = body.strip() if body and body.strip() else "_(no output)_"
        print(f"\n{'=' * 78}\n{heading}\n{'=' * 78}\n{text}\n", flush=True)
        self.parts.append(f"{heading}\n\n{text}\n")

    def raw(self, text: str):
        print(text, flush=True)
        self.parts.append(text + "\n")

    def markdown(self) -> str:
        return "\n".join(self.parts)


def split_turns(history: str, prefix: str) -> list[str]:
    """Split a debate history string into one entry per turn.

    Each agent appends f"{prefix}: {content}" to its history (see
    agents/researchers/*.py and agents/risk_mgmt/*.py), so the prefix marks a
    turn boundary.
    """
    if not history:
        return []
    chunks = re.split(rf"(?:^|\n){re.escape(prefix)}:\s*", history)
    return [c.strip() for c in chunks if c.strip()]


def by_round(history: str, prefix: str) -> str:
    turns = split_turns(history, prefix)
    return "\n\n".join(f"### Round {i}\n\n{t}" for i, t in enumerate(turns, 1))


# --------------------------------------------------------------------------
# Summary + level extraction
# --------------------------------------------------------------------------

LEVELS = ("breakout_trigger", "pullback_zone_low", "pullback_zone_high", "stop", "entry",
          "position_size_pct", "target")
LEVEL_HELP = {
    "breakout_trigger": "price above which the desk says to buy a breakout",
    "pullback_zone_low": "lower bound of the desk's stated pullback / buy zone",
    "pullback_zone_high": "upper bound of the desk's stated pullback / buy zone",
    "stop": "the desk's stated stop-loss price",
    "entry": "the desk's stated entry price",
    "position_size_pct": ("position size the desk chose, as a percent of the book "
                          "(e.g. 25 for '25% of book')"),
    "target": ("the desk's profit target / take-profit price for the long trade (e.g. the "
               "prior swing high or the Portfolio Manager's price target)"),
}


def _level_fields():
    # Flat fields (price + quoted line per level): models fill these far more
    # reliably than nested objects, which Haiku intermittently mangled.
    fields = {}
    for name in LEVELS:
        # Required (no default) but nullable: with optional fields the model
        # simply omitted every level instead of answering null or a price.
        fields[f"{name}_price"] = (float | None, Field(
            description=f"The {LEVEL_HELP[name]}, as a plain number exactly as "
                        "written in the report. null if not stated explicitly."))
        fields[f"{name}_line"] = (str | None, Field(
            description="The report line stating that price, copied verbatim. "
                        "null if the price is null."))
    return fields



DeskSummary = create_model(
    "DeskSummary",
    conviction=(str, Field(description="low, medium, or high: how decisively the Portfolio "
                                       "Manager's reasoning supports the rating.")),
    strongest_bull_point=(str, Field(description="One sentence.")),
    strongest_bear_point=(str, Field(description="One sentence.")),
    what_would_flip=(str, Field(description="One sentence: the concrete development that "
                                            "would reverse the verdict.")),
    **_level_fields(),
)


SUMMARY_PROMPT = """You are summarising a finished trading-desk report. Do not add analysis of your own.

Final rating (already decided, do not change it): {rating}

Price levels: the PORTFOLIO MANAGER's final plan (section 11) is the desk's decision and
overrides the Trader, Research Manager and Risk Team. Take every level from the Portfolio
Manager when it states one; use earlier sections only for levels the Portfolio Manager does not
address. Never take a level from a plan the Portfolio Manager rejected.
- breakout_trigger: a price the Portfolio Manager says a move/close above would justify buying.
  If the Portfolio Manager says not to chase / not to buy strength above a price, that price is
  NOT a breakout trigger: leave breakout_trigger null.
- pullback_zone_low / pullback_zone_high: the bounds of a price range the desk names for buying
  on a dip or re-entering. A single named re-entry price is not a zone; leave the zone null.
- Fill a level whenever the report names that price explicitly, and copy the line it is on
  verbatim into the matching _line field.
- Never compute, round, average or infer a price. If the report does not name one, use null.

REPORT:
{report}
"""


def trader_levels(report_md: str) -> dict:
    """Entry and stop straight from the Trader's structured lines (no model involved).

    render_trader_proposal() in the repo writes "**Entry Price**: X" and
    "**Stop Loss**: X" (or "not provided") in the Trader section.
    """
    section = report_md.split("## 9. Trader", 1)[-1].split("\n## 10.", 1)[0]
    found = {}
    for name, label in (("entry", "Entry Price"), ("stop", "Stop Loss")):
        m = re.search(rf"^\*\*{label}\*\*:\s*\$?([\d,]+(?:\.\d+)?)\s*$", section, re.M)
        if m:
            found[name] = {"value": float(m.group(1).replace(",", "")),
                           "report_line": m.group(0).strip(), "source": "Trader structured output"}
    # Portfolio Manager's structured output (render_pm_decision) has "**Price Target**: X".
    pm = report_md.split("## 11. Portfolio Manager", 1)
    if len(pm) == 2:
        m = re.search(r"^\*\*Price Target\*\*:\s*\$?([\d,]+(?:\.\d+)?)\s*$", pm[1], re.M)
        if m:
            found["target"] = {"value": float(m.group(1).replace(",", "")),
                               "report_line": m.group(0).strip(),
                               "source": "Portfolio Manager structured output"}
        # The PM's own stop, read deterministically from its Executive Summary: the LAST
        # "stop $X" there is its plan (it usually quotes the Trader's rejected stop first).
        # On 2026-09-25 the model-based extraction returned the Trader's 209.50 while the PM
        # said "stop $209.00".
        m = re.search(r"^\*\*Executive Summary\*\*:.*$", pm[1], re.M)
        if m:
            stops = re.findall(r"\bstop\s+\$([\d,]+(?:\.\d+)?)", m.group(0), re.I)
            if stops:
                found["stop"] = {"value": float(stops[-1].replace(",", "")),
                                 "report_line": m.group(0).strip(),
                                 "source": "Portfolio Manager executive summary"}
    return found


_NUM_RE = re.compile(r"(?<![\d.])\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


def _numbers(line: str) -> set[float]:
    return {float(n.replace(",", "")) for n in _NUM_RE.findall(line)}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("*", "")).strip()


def verify_level(value: float | None, quoted: str | None, report_lines: list[str]) -> dict:
    """Keep a price only if that exact number is written in the report.

    Prefer the line the summariser quoted; if its quote is not verbatim, cite
    the first report line that contains the number instead. A price that
    appears nowhere in the report is rejected, so nothing invented gets through.
    """
    if value is None:
        return {"value": None, "report_line": None}
    q = _norm(quoted or "")
    candidates = [l for l in report_lines if value in _numbers(l)]
    for line in candidates:
        if q and q in _norm(line):
            return {"value": value, "report_line": line.strip()}
    if candidates:
        return {"value": value, "report_line": candidates[0].strip(),
                "note": "summariser quote was not verbatim; cited the report line containing this price"}
    return {"value": None, "report_line": None,
            "rejected": f"{value} does not appear anywhere in the report"}


def summarise(llm, rating: str, report_md: str) -> tuple[dict, list[str]]:
    """Summary lines + verified levels. Retries once; never raises."""
    errors = []
    for _ in range(2):
        try:
            s = llm.with_structured_output(DeskSummary).invoke(
                SUMMARY_PROMPT.format(rating=rating, report=report_md))
            if s is None:
                raise ValueError("no structured result")
            # Only the desk's decision sections (8. Research Manager onward) count
            # as the desk producing a level; analyst data tables and quoted social
            # posts contain prices too, but nobody on the desk chose them.
            decision_part = report_md.split("## 8. Research Manager", 1)
            lines = decision_part[1].splitlines() if len(decision_part) == 2 else []
            levels = {n: verify_level(getattr(s, f"{n}_price"), getattr(s, f"{n}_line"), lines)
                      for n in LEVELS}
            # Precedence (2026-09-24 fix): the Trader's structured entry/stop are only a
            # fallback. A level the summariser verified on a Portfolio Manager line wins,
            # because the PM's final plan supersedes the Trader (on NVDA 2026-09-24 the PM
            # replaced the Trader's 215.19 stop with 209.50; using 215.19 killed a valid order).
            pm_lines = set(report_md.split("## 11. Portfolio Manager", 1)[-1].splitlines())
            for name, lv in trader_levels(report_md).items():
                mine = levels.get(name) or {}
                from_pm = mine.get("value") is not None and mine.get("report_line") in {
                    ln.strip() for ln in pm_lines}
                if name == "target" or lv.get("source", "").startswith("Portfolio Manager") or not from_pm:
                    levels[name] = lv
            return {"conviction": s.conviction.strip().lower(),
                    "strongest_bull_point": s.strongest_bull_point,
                    "strongest_bear_point": s.strongest_bear_point,
                    "what_would_flip": s.what_would_flip,
                    "levels": levels}, errors
        except Exception as exc:  # keep the paid-for report even if the summary fails
            errors.append(f"{type(exc).__name__}: {str(exc)[:200]}")
    unavailable = "(summary unavailable - see report)"
    return {"conviction": "unknown", "strongest_bull_point": unavailable,
            "strongest_bear_point": unavailable, "what_would_flip": unavailable,
            "levels": {**{n: {"value": None, "report_line": None} for n in LEVELS},
                       **trader_levels(report_md)}}, errors


# --------------------------------------------------------------------------
# Portfolio + stop-loss track record for the decision agents
# --------------------------------------------------------------------------

from tradingagents.portfolio import PortfolioContext, Position  # noqa: E402


class DeskPortfolio(PortfolioContext):
    """The repo's PortfolioContext plus the desk's open stops and stop history.

    PortfolioContext.render() is what the Trader, the three risk analysts and
    the Portfolio Manager read (via portfolio_context in the state). Extending
    it is how the desk learns from its own stop-loss outcomes: the record is in
    front of the agents every time they choose a stop.
    """

    open_stops: list[str] = []
    journal: list[str] = []
    book_line: str = ""

    def render(self, ticker: str) -> str:
        from mandate import MANDATE_TEXT

        parts = [MANDATE_TEXT, super().render(ticker)]
        if self.book_line:
            parts.append(self.book_line)
        if self.open_stops:
            parts.append("Open protective stops:\n" + "\n".join(f"- {s}" for s in self.open_stops))
        parts.append("Desk trade journal (paper account) - how past entries and their stop-losses "
                     "worked out:\n" + "\n".join(f"- {j}" for j in self.journal))
        parts.append("Stop-loss policy: the desk chooses its own stop. State it as an absolute "
                     "price, reason from this track record and the instrument's volatility (ATR), "
                     "and weigh stops that fired before a recovery (too tight) against large "
                     "stopped-out losses (too wide). The execution layer enforces a "
                     "0.5x-3x ATR(14) safety range and sizes the position so that hitting the "
                     "stop loses at most 2% of the book.")
        return "\n\n".join(parts)


def load_desk_portfolio() -> DeskPortfolio | None:
    """Cash, positions, open stops and the stop journal from the paper account; None without keys."""
    try:
        from alpaca_paper import PaperClient
        import trade_journal

        client = PaperClient()
        positions = [Position(ticker=p["symbol"].replace("/", "-"), quantity=float(p["qty"]),
                              average_price=float(p["avg_entry_price"]))
                     for p in client.positions()]
        stops = []
        for o in client.open_orders():
            for leg in [o] + (o.get("legs") or []):
                if leg["side"] == "sell" and leg.get("stop_price") and leg["status"] in (
                        "new", "accepted", "held"):
                    state = ("waits for its buy order to fill" if leg["status"] == "held"
                             else f"active, {leg['time_in_force'].upper()}")
                    stops.append(f"{leg['symbol']} sell {leg['qty']} at stop {leg['stop_price']} "
                                 f"({state})")
        trades = trade_journal.build(client)
        b = trade_journal.book(client, trades)
        legacy = ", ".join(f"{t['symbol']} {t['qty']:g} sh" for t in b["legacy"]) or "none"
        book_line = (f"Desk book: equity ${b['equity']:,.2f} (started ${b['capital']:,.0f}; "
                     f"realized {b['realized']:+,.2f}, unrealized {b['unrealized']:+,.2f}), "
                     f"free cash ${b['cash']:,.2f}. Legacy positions from before the mandate, "
                     f"held in the account but outside the book: {legacy}.")
        # "Cash available" in the repo's render is the book's cash, not the $100k paper balance.
        return DeskPortfolio(cash=b["cash"], currency="USD", positions=positions,
                             open_stops=stops, book_line=book_line,
                             journal=trade_journal.summary_lines(trades))
    except Exception as exc:  # no keys / API down: run without portfolio context
        print(f"(portfolio context unavailable: {type(exc).__name__}: {exc})")
        return None


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()

    ticker = args.ticker.strip().upper()
    trade_date = args.date
    asset_type = detect_asset_type(ticker).value

    # The repo's CLI drops the fundamentals analyst for crypto
    # (cli/utils.py:filter_analysts_for_asset_type); mirror it.
    analysts = ["market", "social", "news", "fundamentals"]
    if asset_type == "crypto":
        analysts.remove("fundamentals")

    config = DEFAULT_CONFIG.copy()
    config.update({
        "llm_provider": "anthropic",
        "deep_think_llm": DEEP_MODEL,
        "quick_think_llm": QUICK_MODEL,
        "backend_url": None,
        "max_debate_rounds": args.rounds,
        "max_risk_discuss_rounds": 2,
        # SDK retry budget per LLM call (repo default is the SDK's 2). A dropped
        # connection mid-run otherwise aborts the whole desk run.
        "llm_max_retries": 6,
    })

    tracker = CostTracker()
    started = time.time()
    graph = TradingAgentsGraph(selected_analysts=analysts, debug=True,
                               config=config, callbacks=[tracker])
    # Stream every model call. This network drops any HTTP connection that is
    # silent for ~60s (verified: a non-streaming call died at 60s, the same call
    # streamed finished in 67s), and a full agent answer often takes longer.
    # Every agent holds these same two objects, so this covers the whole graph.
    graph.deep_thinking_llm.streaming = True
    graph.quick_thinking_llm.streaming = True
    # Research Manager and Portfolio Manager run on the deep model with
    # structured output. langchain_anthropic forces tool_choice for that unless
    # thinking is set, and Fable 5.1 rejects forced tool_choice with a 400, so
    # both agents silently fell back to free text. With adaptive thinking set,
    # langchain uses tool_choice=auto and the structured call works (verified).
    # The agents bind their structured wrappers when the graph is built, so
    # rebuild it the same way TradingAgentsGraph.__init__ does.
    graph.deep_thinking_llm.thinking = {"type": "adaptive"}
    graph.workflow = graph.graph_setup.setup_graph(analysts)
    graph.graph = graph.workflow.compile()
    rep = Report()
    rep.raw(f"# Research desk: {ticker} on {trade_date}\n\n"
            f"Asset type: {asset_type} | Analysts: {', '.join(analysts)} | "
            f"Debate rounds: {args.rounds} | Risk rounds: 2 | "
            f"Deep: {DEEP_MODEL} | Quick: {QUICK_MODEL}\n")

    # 1. Memory. create_run_state settles this ticker's pending decisions
    # (realized return + reflection, via _resolve_pending_entries) and injects
    # past lessons into the state the Portfolio Manager reads.
    rep.working("Memory (settling past calls, loading reflections)")
    portfolio = load_desk_portfolio()
    init_state = graph.create_run_state(ticker, trade_date, asset_type, portfolio)
    mine = [e for e in graph.memory_log.load_entries() if e["ticker"] == ticker]
    memory_lines = [f"Decision log: {config['memory_log_path']}"]
    if not mine:
        memory_lines.append(f"No previous decisions logged for {ticker}.")
    for e in mine[-5:]:
        if e["pending"]:
            memory_lines.append(f"- {e['date']} {e['rating']}: pending (needs "
                                f"{config['holding_period_days']} trading days of prices to settle)")
        else:
            memory_lines.append(f"- {e['date']} {e['rating']}: raw {e['raw']}, alpha {e['alpha']}, "
                                f"held {e['holding']}\n\n  Reflection: {e['reflection'] or '(none)'}")
    memory_lines.append("\nContext passed to the Portfolio Manager:\n\n"
                        + (init_state.get("past_context") or "(none)"))
    memory_lines.append("\nPortfolio and stop-loss track record passed to the Trader, "
                        "Risk Team and Portfolio Manager:\n\n"
                        + (init_state.get("portfolio_context") or "(not available)"))
    rep.section(1, "Memory", "\n".join(memory_lines))

    # (state key, heading, section number, analyst id) in graph execution order.
    stages = [
        ("market_report", "Market Analyst", 2, "market"),
        ("sentiment_report", "Sentiment Analyst", 3, "social"),
        ("news_report", "News Analyst", 4, "news"),
        ("fundamentals_report", "Fundamentals Analyst", 5, "fundamentals"),
    ]
    running = [s for s in stages if s[3] in analysts]
    printed: set[str] = set()
    rep.working(running[0][1])

    args_graph = graph.propagator.get_graph_args()
    tid = graph.begin_checkpoint(ticker, trade_date, asset_type, portfolio)
    if tid is not None:
        args_graph.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = tid

    final_state: dict = {}
    seen_bull = seen_bear = 0
    seen_risk = 0
    try:
        for chunk in graph.graph.stream(graph.checkpoint_input(init_state), **args_graph):
            final_state.update(chunk)

            # Analysts: print each report as soon as it is filled in.
            for i, (key, name, number, _) in enumerate(running):
                if key not in printed and chunk.get(key):
                    printed.add(key)
                    rep.section(number, name, chunk[key])
                    if i + 1 < len(running):
                        rep.working(running[i + 1][1])
                    else:
                        if "fundamentals" not in analysts:
                            rep.section(5, "Fundamentals Analyst",
                                        "Skipped: the repo does not run the fundamentals "
                                        "analyst for crypto (cli/utils.py).")
                        rep.working("Bull Researcher (round 1)")

            debate = chunk.get("investment_debate_state") or {}
            nb = len(split_turns(debate.get("bull_history", ""), "Bull Analyst"))
            nr = len(split_turns(debate.get("bear_history", ""), "Bear Analyst"))
            if nb > seen_bull or nr > seen_bear:
                seen_bull, seen_bear = nb, nr
                if not debate.get("judge_decision"):
                    nxt = (f"Bear Researcher (round {nr + 1})" if nb > nr
                           else f"Bull Researcher (round {nb + 1})")
                    if nb + nr < 2 * args.rounds:
                        rep.working(nxt)
                    else:
                        rep.working("Research Manager")

            if debate.get("judge_decision") and "bull" not in printed:
                printed.update({"bull", "bear", "rm"})
                rep.section(6, "Bull Case", by_round(debate.get("bull_history", ""), "Bull Analyst"))
                rep.section(7, "Bear Case", by_round(debate.get("bear_history", ""), "Bear Analyst"))
                rep.section(8, "Research Manager", chunk.get("investment_plan") or debate["judge_decision"])
                rep.working("Trader")

            if chunk.get("trader_investment_plan") and "trader" not in printed:
                printed.add("trader")
                rep.section(9, "Trader", chunk["trader_investment_plan"])
                rep.working("Aggressive Analyst (round 1)")

            risk = chunk.get("risk_debate_state") or {}
            if risk.get("count", 0) > seen_risk and not risk.get("judge_decision"):
                seen_risk = risk["count"]
                order = ["Aggressive Analyst", "Conservative Analyst", "Neutral Analyst"]
                if seen_risk < 3 * 2:
                    rep.working(f"{order[seen_risk % 3]} (round {seen_risk // 3 + 1})")
                else:
                    rep.working("Portfolio Manager")

            if risk.get("judge_decision") and "risk" not in printed:
                printed.add("risk")
                body = []
                for persona in ("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst"):
                    key = persona.split()[0].lower() + "_history"
                    body.append(f"### {persona}\n\n" + (by_round(risk.get(key, ""), persona)
                                                        .replace("### Round", "#### Round")))
                rep.section(10, "Risk Team", "\n\n".join(body))
                rep.section(11, "Portfolio Manager", chunk.get("final_trade_decision")
                            or risk["judge_decision"])

        graph.record_decision(ticker, trade_date, final_state)
        graph.clear_checkpoint_on_success(ticker, trade_date, asset_type, portfolio)
    finally:
        graph.end_checkpoint()

    decision_text = final_state.get("final_trade_decision", "")
    signal = graph.process_signal(decision_text)

    # Summary lines + desk-stated price levels.
    rep.working("Desk summary (extracting verdict lines and price levels)")
    report_md = rep.markdown()
    summary, summary_errors = summarise(graph.quick_thinking_llm, signal, report_md)
    for err in summary_errors:
        rep.raw(f"Summary attempt failed: {err}")
    levels = summary["levels"]

    verdict = "REVIEW (no readable rating)" if is_review(signal) else signal
    rep.raw("\n## Summary\n")
    rep.raw(f"- **Verdict:** {verdict}")
    rep.raw(f"- **Conviction:** {summary['conviction']}")
    rep.raw(f"- **Strongest bull point:** {summary['strongest_bull_point']}")
    rep.raw(f"- **Strongest bear point:** {summary['strongest_bear_point']}")
    rep.raw(f"- **What would flip the verdict:** {summary['what_would_flip']}")

    elapsed = time.time() - started
    cost, cost_lines = tracker.cost()
    rep.raw(f"\nRun time: {elapsed / 60:.1f} min | Approx. cost: ${cost:.2f}")
    for cl in cost_lines:
        rep.raw(f"  {cl}")

    REPORTS_DIR.mkdir(exist_ok=True)
    stem = f"{ticker}-{trade_date}"
    (REPORTS_DIR / f"{stem}.md").write_text(rep.markdown(), encoding="utf-8")
    payload = {
        "ticker": ticker,
        "date": trade_date,
        "asset_type": asset_type,
        "verdict": signal,
        "conviction": summary["conviction"],
        "strongest_bull_point": summary["strongest_bull_point"],
        "strongest_bear_point": summary["strongest_bear_point"],
        "what_would_flip": summary["what_would_flip"],
        "levels": levels,
        "portfolio_manager_decision": decision_text,
        "report_path": str(REPORTS_DIR / f"{stem}.md"),
        "run_seconds": round(elapsed, 1),
        "approx_cost_usd": round(cost, 2),
        "models": {"deep": DEEP_MODEL, "quick": QUICK_MODEL},
    }
    (REPORTS_DIR / f"{stem}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nSaved {REPORTS_DIR / (stem + '.md')}\nSaved {REPORTS_DIR / (stem + '.json')}")
    return 0 if not is_review(signal) else 2


if __name__ == "__main__":
    sys.exit(main())
