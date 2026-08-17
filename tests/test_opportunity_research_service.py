from __future__ import annotations

import json
import socket
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from test_opportunity_persistence import _initialize_schema_through

from intraday_scanner import cli
from intraday_scanner.services.opportunity_catalyst_adapter import (
    load_retained_catalyst_adapter,
)
from intraday_scanner.services.opportunity_research_service import (
    LOCAL_UNIVERSE_SCHEMA,
    LocalOpportunityResearchEntrypoint,
    LocalResearchStatus,
    OpportunityResearchMode,
)
from intraday_scanner.v2.data import MarketBar, MarketDataset, write_ohlcv_csv
from intraday_scanner.v2.data_truth import build_data_truth_snapshot
from intraday_scanner.v2.opportunity.producer import StageStatus

UTC = timezone.utc
DECISION_AT = datetime(2026, 1, 7, 15, tzinfo=UTC)
CAPTURED_AT = datetime(2026, 1, 6, 22, tzinfo=UTC)


def test_disabled_cli_mount_is_default_noop_without_paths(capsys) -> None:
    assert cli.main(["opportunity-research"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "DISABLED"
    assert payload["network_enabled"] is False
    assert payload["broker_execution_enabled"] is False


def test_local_entrypoint_mounts_current_historical_and_cache_without_network(
    monkeypatch,
    tmp_path: Path,
) -> None:
    data_root, snapshot_id = _data_truth_fixture(tmp_path)
    universe_path = _universe_evidence(tmp_path)
    databases = tuple((tmp_path / f"research-{name}.sqlite").resolve() for name in ("a", "b", "c"))
    for database in databases:
        _initialize_schema_through(database, 30)

    def network_forbidden(*_args, **_kwargs):
        raise AssertionError("offline opportunity mount attempted network access")

    monkeypatch.setattr(socket, "create_connection", network_forbidden)
    entrypoint = LocalOpportunityResearchEntrypoint(enabled=True)
    current = entrypoint.run(
        mode=OpportunityResearchMode.CURRENT,
        data_truth_root=data_root,
        snapshot_id=snapshot_id,
        database_path=databases[0],
        decision_at=DECISION_AT,
        recorded_at=DECISION_AT,
        universe_evidence_path=universe_path,
    )
    cached = entrypoint.run(
        mode=OpportunityResearchMode.CURRENT,
        data_truth_root=data_root,
        snapshot_id=snapshot_id,
        database_path=databases[1],
        decision_at=DECISION_AT,
        recorded_at=DECISION_AT,
        universe_evidence_path=universe_path,
    )
    historical = entrypoint.run(
        mode=OpportunityResearchMode.HISTORICAL,
        data_truth_root=data_root,
        snapshot_id=snapshot_id,
        database_path=databases[2],
        decision_at=DECISION_AT,
        recorded_at=DECISION_AT,
        universe_evidence_path=universe_path,
    )

    assert current.status is LocalResearchStatus.COMPLETE
    assert cached.status is LocalResearchStatus.COMPLETE
    assert historical.status is LocalResearchStatus.COMPLETE
    assert cached.producer_receipt is not None
    assert cached.producer_receipt.telemetry[0].status is StageStatus.CACHE_HIT
    assert current.producer_receipt is not None
    assert historical.producer_receipt is not None
    assert (
        current.producer_receipt.pipeline_result.to_json()
        == historical.producer_receipt.pipeline_result.to_json()
    )
    assert current.network_enabled is False
    assert current.broker_execution_enabled is False
    for database in databases:
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM opportunity_pipeline_runs"
            ).fetchone() == (1,)


def test_enabled_cli_missing_inputs_emits_structured_failure_without_paths(capsys) -> None:
    assert cli.main(["opportunity-research", "--enable-research"]) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "FAILED"
    assert payload["failure"]["failure_code"] == "input_evidence_failed"
    assert "explicit retained" not in json.dumps(payload)


def test_retained_catalyst_adapter_is_read_only_causal_and_missing_is_unavailable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "retained-catalyst.sqlite"
    payload = {"event_id": "event-1", "event_type": "verified_filing"}
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE catalyst_evidence_events (
                event_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_content_hash_sha256 TEXT NOT NULL,
                published_at TEXT,
                first_seen_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO catalyst_evidence_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "event-1",
                "TST",
                "retained-fixture",
                "a" * 64,
                CAPTURED_AT.isoformat(),
                CAPTURED_AT.isoformat(),
                "verified_filing",
                json.dumps(payload),
            ),
        )
    before = database.read_bytes()

    adapter = load_retained_catalyst_adapter(
        database.resolve(),
        decision_at=DECISION_AT,
        symbols=("TST", "NONE"),
    )

    assert adapter.evidence_at("TST", decision_at=DECISION_AT).state == "verified_filing"
    assert adapter.evidence_at("NONE", decision_at=DECISION_AT) is None
    assert database.read_bytes() == before
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


def _data_truth_fixture(tmp_path: Path) -> tuple[Path, str]:
    raw_dir = tmp_path / "provider-raw"
    raw_dir.mkdir()
    (raw_dir / "tst.json").write_text(json.dumps({"retained": True}), encoding="utf-8")
    source_csv = tmp_path / "source.csv"
    dataset = MarketDataset(
        dataset_id="local-retained-fixture",
        source_kind="local-retained-fixture",
        timeframe="1d",
        bars_by_symbol={
            "TST": tuple(
                MarketBar(
                    symbol="TST",
                    timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
                    open=price,
                    high=price + 1,
                    low=price - 1,
                    close=price + 0.5,
                    volume=1_000_000,
                )
                for day, price in (
                    (date(2026, 1, 1), 10.0),
                    (date(2026, 1, 2), 10.5),
                    (date(2026, 1, 5), 11.0),
                )
            )
        },
    )
    write_ohlcv_csv(dataset, source_csv)
    output_root = (tmp_path / "data-truth").resolve()
    result = build_data_truth_snapshot(
        as_of_date=date(2026, 1, 5),
        output_root=output_root,
        created_at=CAPTURED_AT,
        source_csv=source_csv,
        raw_dir=raw_dir,
        allow_fetch=False,
    )
    return output_root, result.manifest.snapshot_id


def _universe_evidence(tmp_path: Path) -> Path:
    path = (tmp_path / "universe-evidence.json").resolve()
    path.write_text(
        json.dumps(
            {
                "schema_version": LOCAL_UNIVERSE_SCHEMA,
                "observed_at": CAPTURED_AT.isoformat(),
                "members": [
                    {
                        "symbol": "TST",
                        "security_type": "common_stock",
                        "venue": "XNYS",
                        "first_seen_at": "2026-01-01T00:00:00+00:00",
                        "halt_status": "clear",
                        "corporate_action_status": "clear",
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path
