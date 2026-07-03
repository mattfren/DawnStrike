"""Windows Task Scheduler automation layer for OMEGA operations."""

from intraday_scanner.v2.autonomous_runner.core import (
    TASKS,
    doctor,
    init,
    install,
    report,
    status,
    test_run,
    uninstall,
    verify,
    watchdog,
)

__all__ = [
    "TASKS",
    "doctor",
    "init",
    "install",
    "report",
    "status",
    "test_run",
    "uninstall",
    "verify",
    "watchdog",
]
