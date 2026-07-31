from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from intraday_scanner.errors import MarketCalendarCoverageError
from intraday_scanner.services import alpha_cycle_service
from intraday_scanner.services.market_calendar import (
    CALENDAR_AUTHORITY,
    CALENDAR_ID,
    CALENDAR_SOURCE_REFS,
    MarketSessionStatus,
    core_session_phase,
    early_close_time_ct,
    first_eligible_session_date,
    is_market_day,
    main,
    market_session,
    next_session_after_registration,
    registration_coverage_inception_date,
    session_for_timestamp,
)


def test_published_2026_exchange_schedule_has_explicit_lineage() -> None:
    july_second = market_session(date(2026, 7, 2))
    july_third = market_session(date(2026, 7, 3))
    thanksgiving_friday = market_session(date(2026, 11, 27))
    christmas_eve = market_session(date(2026, 12, 24))

    assert july_second.status == MarketSessionStatus.OPEN
    assert july_second.close_time_et == "16:00"
    assert july_second.calendar_id == CALENDAR_ID
    assert july_second.calendar_authority == CALENDAR_AUTHORITY
    assert july_second.source_refs == CALENDAR_SOURCE_REFS
    assert july_third.status == MarketSessionStatus.CLOSED
    assert july_third.reason == "Independence Day observed"
    assert thanksgiving_friday.status == MarketSessionStatus.EARLY_CLOSE
    assert thanksgiving_friday.close_time_et == "13:00"
    assert early_close_time_ct(date(2026, 11, 27)) == "12:00"
    assert christmas_eve.status == MarketSessionStatus.EARLY_CLOSE


def test_calendar_closes_weekends_and_fails_outside_vetted_coverage() -> None:
    assert not is_market_day(date(2026, 7, 4))
    with pytest.raises(MarketCalendarCoverageError, match="covers 2026-01-01 through 2028-12-31"):
        market_session(date(2029, 1, 2))
    with pytest.raises(MarketCalendarCoverageError):
        is_market_day(date(2025, 12, 31))


def test_published_2027_and_2028_boundaries_and_early_closes() -> None:
    assert market_session(date(2027, 1, 1)).status == MarketSessionStatus.CLOSED
    assert market_session(date(2027, 6, 18)).reason.endswith("observed")
    assert market_session(date(2027, 11, 26)).status == MarketSessionStatus.EARLY_CLOSE
    assert market_session(date(2027, 12, 24)).status == MarketSessionStatus.CLOSED

    assert market_session(date(2027, 12, 31)).status == MarketSessionStatus.OPEN
    assert market_session(date(2028, 1, 1)).reason == "weekend"
    assert market_session(date(2028, 7, 3)).status == MarketSessionStatus.EARLY_CLOSE
    assert market_session(date(2028, 7, 4)).status == MarketSessionStatus.CLOSED
    assert market_session(date(2028, 11, 24)).close_time_et == "13:00"
    assert market_session(date(2028, 12, 31)).status == MarketSessionStatus.CLOSED


def test_timestamp_resolution_and_early_close_core_session_phases() -> None:
    decision = session_for_timestamp(datetime(2026, 7, 3, 2, 0, tzinfo=timezone.utc))
    assert decision.market_date == "2026-07-02"
    assert decision.status == MarketSessionStatus.OPEN

    assert (
        core_session_phase(datetime(2026, 11, 27, 14, 29, tzinfo=timezone.utc))
        == "before_core_session"
    )
    assert (
        core_session_phase(datetime(2026, 11, 27, 15, 0, tzinfo=timezone.utc))
        == "core_session_open"
    )
    assert (
        core_session_phase(datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc))
        == "after_core_session"
    )


def test_strategy_registration_uses_first_actually_eligible_session() -> None:
    assert first_eligible_session_date(
        datetime(2026, 7, 16, 19, 59, tzinfo=timezone.utc)
    ) == date(2026, 7, 16)
    assert first_eligible_session_date(
        datetime(2026, 7, 16, 20, 1, tzinfo=timezone.utc)
    ) == date(2026, 7, 17)
    assert first_eligible_session_date(
        datetime(2026, 7, 17, 21, 0, tzinfo=timezone.utc)
    ) == date(2026, 7, 20)
    with pytest.raises(ValueError, match="must include a timezone"):
        first_eligible_session_date(datetime(2026, 7, 16, 12, 0))


