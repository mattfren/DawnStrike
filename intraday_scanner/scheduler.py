"""Local schedule construction for Dawnstrike production workflows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from intraday_scanner.models import utc_now_iso
from intraday_scanner.notifiers.base import NotificationEvent
from intraday_scanner.services.market_calendar import early_close_time_ct, is_market_day


@dataclass(frozen=True)
class ScheduledJob:
    name: str
    time_ct: str
    command: str
    description: str
    market_day_only: bool = True
    max_retries: int = 2
    retry_delay_seconds: int = 60


def build_default_schedule() -> list[ScheduledJob]:
    return [
        ScheduledJob(
            "build-premarket-snapshot",
            "08:00",
            "build-snapshot",
            "Pull/build the premarket snapshot.",
        ),
        ScheduledJob(
            "morning-run",
            "08:10",
            "intraday-scan morning-run --db-path data\\scanner.sqlite",
            "Rank candidates and persist recommendations.",
        ),
        ScheduledJob(
            "push-recommendations",
            "08:15",
            "intraday-scan notify --db-path data\\scanner.sqlite",
            "Push top research recommendations.",
        ),
        ScheduledJob(
            "monitor-open",
            "08:30",
            (
                "intraday-scan monitor-open --provider alpaca "
                "--db-path data\\scanner.sqlite --continuous"
            ),
            "Start 1-minute market-open monitoring.",
        ),
        ScheduledJob(
            "lunch-audit",
            "11:30",
            "intraday-scan audit-latest --db-path data\\scanner.sqlite",
            "Calculate lunch paper returns.",
        ),
        ScheduledJob(
            "close-audit",
            "15:00",
            "intraday-scan audit-latest --db-path data\\scanner.sqlite",
            "Calculate close paper returns.",
        ),
        ScheduledJob(
            "performance-update",
            "15:10",
            "intraday-scan performance-report --db-path data\\scanner.sqlite --persist",
            "Update historical performance.",
        ),
    ]


def schedule_as_rows() -> list[dict[str, str]]:
    today = date.today()
    return schedule_as_rows_for_date(today)


def schedule_as_rows_for_date(value: date) -> list[dict[str, Any]]:
    market_open = is_market_day(value)
    early_close = early_close_time_ct(value)
    rows = []
    for job in build_default_schedule():
        rows.append(
            {
                **job.__dict__,
                "date": value.isoformat(),
                "market_day": market_open,
                "early_close_time_ct": early_close or "",
                "will_run": market_open or not job.market_day_only,
                "skip_reason": "" if market_open or not job.market_day_only else "market closed",
            }
        )
    return rows


def record_scheduler_failure(store: Any, job: ScheduledJob, error: Exception) -> None:
    recorder = getattr(store, "record_provider_health", None)
    if callable(recorder):
        recorder(
            "scheduler",
            "error",
            utc_now_iso(),
            f"{job.name} failed: {str(error)[:300]}",
        )


def scheduler_failure_event(job: ScheduledJob, error: Exception) -> NotificationEvent:
    return NotificationEvent(
        event_key=f"scheduler:{job.name}:failure",
        title=f"Dawnstrike scheduler failure: {job.name}",
        body=f"{job.name} failed after scheduled execution. Error: {str(error)[:300]}",
        channel_hint="scheduler",
        ticker=None,
        payload={
            "job": job.__dict__,
            "error": str(error)[:500],
            "created_at": utc_now_iso(),
        },
    )
