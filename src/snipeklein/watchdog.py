"""Watchdog: notifies Discord when the bot is unhealthy.

The watchdog is deliberately quiet:

* a grace period after start covers the first (slow) poll cycle,
* an alarm needs several consecutive failed checks,
* an alarm is repeated at most once per cooldown,
* a recovery notice is sent when the bot comes back.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .health import STATUS_OK

log = __import__("logging").getLogger("snipeklein.watchdog")

ACTION_OK = "ok"
ACTION_STARTING = "starting"
ACTION_PENDING = "pending"
ACTION_ALERT = "alert"
ACTION_RECOVERED = "recovered"
ACTION_COOLDOWN = "cooldown"
ACTION_GRACE = "grace"


@dataclass
class WatchdogState:
    failures: int = 0
    alerting: bool = False
    last_alert: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"failures": self.failures, "alerting": self.alerting, "last_alert": self.last_alert}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WatchdogState:
        return cls(
            failures=int(data.get("failures", 0)),
            alerting=bool(data.get("alerting", False)),
            last_alert=float(data.get("last_alert", 0.0)),
        )


@dataclass
class Watchdog:
    """Checks the bot health and reports problems to Discord."""

    config: Config
    report: Callable[[], dict[str, Any]]
    notify: Callable[[str], bool]
    interval: int = 300
    grace: int = 600
    failures: int = 3
    cooldown: int = 21600
    state_path: Path | None = None
    clock: Callable[[], float] = time.monotonic
    wall_clock: Callable[[], float] = time.time
    sleeper: Callable[[float], None] = time.sleep
    state: WatchdogState = field(default_factory=WatchdogState)

    def __post_init__(self) -> None:
        self._started = self.clock()
        self.state = self._load_state()

    # -- state -------------------------------------------------------------
    def _load_state(self) -> WatchdogState:
        if self.state_path is None or not self.state_path.exists():
            return WatchdogState()
        try:
            return WatchdogState.from_dict(json.loads(self.state_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return WatchdogState()

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(self.state.to_dict(), indent=2), encoding="utf-8"
            )
        except OSError:  # pragma: no cover - never break the watchdog
            log.exception("could not write the watchdog state")

    # -- logic -------------------------------------------------------------
    def tick(self) -> str:
        """Run one check, returns what was done."""
        in_grace = self.clock() - self._started < self.grace
        report = self.report()
        status = report.get("status", "attention")

        if status == STATUS_OK:
            if self.state.alerting:
                self.notify(self._recovery_message(report))
                self.state = WatchdogState()
                self._save_state()
                return ACTION_RECOVERED
            self.state.failures = 0
            self._save_state()
            return ACTION_OK

        self.state.failures += 1
        if in_grace:
            # during the grace period we count but never alarm - the bot may
            # simply be finishing its first (slow) cycle
            self._save_state()
            return ACTION_GRACE

        if self.state.failures < self.failures:
            self._save_state()
            return ACTION_PENDING

        now = self.wall_clock()
        if self.state.alerting and now - self.state.last_alert < self.cooldown:
            self._save_state()
            return ACTION_COOLDOWN

        self.notify(self._alert_message(report))
        self.state.alerting = True
        self.state.last_alert = now
        self._save_state()
        return ACTION_ALERT

    def run(self) -> None:  # pragma: no cover - long running
        log.info(
            "watchdog started (interval=%ss grace=%ss failures=%s cooldown=%ss)",
            self.interval,
            self.grace,
            self.failures,
            self.cooldown,
        )
        while True:
            try:
                action = self.tick()
                if action in (ACTION_ALERT, ACTION_RECOVERED):
                    log.info("watchdog: %s", action)
            except Exception:
                log.exception("watchdog check failed")
            self.sleeper(self.interval)

    # -- messages ----------------------------------------------------------
    def _alert_message(self, report: dict[str, Any]) -> str:
        age = report.get("last_run_age_s")
        age_text = "never" if age is None else f"{int(age)}s ago"
        reasons = []
        if report.get("stale"):
            reasons.append("no recent run")
        if report.get("last_run") and report["last_run"].get("errors"):
            reasons.append(f"{report['last_run']['errors']} errors in the last run")
        if report.get("missing"):
            reasons.append("missing " + ", ".join(report["missing"]))
        detail = "; ".join(reasons) or "unknown problem"
        return (
            f"🔴 SnipeKlein is unhealthy - {detail}\n"
            f"last run: {age_text} | listings: {report.get('listings', 0)} | "
            f"alerts: {report.get('alerts', 0)}\n"
            f"check the logs: docker compose logs bot"
        )

    def _recovery_message(self, report: dict[str, Any]) -> str:
        return (
            "🟢 SnipeKlein is healthy again - "
            f"last run {int(report.get('last_run_age_s') or 0)}s ago, "
            f"{report.get('alerts', 0)} alerts sent"
        )