def test_new_catalog_activation_waits_until_next_session_without_close_race() -> None:
    assert next_session_after_registration(
        datetime(2026, 7, 16, 19, 59, 59, 999999, tzinfo=timezone.utc)
    ) == date(2026, 7, 17)
    assert next_session_after_registration(
        datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc)
    ) == date(2026, 7, 20)
    assert next_session_after_registration(
        datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)
    ) == date(2026, 11, 27)
    with pytest.raises(ValueError, match="must include a timezone"):
        next_session_after_registration(datetime(2026, 7, 16, 12, 0))
    assert registration_coverage_inception_date(
        datetime(2026, 7, 16, 19, 59, tzinfo=timezone.utc),
        "first_eligible_session",
    ) == date(2026, 7, 16)
    assert registration_coverage_inception_date(
        datetime(2026, 7, 16, 19, 59, tzinfo=timezone.utc),
        "next_market_session_after_registration",
    ) == date(2026, 7, 17)
    with pytest.raises(ValueError, match="unsupported registration activation policy"):
        registration_coverage_inception_date(
            datetime(2026, 7, 16, 19, 59, tzinfo=timezone.utc),
            "unknown",
        )


def test_calendar_shell_gate_codes_distinguish_closed_from_unavailable(capsys) -> None:
    assert main(["--date", "2026-07-02"]) == 0
    assert main(["--date", "2026-07-03"]) == 10
    assert main(["--date", "2029-01-02"]) == 11
    assert main(["--date", "not-a-date"]) == 11
    output = capsys.readouterr().out
    assert '"calendar_unavailable"' in output


def test_alpha_cycle_external_notification_skips_closed_session_before_collection(
    tmp_path, monkeypatch
) -> None:
    def unexpected_collection(**_kwargs):
        raise AssertionError("closed session must not collect market data")

    monkeypatch.setattr(alpha_cycle_service, "web_auto_collect", unexpected_collection)
    result = alpha_cycle_service.alpha_cycle(
        db_path=tmp_path / "alpha.sqlite",
        out_dir=tmp_path / "alpha",
        notify="telegram",
        dry_run=True,
        as_of=datetime(2026, 7, 3, 13, 0, tzinfo=timezone.utc),
    )

    assert result["status"] == "skipped_market_closed"
    assert result["notification_stats"]["sent"] == 0
    assert result["session_gate"]["reason"] == "Independence Day observed"
    assert (tmp_path / "alpha/alpha_session_gate.json").exists()
    assert not (tmp_path / "alpha.sqlite").exists()


def test_alpha_cycle_external_notification_fails_closed_without_calendar(tmp_path) -> None:
    with pytest.raises(MarketCalendarCoverageError):
        alpha_cycle_service.alpha_cycle(
            db_path=tmp_path / "alpha.sqlite",
            out_dir=tmp_path / "alpha",
            notify="telegram",
            dry_run=True,
            as_of=datetime(2029, 1, 2, 14, 0, tzinfo=timezone.utc),
        )


def test_alpha_cycle_external_notification_skips_after_premarket(
    tmp_path, monkeypatch
) -> None:
    def unexpected_collection(**_kwargs):
        raise AssertionError("outside-premarket cycle must not collect market data")

    monkeypatch.setattr(alpha_cycle_service, "web_auto_collect", unexpected_collection)
    result = alpha_cycle_service.alpha_cycle(
        db_path=tmp_path / "alpha.sqlite",
        out_dir=tmp_path / "alpha",
        notify="telegram",
        dry_run=True,
        as_of=datetime(2026, 7, 13, 14, 0, tzinfo=timezone.utc),
    )

    assert result["status"] == "skipped_outside_premarket_session"
    assert result["phase"] == "core_session_open"
    assert result["notification_stats"]["sent"] == 0
    assert not (tmp_path / "alpha.sqlite").exists()


def test_alpha_monitor_external_notification_only_runs_during_core_session(tmp_path) -> None:
    before_open = alpha_cycle_service.alpha_monitor(
        db_path=tmp_path / "alpha.sqlite",
        notify="telegram",
        dry_run=True,
        as_of=datetime(2026, 11, 27, 14, 29, tzinfo=timezone.utc),
    )
    after_early_close = alpha_cycle_service.alpha_monitor(
        db_path=tmp_path / "alpha.sqlite",
        notify="telegram",
        dry_run=True,
        as_of=datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc),
    )

    assert before_open["status"] == "skipped_outside_core_session"
    assert before_open["phase"] == "before_core_session"
    assert after_early_close["status"] == "skipped_outside_core_session"
    assert after_early_close["phase"] == "after_core_session"
    assert not (tmp_path / "alpha.sqlite").exists()
