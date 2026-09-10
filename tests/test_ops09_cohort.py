from __future__ import annotations

import json
from pathlib import Path

import pytest

from intraday_scanner.observation.ops09 import (
    CAPTURE_BYTES,
    DOWNSTREAM_BYTES,
    MAX_BYTES,
    NATIVE_RESERVED_BYTES,
    _ByteLedger,
    Ops09Error,
    _sample_movers,
    prepare_ops09_cohort,
    resume_ops09_cohort,
    validate_ops09_scope,
)
from intraday_scanner.observation.cohort import APPROVED_PYTHON


def _source_config(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "source-config.json"
    path.write_text('{"provider":"fixture","feed":"sip"}\n', encoding="utf-8")
    import hashlib

    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _scope(tmp_path: Path, market_date: str = "2026-09-10", *, missing: bool = False) -> Path:
    marker = tmp_path / "producer.json"
    marker.write_text(json.dumps({"source": "fixture", "market_date": market_date}), encoding="utf-8")
    import hashlib

    movers = [] if missing else [
        {"symbol": f"S{i:03d}", "membership": "selected", "source_lane": "mover"}
        for i in range(2)
    ] + [
        {"symbol": f"R{i:03d}", "membership": "rejected", "source_lane": "mover"}
        for i in range(10)
    ] + [
        {"symbol": f"U{i:03d}", "membership": "unselected", "source_lane": "mover"}
        for i in range(167)
    ] + [
        {"symbol": f"M{i:03d}", "membership": "missing_input", "source_lane": "mover"}
        for i in range(2)
    ]
    panel = [{"symbol": name, "source_lane": "reference_panel"} for name in ("DIA", "IWM", "QQQ", "SPY", "TLT")]
    count = len(movers)
    payload = {
        "schema_version": "dawnstrike.observation.scope_declaration.v1",
        "market_date": market_date,
        "scopes": {"original_small_cap_gap": movers, "liquid_reference_panel": panel},
        "producer_completeness": {
            "status": "MISSING_INPUT" if missing else "COMPLETE",
            "source_count": count,
            "declared_count": count,
            "included_count": count,
            "source_as_of": market_date,
            "truncated": False,
            "survivorship_filter": False,
        },
        "missing_input": missing,
        "source_identity": {"market_date": market_date, "producer": "fixture"},
        "source_artifacts": {"producer": {"path": str(marker), "sha256": hashlib.sha256(marker.read_bytes()).hexdigest()}},
    }
    path = tmp_path / "scope.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_d042_sampling_keeps_four_strata_and_missing_input() -> None:
    rows = (
        [{"symbol": f"S{i}", "membership": "selected"} for i in range(2)]
        + [{"symbol": f"R{i}", "membership": "rejected"} for i in range(10)]
        + [{"symbol": f"U{i}", "membership": "unselected"} for i in range(167)]
        + [{"symbol": f"M{i}", "membership": "missing_input"} for i in range(2)]
    )
    result = _sample_movers(rows)
    assert {name: sum(row["membership"] == name for row in result["rows"]) for name in ("selected", "rejected", "unselected", "missing_input")} == {"selected": 2, "rejected": 4, "unselected": 4, "missing_input": 2}
    assert result["population_counts"] == {"selected": 2, "rejected": 10, "unselected": 167, "missing_input": 2}


def test_d081_sampling_redistributes_when_strata_are_unavailable() -> None:
    rows = (
        [{"symbol": "S0", "membership": "selected"}]
        + [{"symbol": "R0", "membership": "rejected"}]
        + [{"symbol": f"U{i}", "membership": "unselected"} for i in range(5)]
    )
    result = _sample_movers(rows)
    assert result["population_counts"] == {"selected": 1, "rejected": 1, "unselected": 5, "missing_input": 0}
    assert result["sampled_count"] == 7
    assert {row["membership"] for row in result["rows"]} == {"selected", "rejected", "unselected"}
    assert next(row for row in result["rows"] if row["membership"] == "selected")["inclusion_probability"] == 1.0


def test_scope_requires_date_bound_complete_producer(tmp_path: Path) -> None:
    path = _scope(tmp_path)
    value = validate_ops09_scope(path, expected_date="2026-09-10")
    assert value["mover_count"] == 181
    assert value["sampling"]["sampled_count"] == 12
    with pytest.raises(Ops09Error, match="date mismatch"):
        validate_ops09_scope(path, expected_date="2026-09-11")


def test_missing_input_is_explicit_panel_partial(tmp_path: Path) -> None:
    value = validate_ops09_scope(_scope(tmp_path, missing=True), expected_date="2026-09-10")
    assert value["missing_input"] is True
    assert value["mover_count"] == 0


def test_prepare_freezes_ten_sessions_and_caps(tmp_path: Path) -> None:
    source_path, source_hash = _source_config(tmp_path)
    plan = prepare_ops09_cohort(
        output_root=tmp_path / "out", input_root=tmp_path / "in", scope_root=tmp_path / "scope",
        database_root=tmp_path / "db", repo_root=Path(__file__).parents[1],
        source_config_hash=source_hash, source_config_path=source_path, python_path=APPROVED_PYTHON,
    )
    assert len(plan["expected_sessions"]) == 10
    assert plan["caps"]["max_pages"] == 100
    assert plan["caps"]["max_attempts_total"] == 3
    assert plan["caps"]["capture_bytes"] == 48 * 1024 * 1024
    assert plan["safety"]["no_auto_renewal"] is True


def test_operator_stop_is_durable_and_stops_pending_sessions(tmp_path: Path) -> None:
    out = tmp_path / "out"
    source_path, source_hash = _source_config(tmp_path)
    prepare_ops09_cohort(
        output_root=out, input_root=tmp_path / "in", scope_root=tmp_path / "scope",
        database_root=tmp_path / "db", repo_root=Path(__file__).parents[1],
        source_config_hash=source_hash, source_config_path=source_path, python_path=APPROVED_PYTHON,
    )
    (out / ".cohort.stop").write_text("operator test\n", encoding="utf-8")
    state = resume_ops09_cohort(
        output_root=out, input_root=tmp_path / "in", scope_root=tmp_path / "scope",
        database_root=tmp_path / "db", repo_root=Path(__file__).parents[1],
        now=None,
    )
    assert state["status"] == "STOPPED"
    assert all(row["status"] == "STOPPED" for row in state["sessions"])


def test_byte_ledger_admits_native_capture_and_rejects_overcommitted_downstream(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output"
    database_root = tmp_path / "database"
    identity = {
        "cohort_id": "cohort-test",
        "output_root": str(output_root.resolve()),
        "database_root": str(database_root.resolve()),
        "repository": {"code_sha": "a" * 40, "tree_sha": "b" * 40, "root": "fixture"},
    }
    ledger = _ByteLedger(output_root=output_root, database_root=database_root, identity=identity)
    ledger.admit("capture", CAPTURE_BYTES + NATIVE_RESERVED_BYTES)
    with pytest.raises(Ops09Error, match="cumulative byte budget rejects downstream"):
        ledger.admit("downstream", DOWNSTREAM_BYTES)
    released = ledger.release("capture")
    assert released["reserved_bytes"] == CAPTURE_BYTES + NATIVE_RESERVED_BYTES
    ledger.admit("downstream", DOWNSTREAM_BYTES)
    ledger.release("downstream")
    payload = json.loads((output_root / ".ops09-byte-ledger.json").read_text(encoding="utf-8"))
    assert payload["max_bytes"] == MAX_BYTES
    assert payload["reservations"] == {}


def test_byte_ledger_rejects_identity_reuse_across_database_root(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    identity = {
        "cohort_id": "cohort-test",
        "output_root": str(output_root.resolve()),
        "database_root": str((tmp_path / "database").resolve()),
        "repository": {"code_sha": "a" * 40, "tree_sha": "b" * 40, "root": "fixture"},
    }
    _ByteLedger(output_root=output_root, database_root=tmp_path / "database", identity=identity)
    changed = dict(identity)
    changed["database_root"] = str((tmp_path / "other-database").resolve())
    with pytest.raises(Ops09Error, match="identity changed"):
        _ByteLedger(output_root=output_root, database_root=tmp_path / "other-database", identity=changed)
