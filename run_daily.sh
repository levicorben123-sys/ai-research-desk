#!/usr/bin/env bash
# Daily research loop for the PAPER trading desk. Orders: dry-run only, unless the file
# AUTO_ORDERS exists (owner-approved automatic orders, 2026-09-25) - then plans that pass
# every rule AND have structured stop/target sources are placed automatically.
# Cadence: daily research. Orders are GTC so attached stop-losses survive the close;
# the cleanup step below cancels yesterday's unfilled BUY entries each morning.
#
#   ./run_daily.sh                      (default tickers: NVDA JPM)
#   TICKERS="NVDA AAPL" ./run_daily.sh  (override for one run)
#
# Kill switch: create ~/trading-desk/STOP and this script exits immediately.
set -u

DESK="$HOME/trading-desk"
[ -e "$DESK/STOP" ] && { echo "STOP file present at $DESK/STOP - exiting."; exit 0; }

cd "$DESK" || exit 1
TICKERS="${TICKERS:-NVDA JPM}"  # XOM removed by owner 2026-09-24
TODAY="$(date +%F)"
LOG_CSV="$DESK/reports/daily_log.csv"
RUN_LOG="$DESK/reports/run_daily-$TODAY.log"
mkdir -p "$DESK/reports"

# The venv lives inside the repo; Scripts/ on Windows, bin/ elsewhere.
PY="$DESK/tradingagents/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$DESK/tradingagents/.venv/bin/python"

# Pick up only TELEGRAM_* from the desk's .env. Exporting the whole file would
# freeze every key at loop start (blank ones included), and the Python scripts'
# load_dotenv() does not override variables that already exist - that is how a
# FRED key added mid-loop was ignored on 2026-09-23. The scripts read .env themselves.
if [ -f "$DESK/.env" ]; then
  eval "$(grep -E '^TELEGRAM_(BOT_TOKEN|CHAT_ID)=' "$DESK/.env" | sed 's/^/export /')"
fi

log() { echo "[$(date +%T)] $*" | tee -a "$RUN_LOG"; }

export PYTHONIOENCODING=utf-8 PYTHONUNBUFFERED=1

# Scheduled runs: skip weekends/market holidays (Alpaca calendar), so a closed
# day costs nothing.
if ! "$PY" alpaca_paper.py is-trading-day >/dev/null 2>&1; then
  echo "[$(date +%T)] Market closed today - nothing to do." >> "$DESK/reports/scheduler.log"
  exit 0
fi

# Run until the scorecard has TARGET_CALLS settled directional calls, then
# switch itself off by creating the STOP file (user request 2026-09-23).
TARGET_CALLS=50
target_reached() {
  local n
  n="$("$PY" score_calls.py --count 2>/dev/null | tail -1)"
  [ -n "$n" ] && [ "$n" -ge "$TARGET_CALLS" ] 2>/dev/null
}
if target_reached; then
  log "Scorecard has $TARGET_CALLS+ settled calls - target reached; creating STOP and exiting."
  touch "$DESK/STOP"
  exit 0
fi

# Run a command; on failure retry once, then log and carry on.
twice() {
  "$@" && return 0
  log "retrying: $*"
  "$@" && return 0
  log "FAILED twice, continuing: $*"
  return 1
}

telegram() {
  [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ] || return 0
  curl -s -o /dev/null "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=$1" \
    || log "Telegram send failed"
}

[ -f "$LOG_CSV" ] || echo "date,ticker,verdict,conviction,action,account equity" > "$LOG_CSV"
# "account equity" = the desk's $5,000 book (mandate.py), not the $100k paper balance.

log "Cancelling leftover buy entries from previous days (sell-side stops are kept)"
twice "$PY" alpaca_paper.py cancel-stale 2>&1 | tee -a "$RUN_LOG"

for T in $TICKERS; do
  log "Research: $T"
  twice "$PY" research_floor.py --ticker "$T" --date "$TODAY" --rounds 2 >> "$RUN_LOG" 2>&1
  JSON="$DESK/reports/$T-$TODAY.json"
  if [ ! -f "$JSON" ]; then
    echo "$TODAY,$T,ERROR,,research failed,$("$PY" trade_journal.py --equity 2>/dev/null)" >> "$LOG_CSV"
    continue
  fi

  PLAN="$(twice "$PY" trade_from_verdict.py --file "$JSON" --dry-run 2>&1)"
  echo "$PLAN" >> "$RUN_LOG"
  if echo "$PLAN" | grep -q "^Action: none"; then
    ACTION="none"
  elif [ -e "$DESK/AUTO_ORDERS" ]; then
    # Permanent automatic orders (owner, 2026-09-25). Delete AUTO_ORDERS to go back to dry runs.
    PLACED="$("$PY" trade_from_verdict.py --file "$JSON" --auto 2>&1)"
    echo "$PLACED" >> "$RUN_LOG"
    N="$(echo "$PLACED" | grep -c '^Submitted')"
    if [ "$N" -gt 0 ]; then
      ACTION="placed $N order(s)"
    else
      ACTION="not placed: $(echo "$PLACED" | grep -E '^AUTO:|REJECTED' | head -1 | cut -c1-90)"
    fi
    log "$T: $ACTION"
  else
    ACTION="dry-run plan: $(echo "$PLAN" | grep -cE '^[A-Z0-9./-]+ +\| ') order(s)"
  fi

  SUMMARY="$("$PY" - "$JSON" <<'EOF'
import json, sys
v = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"{v['ticker']} {v['date']}")
print(f"Verdict: {v['verdict']}")
print(f"Conviction: {v['conviction']}")
print(f"Strongest bull point: {v['strongest_bull_point']}")
print(f"Strongest bear point: {v['strongest_bear_point']}")
print(f"What would flip the verdict: {v['what_would_flip']}")
EOF
)"
  telegram "$SUMMARY"

  VERDICT="$(echo "$SUMMARY" | sed -n 's/^Verdict: //p')"
  CONVICTION="$(echo "$SUMMARY" | sed -n 's/^Conviction: //p')"
  EQUITY="$("$PY" trade_journal.py --equity 2>/dev/null || echo "n/a")"
  echo "$TODAY,$T,$VERDICT,$CONVICTION,\"$ACTION\",$EQUITY" >> "$LOG_CSV"
done

log "Scoring all past calls against what prices did next"
twice "$PY" score_calls.py 2>&1 | grep -v Warning | tee -a "$RUN_LOG"

if target_reached; then
  log "Scorecard reached $TARGET_CALLS settled calls - creating STOP; future scheduled runs will exit."
  touch "$DESK/STOP"
  telegram "Trading desk: $TARGET_CALLS settled calls reached - daily runs stopped. See reports/scorecard.csv."
fi

log "Done. Log: $LOG_CSV"
