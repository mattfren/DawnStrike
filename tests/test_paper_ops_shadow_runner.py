from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from intraday_scanner.v2.data import MarketBar, MarketDataset, write_ohlcv_csv
from intraday_scanner.v2.data_truth import build_data_truth_snapshot
from intraday_scanner.v2.data_truth.models import DataTruthManifest
from intraday_scanner.v2.paper_ops import engine as paper_engine
from intraday_scanner.v2.paper_ops import shadow_runner
from intraday_scanner.v2.paper_ops.models import (
    PAPER_EXECUTION_POLICY_VERSION,
    PaperRunMode,
    StrategyCalendarRow,
    StrategyPaperAccount,
)
from intraday_scanner.v2.paper_ops.storage import (
    append_jsonl_unique,
    read_json,
    read_jsonl,
    upsert_rows,
    write_json,
)
from intraday_scanner.v2.strategies import Direction, StrategySignal, StrategySpec

STRATEGY_ID = "fixture_shadow_parent"
CHALLENGER_ID = "fixture_shadow_parent_candidate_v2"


def _always_long(
    spec: StrategySpec,
    _dataset: MarketDataset,
    symbol: str,
    bars: tuple[MarketBar, ...],
    index: int,
) -> StrategySignal:
    close = bars[index].close
    return StrategySignal(
        strategy_id=spec.strategy_id,
        strategy_version=spec.version,
        symbol=symbol,
        signal_index=index,
        direction=Direction.LONG,
        entry_reference=close,
        stop=close - 1.0,
        target=close + 2.0,
        score=90.0,
        evidence=("deterministic fixture signal",),
        invalidation="close below the frozen stop",
    )


def _parent_strategy() -> StrategySpec:
    return StrategySpec(
        strategy_id=STRATEGY_ID,
        version="v1.0",
        status="experimental",
        description="Deterministic shadow lifecycle fixture",
        compatible_timeframe="1d",
        required_data_fields=("open", "high", "low", "close", "volume"),
        parameters={},
        indicators=(),
        entry_logic="Emit one deterministic long signal.",
        exit_logic="Use the fixed target or stop.",
        stop_logic="One point below signal close.",
        target_logic="Two points above signal close.",
        position_sizing_assumption="PaperOps risk policy.",
        known_failure_modes=(),
        validation_status="fixture",
        generate_signal=_always_long,
    )


def test_shadow_activation_uses_new_york_market_date_at_utc_midnight() -> None:
    assert shadow_runner._market_date_from_timestamp(
        "2026-07-17T00:30:00+00:00",
        field="registered_at",
    ) == date(2026, 7, 16)


