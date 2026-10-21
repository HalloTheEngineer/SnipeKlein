"""The watchdog: grace period, failure counter, cooldown, recovery notice."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from snipeklein.watchdog import (
    ACTION_ALERT,
    ACTION_COOLDOWN,
    ACTION_GRACE,
    ACTION_OK,
    ACTION_PENDING,
    ACTION_RECOVERED,
    Watchdog,
    WatchdogState,
)


class FakeClock:
    def __init__(self) -> None:
        self.mono = 0.0
        self.wall = 1_700_000_000.0

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += seconds


def _watchdog(tmp_path: Path, clock: FakeClock, report: dict[str, Any], messages: list[str]):
    def get_report() -> dict[str, Any]:
        return report

    def notify(message: str) -> bool:
        messages.append(message)
        return True

    return Watchdog(
        config=None,  # type: ignore[arg-type]
        report=get_report,
        notify=notify,
        interval=300,
        grace=600,
        failures=3,
        cooldown=21600,
        state_path=tmp_path / "watchdog_state.json",
        clock=clock.monotonic,
        wall_clock=clock.time,
        sleeper=lambda _s: None,
    )


def _report(status: str, **extra: Any) -> dict[str, Any]:
    report = {
        "status": status,
        "stale": status == "attention",
        "missing": [],
        "last_run": {"errors": 0},
        "last_run_age_s": 30,
        "listings": 3,
        "alerts": 1,
    }
    report.update(extra)
    return report


def test_no_alarm_during_the_grace_period(tmp_path: Path) -> None:
    """The bot may still be finishing its first cycle - stay quiet."""
    clock = FakeClock()
    messages: list[str] = []
    watchdog = _watchdog(tmp_path, clock, _report("attention"), messages)

    for _ in range(5):
        assert watchdog.tick() == ACTION_GRACE
    assert messages == []

    # the grace period is over: the accumulated failures now trigger the alarm
    clock.advance(700)
    assert watchdog.tick() == ACTION_ALERT
    assert len(messages) == 1


def test_alarm_after_three_consecutive_failures(tmp_path: Path) -> None:
    clock = FakeClock()
    messages: list[str] = []
    watchdog = _watchdog(tmp_path, clock, _report("attention"), messages)
    clock.advance(700)

    assert watchdog.tick() == ACTION_PENDING
    assert watchdog.tick() == ACTION_PENDING
    assert watchdog.tick() == ACTION_ALERT
    assert len(messages) == 1
    assert "unhealthy" in messages[0]


def test_no_second_alarm_within_the_cooldown(tmp_path: Path) -> None:
    clock = FakeClock()
    messages: list[str] = []
    watchdog = _watchdog(tmp_path, clock, _report("attention"), messages)
    clock.advance(700)

    for _ in range(3):
        watchdog.tick()
    assert len(messages) == 1

    clock.advance(3600)
    watchdog.tick()
    assert len(messages) == 1  # still inside the cooldown
    assert watchdog.tick() == ACTION_COOLDOWN

    clock.advance(6 * 3600)
    watchdog.tick()
    assert len(messages) == 2


def test_recovery_notice(tmp_path: Path) -> None:
    clock = FakeClock()
    messages: list[str] = []
    report = _report("attention")
    watchdog = _watchdog(tmp_path, clock, report, messages)
    clock.advance(700)

    for _ in range(3):
        watchdog.tick()
    assert len(messages) == 1

    report["status"] = "ok"
    report["stale"] = False
    assert watchdog.tick() == ACTION_RECOVERED
    assert len(messages) == 2
    assert "healthy again" in messages[1]

    # no further messages while everything is fine
    assert watchdog.tick() == ACTION_OK
    assert len(messages) == 2


def test_no_recovery_message_for_a_stale_alarm(tmp_path: Path) -> None:
    """A state file from an old deployment must not trigger a recovery note."""
    clock = FakeClock()
    messages: list[str] = []
    watchdog = _watchdog(tmp_path, clock, _report("ok"), messages)
    # pretend an alarm was raised two days ago
    watchdog.state = WatchdogState(failures=3, alerting=True, last_alert=clock.wall - 2 * 86400)

    assert watchdog.tick() == ACTION_RECOVERED
    assert messages == []


def test_state_survives_a_restart(tmp_path: Path) -> None:
    """A restarted watchdog must not re-alarm immediately."""
    clock = FakeClock()
    messages: list[str] = []
    watchdog = _watchdog(tmp_path, clock, _report("attention"), messages)
    clock.advance(700)
    for _ in range(3):
        watchdog.tick()
    assert len(messages) == 1

    state = json.loads((tmp_path / "watchdog_state.json").read_text())
    assert state["alerting"] is True
    assert state["failures"] >= 3

    # restart: the same state file is picked up again, no second alarm
    restarted = _watchdog(tmp_path, clock, _report("attention"), messages)
    clock.advance(60)
    restarted.tick()
    assert len(messages) == 1
