"""Forward strategy activation and immutable-lineage regression tests."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from intraday_scanner.v2.paper_ops.engine import (
    PaperOpsPaths,
    _ensure_execution_policy_manifest,
    _ensure_strategy_semantics_manifest,
    _series_is_eligible_for_run,
    init,
)
from intraday_scanner.v2.paper_ops.models import PaperOpsConfig, PaperRunMode
from intraday_scanner.v2.paper_ops.storage import read_json, write_json, write_jsonl
from intraday_scanner.v2.strategies import build_strategy_catalog


def test_new_series_waits_for_inception_in_forward_but_not_replay(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    paths = PaperOpsPaths.create(root)
    registry = read_json(paths.state / "strategy_registry.json", [])
    semantics = read_json(paths.state / "strategy_semantics_manifest.json", {})
    policies = read_json(paths.state / "execution_policy_manifest.json", {})
    assert isinstance(registry, list) and registry
    assert isinstance(semantics, dict) and isinstance(policies, dict)
    row = registry[0]
    assert isinstance(row, dict)
    strategy_entry = semantics["strategies"][  # type: ignore[index]
        f"{row['strategy_id']}@{row['strategy_version']}"
    ]
    policy_entry = policies["policies"][row["execution_policy_version"]]  # type: ignore[index]
    assert strategy_entry["activation_policy"] == (
        "next_market_session_after_registration"
    )
    inception = max(
        date.fromisoformat(str(strategy_entry["coverage_inception_date"])),
        date.fromisoformat(str(policy_entry["coverage_inception_date"])),
    )
    identity = {
        "strategy_id": str(row["strategy_id"]),
        "strategy_version": str(row["strategy_version"]),
        "execution_policy_version": str(row["execution_policy_version"]),
        "strategy_semantics_fingerprint": str(
            row["strategy_semantics_fingerprint"]
        ),
    }

    assert not _series_is_eligible_for_run(
        paths,
        run_date=inception - timedelta(days=1),
        mode=PaperRunMode.FORWARD,
        **identity,
    )
    assert _series_is_eligible_for_run(
        paths,
        run_date=inception,
        mode=PaperRunMode.FORWARD,
        **identity,
    )
    assert _series_is_eligible_for_run(
        paths,
        run_date=inception - timedelta(days=1),
        mode=PaperRunMode.REPLAY,
        **identity,
    )


def test_missing_semantics_manifest_fails_closed_with_forward_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    paths = PaperOpsPaths.create(root)
    write_jsonl(paths.ledger / "paper_ledger.jsonl", [{"mode": "forward"}])
    (paths.state / "strategy_semantics_manifest.json").unlink()

    with pytest.raises(ValueError, match="no immutable strategy-semantics manifest"):
        _ensure_strategy_semantics_manifest(paths, tuple(build_strategy_catalog()))


def test_malformed_active_policy_entry_is_not_recreated(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    paths = PaperOpsPaths.create(root)
    manifest_path = paths.state / "execution_policy_manifest.json"
    manifest = read_json(manifest_path, {})
    assert isinstance(manifest, dict)
    active = str(manifest["active_execution_policy_version"])
    manifest["policies"][active] = None  # type: ignore[index]
    write_json(manifest_path, manifest)

    with pytest.raises(ValueError, match="manifest entry is malformed"):
        _ensure_execution_policy_manifest(paths, PaperOpsConfig())


def test_missing_registered_strategy_lineage_is_not_minted_again(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    paths = PaperOpsPaths.create(root)
    write_jsonl(paths.ledger / "paper_ledger.jsonl", [{"mode": "forward"}])
    registry = read_json(paths.state / "strategy_registry.json", [])
    manifest_path = paths.state / "strategy_semantics_manifest.json"
    manifest = read_json(manifest_path, {})
    assert isinstance(registry, list) and registry and isinstance(manifest, dict)
    row = registry[0]
    assert isinstance(row, dict)
    key = f"{row['strategy_id']}@{row['strategy_version']}"
    del manifest["strategies"][key]  # type: ignore[index]
    write_json(manifest_path, manifest)

    with pytest.raises(ValueError, match="lineage is missing"):
        _ensure_strategy_semantics_manifest(paths, tuple(build_strategy_catalog()))