def test_champion_source_context_skips_only_pre_inception_forward_series(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "paper"
    parent = _parent_strategy()
    monkeypatch.setattr(shadow_runner, "build_strategy_catalog", lambda: (parent,))
    run_date = date(2026, 7, 16)
    _seed_root(root, parent)
    _seed_champion_day(root, run_date)
    paths = paper_engine.PaperOpsPaths.create(root)
    future_strategy_id = "future_registered_strategy"
    future_fingerprint = "f" * 64
    registry = read_json(paths.state / "strategy_registry.json", [])
    assert isinstance(registry, list)
    registry.append(
        {
            "strategy_id": future_strategy_id,
            "strategy_version": "v1.0",
            "strategy_status": "experimental",
            "execution_policy_version": PAPER_EXECUTION_POLICY_VERSION,
            "strategy_semantics_fingerprint": future_fingerprint,
        }
    )
    write_json(paths.state / "strategy_registry.json", registry)
    semantics_manifest = read_json(
        paths.state / "strategy_semantics_manifest.json",
        {},
    )
    assert isinstance(semantics_manifest, dict)
    strategies = semantics_manifest["strategies"]
    assert isinstance(strategies, dict)
    future_entry = {
        "activation_policy": "next_market_session_after_registration",
        "configuration": {},
        "coverage_inception_date": "2026-07-17",
        "fingerprint": future_fingerprint,
        "registered_at": "2026-07-16T18:00:00+00:00",
    }
    strategies[f"{future_strategy_id}@v1.0"] = future_entry
    write_json(paths.state / "strategy_semantics_manifest.json", semantics_manifest)

    source = shadow_runner._champion_source_context(
        root,
        run_date,
        PaperRunMode.FORWARD,
    )

    assert source["run_id"].startswith("paper_ops:forward:2026-07-16:")
    future_entry["registered_at"] = "2026-07-15T18:00:00+00:00"
    future_entry["coverage_inception_date"] = "2026-07-16"
    write_json(paths.state / "strategy_semantics_manifest.json", semantics_manifest)
    with pytest.raises(
        ValueError,
        match=r"champion calendar lineage is incomplete for future_registered_strategy@v1.0",
    ):
        shadow_runner._champion_source_context(
            root,
            run_date,
            PaperRunMode.FORWARD,
        )


def test_new_candidate_semantics_ignore_unrelated_runner_module_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "paper"
    parent = _parent_strategy()
    monkeypatch.setattr(shadow_runner, "build_strategy_catalog", lambda: (parent,))
    _seed_root(root, parent)
    shadow_runner.initialize_shadow_registry(output_root=root)
    registration = _seed_registered_challenger(
        root,
        _registration_manifest(),
        registered_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
    )
    assert registration["candidate_semantics_contract"] == (
        shadow_runner.SCOPED_CANDIDATE_SEMANTICS_CONTRACT
    )
    candidate = shadow_runner._build_candidate_strategy(registration, root)
    generic_before = paper_engine._strategy_semantics_fingerprint(candidate)
    frozen = registration["candidate_strategy_semantics_fingerprint"]

    _simulate_unrelated_runner_module_drift(monkeypatch)

    assert paper_engine._strategy_semantics_fingerprint(candidate) != generic_before
    assert shadow_runner._scoped_candidate_semantics_fingerprint(
        candidate,
        registration,
    ) == frozen
    assert shadow_runner.verify_registration_integrity(
        registration,
        output_root=root,
    ) == ()


def test_legacy_candidate_accepts_only_scoped_drift_and_preserves_frozen_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "paper"
    parent = _parent_strategy()
    monkeypatch.setattr(shadow_runner, "build_strategy_catalog", lambda: (parent,))
    run_date = date.today() + timedelta(days=1)
    _seed_root(root, parent)
    shadow_runner.initialize_shadow_registry(output_root=root)
    registration = _seed_registered_challenger(
        root,
        {
            **_registration_manifest(),
            "frozen_at": f"{(run_date - timedelta(days=1)).isoformat()}T12:00:00+00:00",
        },
        registered_at=(
            f"{(run_date - timedelta(days=1)).isoformat()}T13:00:00+00:00"
        ),
        candidate_semantics_contract=(
            shadow_runner.LEGACY_CANDIDATE_SEMANTICS_CONTRACT
        ),
    )
    assert "candidate_semantics_contract" not in registration
    frozen = str(registration["candidate_strategy_semantics_fingerprint"])
    registry_path = root / "state" / "strategy_challenger_registry.json"
    event_path = root / "state" / "shadow_registration_ledger.jsonl"
    registry_before = registry_path.read_bytes()
    event_before = event_path.read_bytes()
    candidate = shadow_runner._build_candidate_strategy(registration, root)
    generic_before = paper_engine._strategy_semantics_fingerprint(candidate)

    _simulate_unrelated_runner_module_drift(monkeypatch)

    assert paper_engine._strategy_semantics_fingerprint(candidate) != generic_before
    assert shadow_runner.verify_registration_integrity(
        registration,
        output_root=root,
    ) == ()
    _seed_champion_day(root, run_date)
    _write_truth_audits(root)

    def fake_loader(
        **_kwargs: object,
    ) -> tuple[MarketDataset, DataTruthManifest, tuple[str, ...]]:
        snapshot = f"sourced-shadow-{run_date.isoformat()}"
        return _dataset(run_date), _manifest(run_date, snapshot), ()

    monkeypatch.setattr(shadow_runner, "_load_retained_champion_snapshot", fake_loader)
    result = shadow_runner.run_shadow_day(
        run_date=run_date,
        mode=PaperRunMode.FORWARD,
        output_root=root,
        allow_fetch=False,
    )

    assert result["status"] == "passed"
    assert registry_path.read_bytes() == registry_before
    assert event_path.read_bytes() == event_before
    manifest = read_json(
        root / "manifests" / f"shadow_forward_{run_date}_{CHALLENGER_ID}.json",
        {},
    )
    assert manifest["strategy_semantics_fingerprint"] == frozen
    account = read_json(
        root / "state" / "shadow" / CHALLENGER_ID / "forward_account.json",
        {},
    )
    assert account["account"]["strategy_semantics_fingerprint"] == frozen
    pending = read_json(
        root / "state" / "shadow" / CHALLENGER_ID / "forward_pending_orders.json",
        [],
    )
    assert pending
    assert all(row["strategy_semantics_fingerprint"] == frozen for row in pending)
    decisions = read_json(
        root
        / "exports"
        / f"shadow_strategy_decisions_forward_{run_date}_{CHALLENGER_ID}.json",
        [],
    )
    assert decisions
    assert all(row["strategy_semantics_fingerprint"] == frozen for row in decisions)
    candidate_event_payloads = [
        event["payload"]
        for event in read_jsonl(root / "ledger" / "paper_ledger.jsonl")
        if isinstance(event.get("payload"), dict)
        and event["payload"].get("strategy_version")
        == registration["candidate_strategy_version"]
    ]
    assert candidate_event_payloads
    assert all(
        row["strategy_semantics_fingerprint"] == frozen
        for row in candidate_event_payloads
    )
    candidate_calendar = next(
        row
        for row in paper_engine._read_calendar_rows(
            paper_engine.PaperOpsPaths.create(root)
        )
        if row.get("strategy_version") == registration["candidate_strategy_version"]
    )
    assert candidate_calendar["strategy_semantics_fingerprint"] == frozen


def test_retained_snapshot_forward_gate_runs_before_immutable_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_date = date(2026, 7, 15)
    immutable_loads: list[str] = []
    monkeypatch.setattr(
        paper_engine,
        "_current_utc_time",
        lambda: datetime(2026, 7, 15, 19, 0, tzinfo=timezone.utc),
    )

    def unexpected_load(snapshot_id: str, _data_truth_root: Path) -> object:
        immutable_loads.append(snapshot_id)
        raise AssertionError("immutable load must not run before the session close")

    monkeypatch.setattr(shadow_runner, "load_datatruth_snapshot", unexpected_load)

    with pytest.raises(ValueError, match="before the US equities regular session"):
        shadow_runner._load_retained_champion_snapshot(
            output_root=tmp_path / "paper",
            run_date=run_date,
            mode=PaperRunMode.FORWARD,
            snapshot_id="retained-champion-snapshot",
            universe_symbols=("TST",),
        )

    assert immutable_loads == []
    assert not (tmp_path / "paper").exists()


def test_shadow_day_loads_exact_retained_champion_snapshot_when_latest_differs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "paper"
    parent = _parent_strategy()
    run_date = date(2026, 7, 15)
    monkeypatch.setattr(
        paper_engine,
        "_current_utc_time",
        lambda: datetime(2026, 7, 15, 21, 30, tzinfo=timezone.utc),
    )
    data_truth_root = root.parent / "v2_data_truth"
    champion_manifest = _build_retained_data_truth_snapshot(
        data_truth_root=data_truth_root,
        fixture_root=tmp_path / "champion_source",
        run_date=run_date,
        revision="champion",
        close=100.0,
    )
    latest_manifest = _build_retained_data_truth_snapshot(
        data_truth_root=data_truth_root,
        fixture_root=tmp_path / "revised_source",
        run_date=run_date,
        revision="revised-latest",
        close=101.0,
    )
    assert latest_manifest.snapshot_id != champion_manifest.snapshot_id
    latest_alias = read_json(data_truth_root / "manifests" / "latest.json", {})
    assert latest_alias["snapshot_id"] == latest_manifest.snapshot_id

    monkeypatch.setattr(shadow_runner, "build_strategy_catalog", lambda: (parent,))
    _seed_root(root, parent)
    shadow_runner.initialize_shadow_registry(output_root=root)
    _seed_registered_challenger(
        root,
        {
            **_registration_manifest(),
            "frozen_at": (
                f"{(run_date - timedelta(days=1)).isoformat()}T12:00:00+00:00"
            ),
        },
        registered_at=(
            f"{(run_date - timedelta(days=1)).isoformat()}T13:00:00+00:00"
        ),
    )
    _seed_champion_day(
        root,
        run_date,
        snapshot_id=champion_manifest.snapshot_id,
    )
    _write_truth_audits(root)

    def fail_if_refetched(**_kwargs: object) -> object:
        raise AssertionError("shadow recovery must never refetch the latest snapshot")

    monkeypatch.setattr(paper_engine, "_load_dataset_for_mode", fail_if_refetched)

    result = shadow_runner.run_shadow_day(
        run_date=run_date,
        mode=PaperRunMode.FORWARD,
        output_root=root,
        allow_fetch=True,
    )

    assert result["status"] == "passed"
    assert result["data_snapshot_id"] == champion_manifest.snapshot_id
    assert read_json(data_truth_root / "manifests" / "latest.json", {})[
        "snapshot_id"
    ] == latest_manifest.snapshot_id
    decisions = read_json(
        root
        / "exports"
        / f"shadow_strategy_decisions_forward_{run_date}_{CHALLENGER_ID}.json",
        [],
    )
    assert decisions[0]["entry_reference"] == 100.0


@pytest.mark.parametrize(
    "failure_mode",
    ("missing", "tampered"),
    ids=("missing", "tampered"),
)
def test_shadow_day_fails_closed_when_retained_champion_snapshot_is_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "paper"
    parent = _parent_strategy()
    run_date = date(2026, 7, 15)
    monkeypatch.setattr(
        paper_engine,
        "_current_utc_time",
        lambda: datetime(2026, 7, 15, 21, 30, tzinfo=timezone.utc),
    )
    data_truth_root = root.parent / "v2_data_truth"
    champion_manifest = _build_retained_data_truth_snapshot(
        data_truth_root=data_truth_root,
        fixture_root=tmp_path / "champion_source",
        run_date=run_date,
        revision="champion",
        close=100.0,
    )
    latest_manifest = _build_retained_data_truth_snapshot(
        data_truth_root=data_truth_root,
        fixture_root=tmp_path / "revised_source",
        run_date=run_date,
        revision="revised-latest",
        close=101.0,
    )
    assert latest_manifest.snapshot_id != champion_manifest.snapshot_id
    if failure_mode == "missing":
        assert champion_manifest.snapshot_relative_path
        retained_manifest_path = (
            data_truth_root
            / champion_manifest.snapshot_relative_path
            / "manifest.json"
        )
        retained_manifest_path.unlink()
    else:
        assert champion_manifest.normalized_artifact_path
        normalized_path = data_truth_root / champion_manifest.normalized_artifact_path
        normalized_path.write_bytes(normalized_path.read_bytes() + b"tampered\n")

    monkeypatch.setattr(shadow_runner, "build_strategy_catalog", lambda: (parent,))
    _seed_root(root, parent)
    shadow_runner.initialize_shadow_registry(output_root=root)
    _seed_registered_challenger(
        root,
        {
            **_registration_manifest(),
            "frozen_at": (
                f"{(run_date - timedelta(days=1)).isoformat()}T12:00:00+00:00"
            ),
        },
        registered_at=(
            f"{(run_date - timedelta(days=1)).isoformat()}T13:00:00+00:00"
        ),
    )
    _seed_champion_day(
        root,
        run_date,
        snapshot_id=champion_manifest.snapshot_id,
    )
    _write_truth_audits(root)
    registry_path = root / "state" / "strategy_challenger_registry.json"
    event_path = root / "state" / "shadow_registration_ledger.jsonl"
    calendar_path = root / "calendar" / "strategy_daily_returns.csv"
    registry_before = registry_path.read_bytes()
    event_before = event_path.read_bytes()
    calendar_before = calendar_path.read_bytes()

    def fail_if_refetched(**_kwargs: object) -> object:
        raise AssertionError("shadow recovery must never refetch the latest snapshot")

    monkeypatch.setattr(paper_engine, "_load_dataset_for_mode", fail_if_refetched)

    with pytest.raises(
        ValueError,
        match="retained champion DataTruth snapshot is missing or invalid",
    ) as exc_info:
        shadow_runner.run_shadow_day(
            run_date=run_date,
            mode=PaperRunMode.FORWARD,
            output_root=root,
            allow_fetch=True,
        )

    if failure_mode == "missing":
        assert isinstance(exc_info.value.__cause__, FileNotFoundError)
        assert "snapshot manifest is missing" in str(exc_info.value.__cause__)
    else:
        assert isinstance(exc_info.value.__cause__, ValueError)
        assert "normalized artifact hash mismatch" in str(exc_info.value.__cause__)
    assert read_json(data_truth_root / "manifests" / "latest.json", {})[
        "snapshot_id"
    ] == latest_manifest.snapshot_id
    assert registry_path.read_bytes() == registry_before
    assert event_path.read_bytes() == event_before
    assert calendar_path.read_bytes() == calendar_before
    assert not (root / "state" / "shadow" / CHALLENGER_ID).exists()
    assert not (
        root / "manifests" / f"shadow_forward_{run_date}_{CHALLENGER_ID}.json"
    ).exists()
    assert not (
        root / "reports" / "daily" / f"shadow_forward_{run_date}.json"
    ).exists()


def test_candidate_integrity_still_rejects_builder_parent_and_registration_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "paper"
    parent = _parent_strategy()
    monkeypatch.setattr(shadow_runner, "build_strategy_catalog", lambda: (parent,))
    _seed_root(root, parent)
    shadow_runner.initialize_shadow_registry(output_root=root)
    registration = _seed_registered_challenger(
        root,
        _registration_manifest(),
        registered_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
    )
    original_getsource = shadow_runner.inspect.getsource

    def changed_filter_source(target: object) -> str:
        source = original_getsource(target)
        if target is shadow_runner._filter_allows:
            return source + "\n# material filter drift"
        return source

    monkeypatch.setattr(shadow_runner.inspect, "getsource", changed_filter_source)
    implementation_reasons = shadow_runner.verify_registration_integrity(
        registration,
        output_root=root,
    )
    assert "shadow implementation source changed after freeze" in implementation_reasons

    monkeypatch.setattr(shadow_runner.inspect, "getsource", original_getsource)
    changed_parent = replace(parent, parameters={"material_parent_drift": 1})
    monkeypatch.setattr(
        shadow_runner,
        "build_strategy_catalog",
        lambda: (changed_parent,),
    )
    parent_reasons = shadow_runner.verify_registration_integrity(
        registration,
        output_root=root,
    )
    assert "parent strategy logic changed after challenger freeze" in parent_reasons
    assert "champion strategy semantics changed after challenger freeze" in parent_reasons

    monkeypatch.setattr(shadow_runner, "build_strategy_catalog", lambda: (parent,))
    tampered = deepcopy(registration)
    tampered["implementation"]["parameters"]["max_atr_pct"] = 0.5
    tamper_reasons = shadow_runner.verify_registration_integrity(
        tampered,
        output_root=root,
    )
    assert "candidate strategy semantics changed after challenger freeze" in tamper_reasons
    assert "challenger logic artifact hash does not match frozen registration" in (
        tamper_reasons
    )
    assert "challenger registration_id does not match immutable registration" in (
        tamper_reasons
    )
    assert "registry row differs from append-only registration event" in tamper_reasons


def test_shadow_candidate_runs_independent_two_day_paper_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "paper"
    parent = _parent_strategy()
    monkeypatch.setattr(shadow_runner, "build_strategy_catalog", lambda: (parent,))
    day_one = date.today() + timedelta(days=1)
    day_two = day_one + timedelta(days=1)
    datasets = {
        day_one: _dataset(day_one),
        day_two: _dataset(day_one, day_two),
    }
    _seed_root(root, parent)
    manifest_path = root / "candidate_registration.json"
    write_json(manifest_path, _registration_manifest())

    class RegistrationDayDateTime(datetime):
        """Freeze public registration before the first forward evidence day."""

        @classmethod
        def now(cls, tz: timezone | None = None) -> datetime:
            assert tz is timezone.utc
            return datetime.combine(
                day_one - timedelta(days=1),
                datetime.min.time(),
                tzinfo=timezone.utc,
            )

    monkeypatch.setattr(shadow_runner, "datetime", RegistrationDayDateTime)
    registered = shadow_runner.register_shadow_challenger(
        manifest_path=manifest_path,
        output_root=root,
    )
    registration = registered["challenger"]
    assert isinstance(registration, dict)

    def fake_loader(**kwargs: object) -> tuple[MarketDataset, DataTruthManifest, tuple[str, ...]]:
        run_date = kwargs["run_date"]
        assert isinstance(run_date, date)
        snapshot = f"sourced-shadow-{run_date.isoformat()}"
        return datasets[run_date], _manifest(run_date, snapshot), ()

    monkeypatch.setattr(shadow_runner, "_load_retained_champion_snapshot", fake_loader)
    _seed_champion_day(root, day_one)
    _write_truth_audits(root)

    first = shadow_runner.run_shadow_day(
        run_date=day_one,
        mode=PaperRunMode.FORWARD,
        output_root=root,
        allow_fetch=False,
    )

    assert first["status"] == "passed"
    assert first["results"][0]["orders_created"] == 1
    assert first["results"][0]["fills"] == 0
    champion_decisions = read_json(
        root / "exports" / f"strategy_decisions_forward_{day_one}.json", []
    )
    assert champion_decisions == [{"sentinel": "champion-only"}]
    shadow_decisions = read_json(
        root
        / "exports"
        / f"shadow_strategy_decisions_forward_{day_one}_{CHALLENGER_ID}.json",
        [],
    )
    assert isinstance(shadow_decisions, list) and len(shadow_decisions) == 1
    assert shadow_decisions[0]["strategy_version"] == "v2.0"
    assert shadow_decisions[0]["challenger_id"] == CHALLENGER_ID

    _seed_champion_day(root, day_two)
    _write_truth_audits(root)
    second = shadow_runner.run_shadow_day(
        run_date=day_two,
        mode=PaperRunMode.FORWARD,
        output_root=root,
        allow_fetch=False,
    )

    assert second["results"][0]["fills"] == 1
    assert second["results"][0]["closes"] == 1
    events = read_jsonl(root / "ledger" / "paper_ledger.jsonl")
    candidate_events = [
        event
        for event in events
        if isinstance(event.get("payload"), dict)
        and event["payload"].get("strategy_version") == "v2.0"
    ]
    assert any(event["event_type"] == "paper_fill" for event in candidate_events)
    assert any(
        event["event_type"] == "paper_position_closed" for event in candidate_events
    )
    assert all(
        event["payload"]["execution_policy_version"]
        == PAPER_EXECUTION_POLICY_VERSION
        for event in candidate_events
    )
    account_payload = read_json(
        root / "state" / "shadow" / CHALLENGER_ID / "forward_account.json", {}
    )
    assert isinstance(account_payload, dict)
    assert account_payload["account"]["realized_pnl"] > 0
    champion_account = _account_row(root, "v1.0")
    candidate_account = _account_row(root, "v2.0")
    assert champion_account["current_equity"] == 100_000.0
    assert candidate_account["current_equity"] > 100_000.0
    assert candidate_account["strategy_semantics_fingerprint"] == registration[
        "candidate_strategy_semantics_fingerprint"
    ]
    candidate_calendar = next(
        row
        for row in paper_engine._read_calendar_rows(
            paper_engine.PaperOpsPaths.create(root)
        )
        if row.get("strategy_version") == "v2.0"
        and row.get("date") == day_two.isoformat()
    )
    assert candidate_calendar["strategy_semantics_fingerprint"] == registration[
        "candidate_strategy_semantics_fingerprint"
    ]
    assert not (root / "state" / "paper_transaction_pending.json").exists()

    event_count = len(events)
    _write_truth_audits(root)
    repeated = shadow_runner.run_shadow_day(
        run_date=day_two,
        mode=PaperRunMode.FORWARD,
        output_root=root,
        allow_fetch=False,
    )
    repeated_events = read_jsonl(root / "ledger" / "paper_ledger.jsonl")
    assert repeated["results"][0]["fills"] == 1
    assert len(repeated_events) == event_count
    assert len({str(event["event_id"]) for event in repeated_events}) == event_count

    decisions_path = (
        root
        / "exports"
        / f"shadow_strategy_decisions_forward_{day_two}_{CHALLENGER_ID}.json"
    )
    exact_decisions = read_json(decisions_path, [])
    assert isinstance(exact_decisions, list)
    write_json(decisions_path, [*exact_decisions, {"symbol": "CONTAMINATION"}])
    with pytest.raises(ValueError, match="decision (artifact|coverage)"):
        shadow_runner.run_shadow_day(
            run_date=day_two,
            mode=PaperRunMode.FORWARD,
            output_root=root,
            allow_fetch=False,
        )

    write_json(decisions_path, exact_decisions)
    shadow_account_path = (
        root / "state" / "shadow" / CHALLENGER_ID / "forward_account.json"
    )
    tampered_account = read_json(shadow_account_path, {})
    assert isinstance(tampered_account, dict)
    tampered_account["account"]["current_equity"] += 1_000.0
    write_json(shadow_account_path, tampered_account)
    with pytest.raises(ValueError, match="account state is inconsistent"):
        shadow_runner.run_shadow_day(
            run_date=day_two,
            mode=PaperRunMode.FORWARD,
            output_root=root,
            allow_fetch=False,
        )


def test_registration_event_recovers_exact_orphan_and_rejects_changed_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "paper"
    parent = _parent_strategy()
    monkeypatch.setattr(shadow_runner, "build_strategy_catalog", lambda: (parent,))
    _seed_root(root, parent)
    shadow_runner.initialize_shadow_registry(output_root=root)
    original = _registration_manifest()
    orphan = shadow_runner._freeze_registration(
        original,
        root,
        registered_at=datetime.now(timezone.utc).isoformat(),
    )
    append_jsonl_unique(
        root / "state" / "shadow_registration_ledger.jsonl",
        [
            {
                "schema_version": shadow_runner.REGISTRATION_EVENT_SCHEMA,
                "event_type": "shadow_challenger_registered",
                "registration_event_id": orphan["registration_id"],
                "registered_at": orphan["registered_at"],
                "registration": orphan,
            }
        ],
        "registration_event_id",
    )
    changed = {
        **original,
        "hypothesis": "Changed after the append-only registration event.",
    }
    changed_path = root / "changed.json"
    write_json(changed_path, changed)

    with pytest.raises(ValueError, match="freezes different semantics"):
        shadow_runner.register_shadow_challenger(
            manifest_path=changed_path,
            output_root=root,
        )

    original_path = root / "original.json"
    write_json(original_path, original)
    recovered = shadow_runner.register_shadow_challenger(
        manifest_path=original_path,
        output_root=root,
    )
    assert recovered["status"] == "recovered_registration"
    assert recovered["challenger"] == orphan
    assert shadow_runner.verify_registration_integrity(
        orphan,
        output_root=root,
    ) == ()

    registry = read_json(root / "state" / "strategy_challenger_registry.json", {})
    assert isinstance(registry, dict)
    registry["challengers"][0]["hypothesis"] = "tampered"
    write_json(root / "state" / "strategy_challenger_registry.json", registry)
    with pytest.raises(ValueError, match="registry integrity failed"):
        shadow_runner.run_shadow_day(
            run_date=date.today() + timedelta(days=1),
            output_root=root,
        )


def test_shadow_day_preserves_not_yet_eligible_challenger_as_na_without_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "paper"
    parent = _parent_strategy()
    monkeypatch.setattr(shadow_runner, "build_strategy_catalog", lambda: (parent,))
    run_date = date.today()
    _seed_root(root, parent)
    shadow_runner.initialize_shadow_registry(output_root=root)
    raw = {
        **_registration_manifest(),
        "frozen_at": f"{run_date.isoformat()}T12:00:00+00:00",
    }
    registration = _seed_registered_challenger(
        root,
        raw,
        registered_at=f"{run_date.isoformat()}T13:00:00+00:00",
        candidate_semantics_contract=(
            shadow_runner.LEGACY_CANDIDATE_SEMANTICS_CONTRACT
        ),
    )
    _seed_champion_day(root, run_date)
    _write_truth_audits(root)
    ledger_before = read_jsonl(root / "ledger" / "paper_ledger.jsonl")

    def fail_if_loaded(**_kwargs: object) -> object:
        raise AssertionError("an ineligible challenger must not load execution data")

    monkeypatch.setattr(
        shadow_runner,
        "_load_retained_champion_snapshot",
        fail_if_loaded,
    )
    result = shadow_runner.run_shadow_day(
        run_date=run_date,
        mode=PaperRunMode.FORWARD,
        output_root=root,
        allow_fetch=False,
    )

    assert result["status"] == "skipped_no_eligible_challengers"
    assert result["challenger_count"] == 0
    assert result["registered_challenger_count"] == 1
    assert result["eligible_challenger_count"] == 0
    assert result["skipped_challenger_count"] == 1
    assert result["results"] == []
    skipped = result["skipped_challengers"][0]
    assert skipped["challenger_id"] == CHALLENGER_ID
    assert skipped["status"] == "skipped_not_yet_eligible"
    assert skipped["evidence_created"] is False
    assert skipped["return_status"] == "not_applicable_no_evidence"
    assert skipped["daily_return_pct"] is None
    assert skipped["after_cost_return_pct"] is None
    earliest_eligible = (run_date + timedelta(days=1)).isoformat()
    assert skipped["earliest_eligible_calendar_date"] == earliest_eligible
    assert skipped["first_eligible_run_date"] == earliest_eligible
    assert skipped["research_only"] is True
    assert skipped["automatic_promotion_enabled"] is False
    assert skipped["broker_execution_allowed"] is False
    assert read_jsonl(root / "ledger" / "paper_ledger.jsonl") == ledger_before
    assert not (root / "state" / "shadow" / CHALLENGER_ID).exists()
    assert not (
        root / "manifests" / f"shadow_forward_{run_date}_{CHALLENGER_ID}.json"
    ).exists()
    assert not (
        root
        / "exports"
        / f"shadow_strategy_decisions_forward_{run_date}_{CHALLENGER_ID}.json"
    ).exists()
    assert all(
        row.get("strategy_version") != registration["candidate_strategy_version"]
        for row in paper_engine._read_calendar_rows(
            paper_engine.PaperOpsPaths.create(root)
        )
    )
    assert read_json(
        root / "reports" / "daily" / f"shadow_forward_{run_date}.json", {}
    ) == result


def test_shadow_day_runs_eligible_challengers_while_preserving_ineligible_as_na(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "paper"
    parent = _parent_strategy()
    monkeypatch.setattr(shadow_runner, "build_strategy_catalog", lambda: (parent,))
    run_date = date.today() + timedelta(days=2)
    _seed_root(root, parent)
    shadow_runner.initialize_shadow_registry(output_root=root)
    eligible = _seed_registered_challenger(
        root,
        {
            **_registration_manifest(),
            "frozen_at": f"{(run_date - timedelta(days=2)).isoformat()}T00:00:00+00:00",
        },
        registered_at=(
            f"{(run_date - timedelta(days=1)).isoformat()}T00:00:00+00:00"
        ),
    )
    ineligible_id = "fixture_shadow_parent_candidate_v3"
    ineligible = _seed_registered_challenger(
        root,
        {
            **_registration_manifest(),
            "challenger_id": ineligible_id,
            "candidate_strategy_version": "v3.0",
            "frozen_at": f"{run_date.isoformat()}T12:00:00+00:00",
        },
        registered_at=f"{run_date.isoformat()}T13:00:00+00:00",
    )
    _seed_champion_day(root, run_date)
    _write_truth_audits(root)

    def fake_loader(**_kwargs: object) -> tuple[MarketDataset, DataTruthManifest, tuple[str, ...]]:
        snapshot = f"sourced-shadow-{run_date.isoformat()}"
        return _dataset(run_date), _manifest(run_date, snapshot), ()

    monkeypatch.setattr(shadow_runner, "_load_retained_champion_snapshot", fake_loader)
    result = shadow_runner.run_shadow_day(
        run_date=run_date,
        mode=PaperRunMode.FORWARD,
        output_root=root,
        allow_fetch=False,
    )

    assert result["status"] == "passed_with_ineligible_challengers"
    assert result["registered_challenger_count"] == 2
    assert result["eligible_challenger_count"] == 1
    assert result["skipped_challenger_count"] == 1
    assert result["results"][0]["challenger_id"] == eligible["challenger_id"]
    assert result["results"][0]["orders_created"] == 1
    assert result["skipped_challengers"][0]["challenger_id"] == ineligible_id
    assert result["skipped_challengers"][0]["daily_return_pct"] is None
    assert not (root / "state" / "shadow" / ineligible_id).exists()
    assert not (
        root / "manifests" / f"shadow_forward_{run_date}_{ineligible_id}.json"
    ).exists()
    assert not (
        root
        / "exports"
        / f"shadow_strategy_decisions_forward_{run_date}_{ineligible_id}.json"
    ).exists()
    calendar_rows = paper_engine._read_calendar_rows(
        paper_engine.PaperOpsPaths.create(root)
    )
    assert any(
        row.get("strategy_version") == eligible["candidate_strategy_version"]
        for row in calendar_rows
    )
    assert all(
        row.get("strategy_version") != ineligible["candidate_strategy_version"]
        for row in calendar_rows
    )


def _seed_root(root: Path, parent: StrategySpec) -> None:
    paths = paper_engine.PaperOpsPaths.create(root)
    parent_semantics = paper_engine._strategy_semantics_fingerprint(parent)
    write_json(
        paths.state / "paper_ops_config.json",
        {
            "starting_equity": 100_000.0,
            "risk_per_trade_pct": 0.005,
            "max_daily_loss_pct": 0.015,
            "max_open_risk_pct": 0.02,
            "max_gross_exposure_pct": 1.0,
            "max_concurrent_positions": 3,
            "allow_experimental": True,
            "allow_single_provider_forward": True,
            "min_reward_risk": 1.0,
            "fee_bps": 1.0,
            "slippage_bps": 5.0,
            "execution_policy_version": PAPER_EXECUTION_POLICY_VERSION,
            "universe_symbols": ["TST"],
        },
    )
    write_json(
        paths.state / "strategy_registry.json",
        [
            {
                "strategy_id": parent.strategy_id,
                "strategy_version": parent.version,
                "strategy_status": parent.status,
                "execution_policy_version": PAPER_EXECUTION_POLICY_VERSION,
                "strategy_semantics_fingerprint": parent_semantics,
            }
        ],
    )
    write_json(
        paths.state / "strategy_semantics_manifest.json",
        {
            "schema_version": "v2.strategy_semantics_manifest.v1",
            "strategies": {
                f"{parent.strategy_id}@{parent.version}": {
                    "activation_policy": "next_market_session_after_registration",
                    "configuration": {},
                    "coverage_inception_date": "2026-07-02",
                    "fingerprint": parent_semantics,
                    "registered_at": "2026-07-01T18:00:00+00:00",
                }
            },
        },
    )
    write_json(
        paths.state / "execution_policy_manifest.json",
        {
            "active_execution_policy_version": PAPER_EXECUTION_POLICY_VERSION,
            "policies": {
                PAPER_EXECUTION_POLICY_VERSION: {
                    "activation_policy": "next_market_session_after_registration",
                    "configuration": {},
                    "coverage_inception_date": "2026-07-02",
                    "fingerprint": "a" * 64,
                    "registered_at": "2026-07-01T18:00:00+00:00",
                }
            },
            "schema_version": "v2.paper_execution_policy_manifest.v1",
        },
    )
    champion_account = StrategyPaperAccount(
        strategy_id=parent.strategy_id,
        strategy_version=parent.version,
        starting_equity=100_000.0,
        current_equity=100_000.0,
        realized_pnl=0.0,
        unrealized_pnl=0.0,
        execution_policy_version=PAPER_EXECUTION_POLICY_VERSION,
        strategy_semantics_fingerprint=parent_semantics,
    )
    write_json(
        paper_engine._paper_accounts_path(paths, PaperRunMode.FORWARD),
        {
            "schema_version": "v2.paper_account_state.v2",
            "accounts": [champion_account.to_dict()],
        },
    )
    (paths.ledger / "paper_ledger.jsonl").write_text("", encoding="utf-8")


def _registration_manifest() -> dict[str, object]:
    return {
        "schema_version": shadow_runner.SHADOW_MANIFEST_SCHEMA,
        "challenger_id": CHALLENGER_ID,
        "strategy_id": STRATEGY_ID,
        "champion_strategy_version": "v1.0",
        "candidate_strategy_version": "v2.0",
        "execution_policy_version": PAPER_EXECUTION_POLICY_VERSION,
        "status": "shadow",
        "frozen_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        "hypothesis": "A frozen filter can improve after-cost outcomes.",
        "implementation": {
            "kind": shadow_runner.IMPLEMENTATION_KIND,
            "parameters": {
                "trend_sma_period": 1,
                "atr_period": 1,
                "max_atr_pct": 1.0,
                "min_parent_score": 0.0,
            },
        },
    }


def _seed_registered_challenger(
    root: Path,
    raw: dict[str, object],
    *,
    registered_at: str,
    candidate_semantics_contract: str = (
        shadow_runner.SCOPED_CANDIDATE_SEMANTICS_CONTRACT
    ),
) -> dict[str, object]:
    registration = shadow_runner._freeze_registration(
        raw,
        root,
        registered_at=registered_at,
        candidate_semantics_contract=candidate_semantics_contract,
    )
    append_jsonl_unique(
        root / "state" / "shadow_registration_ledger.jsonl",
        [
            {
                "schema_version": shadow_runner.REGISTRATION_EVENT_SCHEMA,
                "event_type": "shadow_challenger_registered",
                "registration_event_id": registration["registration_id"],
                "registered_at": registration["registered_at"],
                "registration": registration,
            }
        ],
        "registration_event_id",
    )
    registry = read_json(root / "state" / "strategy_challenger_registry.json", {})
    assert isinstance(registry, dict)
    challengers = registry.get("challengers")
    assert isinstance(challengers, list)
    challengers.append(registration)
    write_json(root / "state" / "strategy_challenger_registry.json", registry)
    return registration


def _simulate_unrelated_runner_module_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_getsource = shadow_runner.inspect.getsource

    def drifted_getsource(target: object) -> str:
        source = original_getsource(target)
        if target is shadow_runner:
            return source + "\n# unrelated scheduler-only runner change"
        return source

    monkeypatch.setattr(shadow_runner.inspect, "getsource", drifted_getsource)


def _dataset(day_one: date, day_two: date | None = None) -> MarketDataset:
    bars = [
        _bar(day_one, open_price=100.0, high=100.5, low=99.5, close=100.0),
    ]
    if day_two is not None:
        bars.append(
            _bar(day_two, open_price=100.0, high=103.0, low=99.5, close=102.5)
        )
    return MarketDataset(
        dataset_id=f"shadow-{bars[-1].timestamp.date()}",
        source_kind="sourced_fixture",
        timeframe="1d",
        bars_by_symbol={"TST": tuple(bars)},
    )


def _bar(
    run_date: date,
    *,
    open_price: float,
    high: float,
    low: float,
    close: float,
) -> MarketBar:
    return MarketBar(
        symbol="TST",
        timestamp=datetime.combine(run_date, time(21, 0), tzinfo=timezone.utc),
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=1_000_000,
    )


def _manifest(run_date: date, snapshot: str) -> DataTruthManifest:
    return DataTruthManifest(
        snapshot_id=snapshot,
        created_at=datetime.now(timezone.utc).isoformat(),
        provider_id="sourced_fixture",
        provider_name="Sourced Fixture",
        symbols=("TST",),
        timeframe="1d",
        requested_start=run_date.isoformat(),
        requested_end=run_date.isoformat(),
        accepted_start=run_date.isoformat(),
        accepted_end=run_date.isoformat(),
        bar_count=1,
        accepted_bar_count=1,
        rejected_bar_count=0,
        skipped_incomplete_bars=0,
        validation_status="passed",
        warnings=(),
        raw_artifact_hashes={},
        normalized_artifact_hash="fixture",
        source_url_or_reference=("fixture://shadow",),
    )


def _seed_champion_day(
    root: Path,
    run_date: date,
    *,
    snapshot_id: str | None = None,
) -> None:
    paths = paper_engine.PaperOpsPaths.create(root)
    snapshot = snapshot_id or f"sourced-shadow-{run_date.isoformat()}"
    run = paper_engine._paper_run(
        run_date=run_date,
        mode=PaperRunMode.FORWARD,
        data_snapshot_id=snapshot,
    )
    config = paper_engine._config(paths)
    run_manifest = paper_engine.PaperOpsManifest(
        run_id=run.run_id,
        mode=run.mode,
        run_date=run.run_date,
        data_snapshot_id=run.data_snapshot_id,
        output_artifacts=(),
        warnings=(),
        execution_policy_version=config.execution_policy_version,
        execution_policy_fingerprint="a" * 64,
        universe_id=config.universe_id,
        universe_symbols=config.universe_symbols,
    ).to_dict()
    unhashed = dict(run_manifest)
    unhashed.pop("manifest_payload_hash", None)
    run_manifest["manifest_payload_hash"] = hashlib.sha256(
        json.dumps(unhashed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    write_json(
        paths.manifests / f"{paper_engine._safe_filename(run.run_id)}.json",
        run_manifest,
    )
    write_json(
        root / "reports" / "daily" / f"forward_{run_date}.json",
        {
            "date": run_date.isoformat(),
            "mode": "forward",
            "run_id": run.run_id,
            "data_snapshot_id": snapshot,
            "stats": {"phase": "close"},
        },
    )
    write_json(
        root / "exports" / f"preflight_forward_{run_date}.json",
        {
            "status": "passed",
            "mode": "forward",
            "run_date": run_date.isoformat(),
            "latest_completed_date": run_date.isoformat(),
            "run_id": run.run_id,
            "data_snapshot_id": snapshot,
            "universe_status": "complete",
            "symbols": ["TST"],
        },
    )
    write_json(
        root / "exports" / f"strategy_decisions_forward_{run_date}.json",
        [{"sentinel": "champion-only"}],
    )
    row = StrategyCalendarRow(
        date=run_date.isoformat(),
        mode=PaperRunMode.FORWARD,
        strategy_id=STRATEGY_ID,
        strategy_version="v1.0",
        strategy_status="experimental",
        data_snapshot_id=snapshot,
        starting_equity=100_000.0,
        ending_equity=100_000.0,
        realized_pnl=0.0,
        unrealized_pnl=0.0,
        total_pnl=0.0,
        daily_return_pct=0.0,
        cumulative_return_pct=0.0,
        drawdown_pct=0.0,
        trades_opened=0,
        trades_closed=0,
        pending_orders=0,
        open_positions=0,
        wins=0,
        losses=0,
        flats=0,
        average_r=0.0,
        expectancy_r=0.0,
        exposure_pct=0.0,
        fees_paid=0.0,
        slippage_estimate=0.0,
        warnings=(),
        run_id=run.run_id,
        execution_policy_version=PAPER_EXECUTION_POLICY_VERSION,
        strategy_semantics_fingerprint=paper_engine._strategy_semantics_fingerprint(
            _parent_strategy()
        ),
    )
    upsert_rows(
        root / "calendar" / "strategy_daily_returns.csv",
        [row.to_dict()],
        (
            "date",
            "mode",
            "strategy_id",
            "strategy_version",
            "execution_policy_version",
        ),
        paper_engine.CALENDAR_FIELDNAMES,
    )


def _build_retained_data_truth_snapshot(
    *,
    data_truth_root: Path,
    fixture_root: Path,
    run_date: date,
    revision: str,
    close: float,
) -> DataTruthManifest:
    fixture_root.mkdir(parents=True)
    raw_dir = fixture_root / "raw"
    raw_dir.mkdir()
    (raw_dir / "tst_chart.json").write_text(
        f'{{"revision":"{revision}"}}',
        encoding="utf-8",
    )
    source_csv = fixture_root / "ohlcv.csv"
    write_ohlcv_csv(
        MarketDataset(
            dataset_id=f"fixture-{revision}",
            source_kind="sourced_fixture",
            timeframe="1d",
            bars_by_symbol={
                "TST": (
                    _bar(
                        run_date,
                        open_price=100.0,
                        high=max(101.0, close + 1.0),
                        low=99.0,
                        close=close,
                    ),
                )
            },
        ),
        source_csv,
    )
    result = build_data_truth_snapshot(
        as_of_date=run_date + timedelta(days=1),
        output_root=data_truth_root,
        created_at=datetime.combine(
            run_date + timedelta(days=1),
            time(22, 0),
            tzinfo=timezone.utc,
        ),
        source_csv=source_csv,
        raw_dir=raw_dir,
        allow_fetch=False,
        symbols=("TST",),
    )
    assert result.manifest.accepted_end == run_date.isoformat()
    return result.manifest


def _write_truth_audits(root: Path) -> None:
    for name in (
        "reconciliation_latest.json",
        "calendar_truth_latest.json",
        "ledger_rebuild_latest.json",
    ):
        write_json(root / "reconciliation" / name, {"status": "passed"})


def _account_row(root: Path, version: str) -> dict[str, object]:
    payload = read_json(root / "state" / "paper_accounts.json", {})
    assert isinstance(payload, dict)
    rows = payload.get("accounts")
    assert isinstance(rows, list)
    return next(
        row
        for row in rows
        if isinstance(row, dict) and row.get("strategy_version") == version
    )
