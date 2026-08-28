import csv
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from intraday_scanner.services.luna_core_universe_service import _canonical_member_hash
from intraday_scanner.v2.data import MarketBar, MarketDataset
from intraday_scanner.v2.data_truth.models import DataTruthManifest
from intraday_scanner.v2.paper_ops import engine as paper_ops_engine
from intraday_scanner.v2.paper_ops.lifecycle_backtest import _signal_cards
from intraday_scanner.v2.paper_ops.models import PaperRun, PaperRunMode
from intraday_scanner.v2.paper_ops.observer_safety import _is_complete_manifest
from intraday_scanner.v2.paper_ops.universe_handoff import (
    UniverseHandoffError,
    _expected_strategy_ids,
    load_universe_handoff,
)
from intraday_scanner.v2.paper_ops.universe_handoff import (
    build_universe_handoff as _build_universe_handoff,
)
from intraday_scanner.v2.strategies import Direction, StrategySignal

MARKET_DATE = "2026-08-28"


def build_universe_handoff(
    root: Path, market_date: str, *, output_path: Path | None = None
) -> dict[str, object]:
    """Use the explicit fixture trust injection for synthetic Morning inputs."""

    return _build_universe_handoff(
        root,
        market_date,
        output_path=output_path,
        allow_test_override=True,
    )


