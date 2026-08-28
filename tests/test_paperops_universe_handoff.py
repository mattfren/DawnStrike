import csv
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from intraday_scanner.services.luna_core_universe_service import _canonical_member_hash
from intraday_scanner.v2.data import MarketBar, MarketDataset
from intraday_scanner.v2.paper_ops import engine as paper_ops_engine
from intraday_scanner.v2.paper_ops.lifecycle_backtest import _signal_cards
from intraday_scanner.v2.paper_ops.models import PaperRun, PaperRunMode
from intraday_scanner.v2.paper_ops.universe_handoff import (
    UniverseHandoffError,
    build_universe_handoff,
    load_universe_handoff,
)
from intraday_scanner.v2.strategies import Direction, StrategySignal

MARKET_DATE = "2026-08-28"


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
                "valid_from": None,
                "valid_to": None,
            }
        ]
        + [
            {
                "symbol": "AAA",
                "provider_symbol": "AAA",
                "asset_class": "common_stock",
                "index": "Nasdaq-100",
                "valid_from": None,
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
                        "valid_from": None,
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
                            "valid_from": None,
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
                        "valid_from": None,
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
                            "valid_from": None,
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
    source = {
        "status": source_status,
        "run_id": "mover-run",
        "candidate_count": 2 if source_status == "success" else 0,
        "source_failures": 0,
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
    }
    (root / "alpha_run_contract.json").write_text(
        json.dumps(contract, sort_keys=True), encoding="utf-8"
    )
    cycle = {"scan_id": "scan-fixture", "generated_at": f"{MARKET_DATE}T12:00:00+00:00"}
    (root / "alpha_cycle.json").write_text(json.dumps(cycle, sort_keys=True), encoding="utf-8")
    return root


def test_handoff_exactly_deduplicates_core_and_mover_union(tmp_path: Path) -> None:
    root = _morning_root(tmp_path)
    handoff_path = root / "paperops_universe_handoff.json"

    payload = build_universe_handoff(root, MARKET_DATE, output_path=handoff_path)

    assert payload["universe_symbols"] == ["AAA", "BBB"]
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
                "valid_from": None,
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

    with pytest.raises(UniverseHandoffError, match="semantic binding"):
        load_universe_handoff(handoff_path, market_date=MARKET_DATE, require_production=True)

    paths = paper_ops_engine.PaperOpsPaths.create(tmp_path / "paper_ops")
    with pytest.raises(UniverseHandoffError, match="semantic binding"):
        paper_ops_engine._run_config_with_universe_handoff(
            paths,
            run_date=date.fromisoformat(MARKET_DATE),
            mode=PaperRunMode.FORWARD,
            universe_handoff_path=handoff_path,
            scheduled_production=True,
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
        )
    assert not list(paths.manifests.glob("*.json"))


def test_bound_manifest_rejects_mutated_handoff_source_before_observation(tmp_path: Path) -> None:
    morning = _morning_root(tmp_path)
    handoff_path = morning / "paperops_universe_handoff.json"
    handoff = build_universe_handoff(morning, MARKET_DATE, output_path=handoff_path)
    coverage = handoff["coverage"]
    manifest = {
        "mode": "forward",
        "run_date": MARKET_DATE,
        "universe_id": handoff["universe_id"],
        "universe_symbols": handoff["universe_symbols"],
        "universe_handoff_id": handoff["handoff_id"],
        "universe_handoff_content_hash_sha256": handoff["content_hash_sha256"],
        "universe_handoff_path": str(handoff_path),
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


def test_scheduled_powershell_path_requires_handoff_without_implicit_default_fallback() -> None:
    morning = Path("scripts/run_alphaops_morning.ps1").read_text(encoding="utf-8")
    eod = Path("scripts/run_alphaops_eod.ps1").read_text(encoding="utf-8")

    assert '"scripts\\build_paperops_universe_handoff.py"' in morning
    assert '"--morning-root", $outputRoot' in morning
    assert '"--out", $universeHandoffPath' in morning
    assert '"--validate"' in eod
    assert '"--handoff", $universeHandoffPath' in eod
    assert '"--universe-handoff", $universeHandoffPath' in eod
    assert '"--scheduled-production"' in eod
    assert eod.index("$handoffValidation") < eod.index('"--scheduled-production"')
