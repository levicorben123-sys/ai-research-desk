"""Health check for the trading desk (NOT the crypto bots). Runs every 15 minutes.

Fixes desk problems on the spot where that is safe, and alerts either way:
  * Scheduled tasks: TradingDeskDaily / TradingDeskWeeklyReview must exist and be
    enabled; a disabled one is re-enabled.
  * Missed daily run: on a market day, if today's run has not started by 08:45 ET
    (PC was off at 08:20, task failed to launch...), start it now.
  * Failed tickers: if today's run finished but a ticker's research failed twice,
    re-run research for just that ticker (once per ticker per day).
  * Stuck run: a daily run still going after 3 hours is reported (not killed).
Alerts go to reports/ALERTS.log and a Windows toast notification (no focus stealing;
runs under pythonw.exe so no console window ever appears).

Usage:
    python healthcheck.py
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

DESK = Path(__file__).resolve().parent
REPORTS = DESK / "reports"
ALERTS = REPORTS / "ALERTS.log"
PY = DESK / "tradingagents" / ".venv" / "Scripts" / "python.exe"
TASKS = ("TradingDeskDaily", "TradingDeskWeeklyReview")
TICKERS = ("NVDA", "JPM")  # XOM removed by owner 2026-09-24


# Never open a window: this runs every 15 minutes, possibly while a full-screen game
# has focus (a flashing console paused the owner's game - 2026-09-24).
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def ps(cmd: str) -> str:
    return subprocess.run(["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden",
                           "-Command", cmd], capture_output=True, text=True, timeout=60,
                          creationflags=NO_WINDOW).stdout.strip()


def notify(msg: str) -> None:
    """Windows toast notification: shows in the corner without taking focus, and Windows
    holds it back during full-screen games (Focus Assist)."""
    safe = msg.replace("'", "''")[:250]
    ps("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
       "ContentType=WindowsRuntime] > $null; "
       "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
       "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
       "$t = $x.GetElementsByTagName('text'); "
       "$t.Item(0).AppendChild($x.CreateTextNode('Trading desk')) > $null; "
       f"$t.Item(1).AppendChild($x.CreateTextNode('{safe}')) > $null; "
       "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
       "'Windows PowerShell').Show([Windows.UI.Notifications.ToastNotification]::new($x))")


def alert(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M} {msg}"
    REPORTS.mkdir(exist_ok=True)
    with ALERTS.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)
    notify(msg)


def once(tag: str) -> bool:
    """True the first time a tag is seen today (so each fix/alert happens once a day)."""
    stamp = REPORTS / f".health-{datetime.now():%Y-%m-%d}-{tag}"
    if stamp.exists():
        return False
    stamp.touch()
    return True


def check_tasks() -> None:
    for task in TASKS:
        state = ps(f"(Get-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue).State")
        if not state:
            if once(f"missing-{task}"):
                alert(f"Scheduled task {task} is missing - needs re-registering (ask Claude).")
        elif state == "Disabled":
            ps(f"Enable-ScheduledTask -TaskName '{task}' | Out-Null")
            alert(f"Scheduled task {task} was disabled - re-enabled it.")


def check_daily_run() -> None:
    now = datetime.now()
    if now.weekday() >= 5 or (now.hour, now.minute) < (8, 45) or (DESK / "STOP").exists():
        return
    if subprocess.run([str(PY), str(DESK / "alpaca_paper.py"), "is-trading-day"],
                      capture_output=True, cwd=DESK, creationflags=NO_WINDOW).returncode != 0:
        return
    log = REPORTS / f"run_daily-{now:%Y-%m-%d}.log"
    running = ps("(Get-ScheduledTask -TaskName TradingDeskDaily).State") == "Running"

    if not log.exists():
        if not running and once("start-daily"):
            ps("Start-ScheduledTask -TaskName TradingDeskDaily")
            alert("Today's desk run had not started by 08:45 - started it now "
                  "(it takes ~50 min, so plans land after the open).")
        return

    text = log.read_text(encoding="utf-8", errors="replace")
    if running:
        started = datetime.fromtimestamp(log.stat().st_ctime)
        if (now - started).total_seconds() > 3 * 3600 and once("stuck-daily"):
            alert("Today's desk run has been going for over 3 hours - may be stuck; "
                  f"see {log.name}.")
        return
    if "Done." not in text:
        return
    # A ticker failed only if run_daily.sh logged an ERROR row for it today. (Do not
    # infer failure from a missing report file: reports can be archived or moved,
    # which on 2026-09-23 made this check re-run three tickers by mistake.)
    failed = []
    log_csv = REPORTS / "daily_log.csv"
    if log_csv.exists():
        for row in log_csv.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = row.split(",")
            if len(parts) > 2 and parts[0] == f"{now:%Y-%m-%d}" and parts[2] == "ERROR":
                failed.append(parts[1])
    for t in TICKERS:
        if t not in failed:
            continue
        if once(f"rerun-{t}"):
            with log.open("a", encoding="utf-8") as f:
                f.write(f"\n[{now:%H:%M:%S}] healthcheck: re-running research for {t} "
                        "(it failed in the scheduled run)\n")
            subprocess.Popen([str(PY), str(DESK / "research_floor.py"), "--ticker", t,
                              "--date", f"{now:%Y-%m-%d}", "--rounds", "2"],
                             cwd=DESK, stdout=log.open("a", encoding="utf-8"),
                             stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
            alert(f"{t} research failed in today's run - re-running it now.")


def main() -> int:
    for check in (check_tasks, check_daily_run):
        try:
            check()
        except Exception as exc:  # a broken check must not stop the others
            alert(f"health check {check.__name__} errored: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