def _core_contract() -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "dawnstrike.luna.core_universe.v1",
        "requested_market_date": MARKET_DATE,
        "observed_at": f"{MARKET_DATE}T12:00:00+00:00",
        "status": "READY",
        "completeness_verdict": "COMPLETE",
        "freshness_verdict": "FRESH",
        "contract_id": "luna-core-fixture",
        "content_hash_sha256": "",
        "content_hash": "",
        "universe_id": "luna-core-fixture",
        "membership_count": 1,
        "canonical_member_set_hash_sha256": "",
        "members": [
            {
                "symbol": "AAA",
                "index_memberships": ["Nasdaq-100", "S&P 500"],
                "sources": ["fixture-core"],
                "valid_from": MARKET_DATE,
            }
        ],
    }
    payload["canonical_member_set_hash_sha256"] = _canonical_member_hash(
        [
            {
                "symbol": "AAA",
                "provider_symbol": "AAA",
                "asset_class": "common_stock",
                "index": "S&P 500",
                "valid_from": MARKET_DATE,
                "valid_to": None,
            }
        ]
        + [
            {
                "symbol": "AAA",
                "provider_symbol": "AAA",
                "asset_class": "common_stock",
                "index": "Nasdaq-100",
                "valid_from": MARKET_DATE,
                "valid_to": None,
            }
        ]
    )
    payload["source_ids"] = ["fixture-spy", "fixture-ndx"]
    payload["source_uris"] = ["https://example.test/spy", "https://example.test/ndx"]
    payload["source_artifacts"] = [
        {
            "source_id": "fixture-spy",
            "source_uri": "https://example.test/spy",
            "raw_artifact_hashes": ["a" * 64],
            "canonical_member_set_hash_sha256": _canonical_member_hash(
                [
                    {
                        "symbol": "AAA",
                        "provider_symbol": "AAA",
                        "asset_class": "common_stock",
                        "index": "S&P 500",
                        "valid_from": MARKET_DATE,
                        "valid_to": None,
                    }
                ]
            ),
            "source_binding": {
                "status": "VERIFIED",
                "authority": "fixture",
                "index": "S&P 500",
                "transformation_id": "fixture-v1",
                "source_scope": "fixture S&P 500",
                "derived_member_set_hash_sha256": _canonical_member_hash(
                    [
                        {
                            "symbol": "AAA",
                            "provider_symbol": "AAA",
                            "asset_class": "common_stock",
                            "index": "S&P 500",
                            "valid_from": MARKET_DATE,
                            "valid_to": None,
                        }
                    ]
                ),
                "derived_membership_count": 1,
            },
        },
        {
            "source_id": "fixture-ndx",
            "source_uri": "https://example.test/ndx",
            "raw_artifact_hashes": ["b" * 64],
            "canonical_member_set_hash_sha256": _canonical_member_hash(
                [
                    {
                        "symbol": "AAA",
                        "provider_symbol": "AAA",
                        "asset_class": "common_stock",
                        "index": "Nasdaq-100",
                        "valid_from": MARKET_DATE,
                        "valid_to": None,
                    }
                ]
            ),
            "source_binding": {
                "status": "VERIFIED",
                "authority": "fixture",
                "index": "Nasdaq-100",
                "transformation_id": "fixture-v1",
                "source_scope": "fixture Nasdaq-100",
                "derived_member_set_hash_sha256": _canonical_member_hash(
                    [
                        {
                            "symbol": "AAA",
                            "provider_symbol": "AAA",
                            "asset_class": "common_stock",
                            "index": "Nasdaq-100",
                            "valid_from": MARKET_DATE,
                            "valid_to": None,
                        }
                    ]
                ),
                "derived_membership_count": 1,
            },
        },
    ]
    payload["index_verdicts"] = {
        "S&P 500": {
            "status": "READY",
            "expected_count": 1,
            "observed_unique_count": 1,
            "count_verdict": "PASS",
            "freshness_verdict": "FRESH",
            "effective_date_verdict": "PASS",
            "completeness_verdict": "COMPLETE",
        },
        "Nasdaq-100": {
            "status": "READY",
            "expected_count": 1,
            "observed_unique_count": 1,
            "count_verdict": "PASS",
            "freshness_verdict": "FRESH",
            "effective_date_verdict": "PASS",
            "completeness_verdict": "COMPLETE",
        },
    }
    unhashed = dict(payload)
    for key in ("content_hash_sha256", "content_hash", "contract_id", "universe_id"):
        unhashed.pop(key, None)
    digest = hashlib.sha256(
        json.dumps(unhashed, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    payload["content_hash_sha256"] = digest
    payload["content_hash"] = digest
    return payload


def _morning_root(tmp_path: Path, *, source_status: str = "success") -> Path:
    root = tmp_path / "morning"
    (root / "web_collect").mkdir(parents=True)
    core = _core_contract()
    (root / "core_universe_contract.json").write_text(
        json.dumps(core, sort_keys=True), encoding="utf-8"
    )
    source_path = root / "web_collect" / "premarket_snapshot.csv"
    with source_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ticker", "market_date", "source"])
        writer.writeheader()
        if source_status == "success":
            writer.writerow({"ticker": "AAA", "market_date": MARKET_DATE, "source": "mover"})
            writer.writerow({"ticker": "BBB", "market_date": MARKET_DATE, "source": "mover"})
    source_failed = source_status != "success"
    source = {
        "status": source_status,
        "run_id": "mover-run",
        "candidate_count": 2 if source_status == "success" else 0,
        "sources_attempted": 1,
        "sources_succeeded": 0 if source_failed else 1,
        "source_failures": 1 if source_failed else 0,
        "attempts": [
            {
                "source": "fixture_mover",
                "status": source_status,
                "failure_reason": "fixture source unavailable" if source_failed else "",
            }
        ],
        "snapshot_path": str(source_path),
        "requested_observed_at": f"{MARKET_DATE}T12:00:00+00:00",
    }
    (root / "web_collect" / "source_summary.json").write_text(
        json.dumps(source, sort_keys=True), encoding="utf-8"
    )
    contract = {
        "schema_version": "alphaops.run_contract.v1",
        "producer": "alphaops",
        "producer_run_id": "scan-fixture",
        "market_date": MARKET_DATE,
        "generated_at": f"{MARKET_DATE}T12:00:00+00:00",
        "source_status": source_status,
        "code_sha": "a" * 40,
    }
    (root / "alpha_run_contract.json").write_text(
        json.dumps(contract, sort_keys=True), encoding="utf-8"
    )
    cycle = {
        "scan_id": "scan-fixture",
        "generated_at": f"{MARKET_DATE}T12:00:00+00:00",
        "code_sha": "a" * 40,
        "source_summary": {
            **source,
            "code_sha": "a" * 40,
            "morning_strategy_adapter": {
                "enabled_strategy_ids": list(_expected_strategy_ids()),
            },
        },
    }
    (root / "alpha_cycle.json").write_text(json.dumps(cycle, sort_keys=True), encoding="utf-8")
    return root


def test_handoff_exactly_deduplicates_core_and_mover_union(tmp_path: Path) -> None:
    root = _morning_root(tmp_path)
    handoff_path = root / "paperops_universe_handoff.json"

    payload = build_universe_handoff(root, MARKET_DATE, output_path=handoff_path)

    assert payload["universe_symbols"] == ["AAA", "BBB"]
    assert payload["code_sha"] == "a" * 40
    aaa = next(row for row in payload["members"] if row["symbol"] == "AAA")
    assert aaa["lanes"] == ["core", "mover"]
    assert payload["coverage"]["status"] == "COMPLETE"
    assert (
        load_universe_handoff(handoff_path, market_date=MARKET_DATE)["handoff_id"]
        == payload["handoff_id"]
    )


def test_handoff_partial_provider_truth_is_explicit(tmp_path: Path) -> None:
    root = _morning_root(tmp_path, source_status="no_data")
    payload = build_universe_handoff(root, MARKET_DATE)

    assert payload["universe_symbols"] == ["AAA"]
    assert payload["coverage"]["status"] == "PARTIAL"
    assert "governed_mover_source_unavailable" in payload["coverage"]["shortfall_reasons"]


def test_handoff_failed_mover_lane_keeps_verified_core_subset(tmp_path: Path) -> None:
    root = _morning_root(tmp_path, source_status="failed")

    payload = build_universe_handoff(root, MARKET_DATE)

    assert payload["universe_symbols"] == ["AAA"]
    assert payload["coverage"]["status"] == "PARTIAL"
    assert "governed_mover_source_unavailable" in payload["coverage"]["shortfall_reasons"]


def test_handoff_keeps_mover_subset_when_core_membership_is_unavailable(tmp_path: Path) -> None:
    root = _morning_root(tmp_path)
    core_path = root / "core_universe_contract.json"
    core = json.loads(core_path.read_text(encoding="utf-8"))
    core["status"] = "DATA_UNAVAILABLE"
    core["members"] = []
    core["membership_count"] = 0
    core.pop("canonical_member_set_hash_sha256", None)
    core["canonical_member_set_hash_sha256"] = _canonical_member_hash([])
    for verdict in core["index_verdicts"].values():
        verdict.update(
            {
                "status": "DATA_UNAVAILABLE",
                "observed_unique_count": 0,
                "count_verdict": "FAIL",
                "effective_date_verdict": "UNKNOWN",
                "freshness_verdict": "UNKNOWN",
                "completeness_verdict": "INCOMPLETE",
            }
        )
    unhashed = dict(core)
    for key in ("content_hash_sha256", "content_hash", "contract_id", "universe_id"):
        unhashed.pop(key, None)
    digest = hashlib.sha256(
        json.dumps(unhashed, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    core["content_hash_sha256"] = digest
    core["content_hash"] = digest
    core_path.write_text(json.dumps(core, sort_keys=True), encoding="utf-8")

    payload = build_universe_handoff(root, MARKET_DATE)

    assert payload["universe_symbols"] == ["AAA", "BBB"]
    assert "core_membership_unavailable" in payload["coverage"]["shortfall_reasons"]


def test_one_lane_self_hashed_core_cannot_claim_complete(tmp_path: Path) -> None:
    root = _morning_root(tmp_path)
    core_path = root / "core_universe_contract.json"
    core = json.loads(core_path.read_text(encoding="utf-8"))
    core["members"][0]["index_memberships"] = ["S&P 500"]
    core["canonical_member_set_hash_sha256"] = _canonical_member_hash(
        [
            {
                "symbol": "AAA",
                "provider_symbol": "AAA",
                "asset_class": "common_stock",
                "index": "S&P 500",
                "valid_from": MARKET_DATE,
                "valid_to": None,
            }
        ]
    )
    core["index_verdicts"].pop("Nasdaq-100")
    unhashed = {
        key: value
        for key, value in core.items()
        if key not in {"content_hash_sha256", "content_hash", "contract_id", "universe_id"}
    }
    digest = hashlib.sha256(
        json.dumps(unhashed, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    core["content_hash_sha256"] = digest
    core["content_hash"] = digest
    core_path.write_text(json.dumps(core, sort_keys=True), encoding="utf-8")

    with pytest.raises(UniverseHandoffError, match="index verdicts are incomplete"):
        build_universe_handoff(root, MARKET_DATE)


def test_undated_mover_rows_cannot_become_complete(tmp_path: Path) -> None:
    root = _morning_root(tmp_path)
    snapshot_path = root / "web_collect" / "premarket_snapshot.csv"
    snapshot_path.write_text("ticker,source\nAAA,mover\n", encoding="utf-8")

    with pytest.raises(UniverseHandoffError, match="row date is missing"):
        build_universe_handoff(root, MARKET_DATE)


def test_invalid_mover_ticker_cannot_be_dropped_from_complete_count(tmp_path: Path) -> None:
    root = _morning_root(tmp_path)
    snapshot_path = root / "web_collect" / "premarket_snapshot.csv"
    snapshot_path.write_text(
        f"ticker,market_date,source\n!!!,{MARKET_DATE},mover\n", encoding="utf-8"
    )

    with pytest.raises(UniverseHandoffError, match="ticker is invalid"):
        build_universe_handoff(root, MARKET_DATE)


def test_source_summary_snapshot_cannot_duplicate_canonical_artifact(tmp_path: Path) -> None:
    root = _morning_root(tmp_path)
    summary_path = root / "web_collect" / "source_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["snapshot_path"] = str(root / "alpha_cycle.json")
    summary_path.write_text(json.dumps(summary, sort_keys=True), encoding="utf-8")

    with pytest.raises(UniverseHandoffError):
        build_universe_handoff(root, MARKET_DATE)


@pytest.mark.parametrize("mutation", ["bytes", "date"])
def test_handoff_mutation_or_cross_date_is_fail_closed(tmp_path: Path, mutation: str) -> None:
    root = _morning_root(tmp_path)
    path = root / "paperops_universe_handoff.json"
    build_universe_handoff(root, MARKET_DATE, output_path=path)
    if mutation == "bytes":
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["universe_symbols"].append("CCC")
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(UniverseHandoffError, match="content hash"):
            load_universe_handoff(path, market_date=MARKET_DATE)
    else:
        with pytest.raises(UniverseHandoffError, match="market date"):
            load_universe_handoff(path, market_date="2026-08-27")


def test_handoff_source_mutation_is_fail_closed(tmp_path: Path) -> None:
    root = _morning_root(tmp_path)
    path = root / "paperops_universe_handoff.json"
    build_universe_handoff(root, MARKET_DATE, output_path=path)
    source_path = root / "web_collect" / "premarket_snapshot.csv"
    source_path.write_text(
        source_path.read_text(encoding="utf-8") + "CCC,2026-08-28,evil\n", encoding="utf-8"
    )

    with pytest.raises(UniverseHandoffError, match="artifact hash"):
        load_universe_handoff(path, market_date=MARKET_DATE)


def test_self_consistent_forged_union_membership_and_coverage_are_rejected_by_loader_and_consumer(
    tmp_path: Path,
) -> None:
    morning = _morning_root(tmp_path)
    handoff_path = morning / "paperops_universe_handoff.json"
    build_universe_handoff(morning, MARKET_DATE, output_path=handoff_path)

    forged = json.loads(handoff_path.read_text(encoding="utf-8"))
    forged["universe_symbols"] = ["ZZZZ"]
    forged["symbols"] = ["ZZZZ"]
    forged["members"] = [
        {
            "symbol": "ZZZZ",
            "lanes": ["mover"],
            "lane": "mover",
            "index_memberships": [],
            "sources": ["forged"],
            "member_lineage": {"source_identity": "forged", "market_date": MARKET_DATE},
        }
    ]
    forged["coverage"] = {
        "status": "PARTIAL",
        "core_membership_count": 0,
        "core_included_count": 0,
        "mover_declared_count": 1,
        "mover_included_count": 1,
        "union_count": 1,
        "overlap_count": 0,
        "shortfall_reasons": ["forged"],
    }
    unhashed = {
        key: value
        for key, value in forged.items()
        if key not in {"content_hash_sha256", "content_hash", "handoff_id", "universe_id"}
    }
    digest = hashlib.sha256(
        json.dumps(unhashed, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    forged["content_hash_sha256"] = digest
    forged["content_hash"] = digest
    forged["handoff_id"] = "paperops-universe-" + digest[:24]
    forged["universe_id"] = "paperops-pit-universe-" + digest[:24]
    handoff_path.write_text(json.dumps(forged, sort_keys=True), encoding="utf-8")

    with pytest.raises(
        UniverseHandoffError,
        match="(?:count binding|semantic binding|source root is not trusted)",
    ):
        load_universe_handoff(handoff_path, market_date=MARKET_DATE, require_production=True)

    paths = paper_ops_engine.PaperOpsPaths.create(tmp_path / "paper_ops")
    with pytest.raises(
        UniverseHandoffError,
        match="(?:count binding|semantic binding|source root is not trusted)",
    ):
        paper_ops_engine._run_config_with_universe_handoff(
            paths,
            run_date=date.fromisoformat(MARKET_DATE),
            mode=PaperRunMode.FORWARD,
            universe_handoff_path=handoff_path,
            scheduled_production=True,
            expected_code_sha="a" * 40,
        )


def test_forged_strategy_fleet_is_rejected_by_loader(tmp_path: Path) -> None:
    morning = _morning_root(tmp_path)
    handoff_path = morning / "paperops_universe_handoff.json"
    build_universe_handoff(morning, MARKET_DATE, output_path=handoff_path)
    forged = json.loads(handoff_path.read_text(encoding="utf-8"))
    forged["strategy_fleet"]["declared_paperops_strategy_ids"] = forged["strategy_fleet"][
        "expected_strategy_ids"
    ][:-1]
    unhashed = {
        key: value
        for key, value in forged.items()
        if key not in {"content_hash_sha256", "content_hash", "handoff_id", "universe_id"}
    }
    digest = hashlib.sha256(
        json.dumps(unhashed, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    forged["content_hash_sha256"] = digest
    forged["content_hash"] = digest
    forged["handoff_id"] = "paperops-universe-" + digest[:24]
    forged["universe_id"] = "paperops-pit-universe-" + digest[:24]
    handoff_path.write_text(json.dumps(forged, sort_keys=True), encoding="utf-8")

    with pytest.raises(UniverseHandoffError, match="strategy fleet"):
        load_universe_handoff(handoff_path, market_date=MARKET_DATE, require_production=True)


def test_handoff_stale_morning_cycle_is_fail_closed(tmp_path: Path) -> None:
    root = _morning_root(tmp_path)
    cycle_path = root / "alpha_cycle.json"
    cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
    cycle["generated_at"] = "2026-08-27T12:00:00+00:00"
    cycle_path.write_text(json.dumps(cycle), encoding="utf-8")

    with pytest.raises(UniverseHandoffError, match="cycle artifact"):
        build_universe_handoff(root, MARKET_DATE)


def test_scheduled_handoff_binds_outside_default_symbols_to_all_nine_strategies(
    tmp_path: Path,
) -> None:
    morning = _morning_root(tmp_path)
    handoff_path = morning / "paperops_universe_handoff.json"
    build_universe_handoff(morning, MARKET_DATE, output_path=handoff_path)
    paths = paper_ops_engine.PaperOpsPaths.create(tmp_path / "paper_ops")
    config, handoff = paper_ops_engine._run_config_with_universe_handoff(
        paths,
        run_date=date.fromisoformat(MARKET_DATE),
        mode=PaperRunMode.REPLAY,
        universe_handoff_path=handoff_path,
        scheduled_production=False,
    )
    assert config.universe_symbols == ("AAA", "BBB")
    assert handoff and handoff["coverage"]["status"] == "COMPLETE"
    strategies = paper_ops_engine._strategies_eligible_for_run(
        paths,
        config=config,
        run_date=date.fromisoformat(MARKET_DATE),
        mode=PaperRunMode.REPLAY,
    )
    assert len(strategies) == 9
    dataset = MarketDataset(
        dataset_id="fixture-universe",
        source_kind="fixture",
        timeframe="1d",
        bars_by_symbol={"AAA": (), "BBB": ()},
    )
    results = paper_ops_engine._backtest_results(dataset, strategies, config)
    assert set(results) == {strategy.strategy_id for strategy in strategies}

    calls: list[tuple[str, str]] = []

    class _SpyCache:
        def signal(self, *, strategy, symbol, **_kwargs):
            calls.append((strategy.strategy_id, symbol))
            return StrategySignal(
                strategy_id=strategy.strategy_id,
                strategy_version=strategy.version,
                symbol=symbol,
                signal_index=0,
                direction=Direction.LONG,
                entry_reference=100.0,
                stop=90.0,
                target=120.0,
                score=80.0,
                evidence=("fixture",),
                invalidation="fixture",
            )

    spy_dataset = MarketDataset(
        dataset_id="fixture-universe-spy",
        source_kind="fixture",
        timeframe="1d",
        bars_by_symbol={
            symbol: (
                MarketBar(
                    symbol=symbol,
                    timestamp=datetime(2026, 8, 28, tzinfo=timezone.utc),
                    open=100.0,
                    high=110.0,
                    low=90.0,
                    close=105.0,
                    volume=1_000,
                ),
            )
            for symbol in config.universe_symbols
        },
    )
    _signal_cards(
        dataset=spy_dataset,
        strategies=strategies,
        index=0,
        run=PaperRun(
            run_id="fixture-run",
            mode=PaperRunMode.REPLAY,
            run_date=MARKET_DATE,
            data_snapshot_id="fixture-universe-spy",
            created_at="2026-08-28T12:00:00+00:00",
        ),
        config=config,
        signal_cache=_SpyCache(),
    )
    assert set(calls) == {
        (strategy.strategy_id, symbol)
        for strategy in strategies
        for symbol in config.universe_symbols
    }


def test_scheduled_production_without_handoff_is_terminal_before_run_manifest(
    tmp_path: Path,
) -> None:
    paths = paper_ops_engine.PaperOpsPaths.create(tmp_path / "paper_ops")
    with pytest.raises(ValueError, match="requires the Morning universe handoff"):
        paper_ops_engine._run_config_with_universe_handoff(
            paths,
            run_date=date.fromisoformat(MARKET_DATE),
            mode=PaperRunMode.FORWARD,
            universe_handoff_path=None,
            scheduled_production=True,
            expected_code_sha="a" * 40,
        )
    assert not list(paths.manifests.glob("*.json"))


def test_bound_manifest_rejects_mutated_handoff_source_before_observation(tmp_path: Path) -> None:
    morning = _morning_root(tmp_path)
    handoff_path = morning / "paperops_universe_handoff.json"
    handoff = build_universe_handoff(morning, MARKET_DATE, output_path=handoff_path)
    coverage = handoff["coverage"]
    manifest = {
        "mode": "replay",
        "run_date": MARKET_DATE,
        "universe_id": handoff["universe_id"],
        "universe_symbols": handoff["universe_symbols"],
        "universe_handoff_id": handoff["handoff_id"],
        "universe_handoff_content_hash_sha256": handoff["content_hash_sha256"],
        "universe_handoff_path": str(handoff_path),
        "release_code_sha": handoff["code_sha"],
        "universe_coverage_status": coverage["status"],
        "universe_shortfall_reasons": coverage["shortfall_reasons"],
    }
    paths = paper_ops_engine.PaperOpsPaths.create(tmp_path / "paper_ops")

    assert paper_ops_engine._validate_manifest_universe_handoff(paths, manifest)
    source_path = morning / "web_collect" / "premarket_snapshot.csv"
    source_path.write_text(
        source_path.read_text(encoding="utf-8") + "CCC,2026-08-28,evil\n", encoding="utf-8"
    )
    with pytest.raises(UniverseHandoffError, match="artifact hash"):
        paper_ops_engine._validate_manifest_universe_handoff(paths, manifest)


def test_scheduled_consumer_and_loader_reject_cross_release_handoff(
    tmp_path: Path,
) -> None:
    morning = _morning_root(tmp_path)
    handoff_path = morning / "paperops_universe_handoff.json"
    build_universe_handoff(morning, MARKET_DATE, output_path=handoff_path)

    with pytest.raises(UniverseHandoffError, match="release SHA conflicts"):
        load_universe_handoff(
            handoff_path,
            market_date=MARKET_DATE,
            require_production=True,
            expected_code_sha="b" * 40,
        )
    paths = paper_ops_engine.PaperOpsPaths.create(tmp_path / "paper_ops")
    with pytest.raises(UniverseHandoffError, match="release SHA conflicts"):
        paper_ops_engine._run_config_with_universe_handoff(
            paths,
            run_date=date.fromisoformat(MARKET_DATE),
            mode=PaperRunMode.FORWARD,
            universe_handoff_path=handoff_path,
            scheduled_production=True,
            expected_code_sha="b" * 40,
        )


def test_observer_manifest_revalidation_rejects_cross_release_runtime(
    tmp_path: Path,
) -> None:
    morning = _morning_root(tmp_path)
    handoff_path = morning / "paperops_universe_handoff.json"
    handoff = build_universe_handoff(morning, MARKET_DATE, output_path=handoff_path)
    paths = paper_ops_engine.PaperOpsPaths.create(tmp_path / "paper_ops")
    config, _ = paper_ops_engine._run_config_with_universe_handoff(
        paths,
        run_date=date.fromisoformat(MARKET_DATE),
        mode=PaperRunMode.REPLAY,
        universe_handoff_path=handoff_path,
        scheduled_production=False,
        expected_code_sha="a" * 40,
    )
    data_manifest = DataTruthManifest(
        snapshot_id="observer-release-snapshot",
        created_at=f"{MARKET_DATE}T13:00:00+00:00",
        provider_id="fixture",
        provider_name="Fixture",
        symbols=tuple(handoff["universe_symbols"]),
        timeframe="1d",
        requested_start=MARKET_DATE,
        requested_end=MARKET_DATE,
        accepted_start=MARKET_DATE,
        accepted_end=MARKET_DATE,
        bar_count=2,
        accepted_bar_count=2,
        rejected_bar_count=0,
        skipped_incomplete_bars=0,
        validation_status="passed",
        warnings=(),
        raw_artifact_hashes={"fixture": "raw"},
        normalized_artifact_hash="normalized",
        source_url_or_reference=("fixture://release",),
        normalized_artifact_path="snapshots/release/normalized.csv",
        snapshot_content_hash="snapshot-hash",
        manifest_payload_hash="manifest-hash",
    )
    run = paper_ops_engine._paper_run(
        run_date=date.fromisoformat(MARKET_DATE),
        mode=PaperRunMode.REPLAY,
        data_snapshot_id=data_manifest.snapshot_id,
    )
    manifest = paper_ops_engine._ensure_run_manifest(
        paths,
        run,
        config=config,
        data_manifest=data_manifest,
        data_truth_root=paths.root / "replay_data_truth",
        universe_handoff=handoff,
        universe_handoff_path=handoff_path,
    )
    policy = str(manifest["execution_policy_version"])
    identity = {
        run.run_id: {
            "mode": "replay",
            "run_date": MARKET_DATE,
            "data_snapshot_id": data_manifest.snapshot_id,
            "calendar_policies": {policy},
            "ledger_policies": {policy},
        }
    }

    assert _is_complete_manifest(
        manifest,
        identity,
        "replay",
        expected_code_sha="a" * 40,
    )
    assert not _is_complete_manifest(
        manifest,
        identity,
        "replay",
        expected_code_sha="b" * 40,
    )


def test_scheduled_powershell_path_requires_handoff_without_implicit_default_fallback() -> None:
    morning = Path("scripts/run_alphaops_morning.ps1").read_text(encoding="utf-8")
    eod = Path("scripts/run_alphaops_eod.ps1").read_text(encoding="utf-8")

    assert '"scripts\\build_paperops_universe_handoff.py"' in morning
    assert '"--morning-root", $outputRoot' in morning
    assert '"--out", $universeHandoffPath' in morning
    assert '"--validate"' in eod
    assert '"--handoff", $universeHandoffPath' in eod
    assert '"--universe-handoff", $universeHandoffPath' in eod
    assert '"--expected-code-sha", $releaseSha' in eod
    assert '"--scheduled-production"' in eod
    assert eod.index("$handoffValidation") < eod.index('"--scheduled-production"')
    handoff_validation = eod.index("$handoffValidation = Invoke-DawnstrikeNativeProcess")
    expected_sha_binding = eod.index('"--expected-code-sha", $releaseSha', handoff_validation)
    assert expected_sha_binding < eod.index('"alpha-capture-outcomes"')
    assert expected_sha_binding < eod.index('"strategy-learning-daily"')


def test_eod_cross_release_handoff_failure_is_terminal_and_observable_before_writes() -> None:
    eod = Path("scripts/run_alphaops_eod.ps1").read_text(encoding="utf-8")
    guard_start = eod.index("if ($handoffValidation.exit_code -ne 0)")
    first_mutating_capture = eod.index('"alpha-capture-outcomes"')
    failure_path = eod[guard_start:first_mutating_capture]

    assert '"--expected-code-sha", $releaseSha' in eod[:guard_start]
    assert '"--release-sha", $releaseSha' in failure_path
    assert 'Name = "eod_outcome_capture"' in failure_path
    assert 'Name = "paper_reconciliation"' in failure_path
    assert 'Name = "alpha_learning"' in failure_path
    assert 'Name = "paperops_forward"' in failure_path
    assert 'Error = "eod_precondition_universe_handoff_invalid"' in failure_path
    assert failure_path.count('Error = "blocked_by_eod_precondition"') == 3
    assert "-Status FAILED" in failure_path
    assert '"--status", "FAILED"' in failure_path
    assert '"stage_failure_notification-$MarketDate"' in failure_path
    assert "exit $handoffFailureExit" in failure_path
