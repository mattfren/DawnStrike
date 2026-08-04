"""Coverage inception tests for the canonical PaperOps truth gate."""

from __future__ import annotations

from pathlib import Path

from intraday_scanner.v2.paper_ops.calendar_truth import _missing_strategy_rows
from intraday_scanner.v2.paper_ops.engine import PaperOpsPaths
from intraday_scanner.v2.paper_ops.storage import write_json


def test_truth_gate_requires_strategy_only_from_first_eligible_session(
    tmp_path: Path,
) -> None:
    paths = PaperOpsPaths.create(tmp_path / "paper_ops")
    policy = "paper-policy-v1"
    alpha_fingerprint = "a" * 64
    beta_fingerprint = "b" * 64
    registry = [
        _registry_row("alpha", alpha_fingerprint, policy),
        _registry_row("beta", beta_fingerprint, policy),
    ]
    write_json(paths.state / "strategy_registry.json", registry)
    write_json(
        paths.state / "strategy_semantics_manifest.json",
        {
            "schema_version": "v2.strategy_semantics_manifest.v1",
            "strategies": {
                "alpha@v1.0": _manifest_entry(
                    alpha_fingerprint,
                    "2026-07-01T12:00:00+00:00",
                    "2026-07-01",
                ),
                "beta@v1.0": _manifest_entry(
                    beta_fingerprint,
                    "2026-07-16T20:01:00+00:00",
                    "2026-07-17",
                ),
            },
        },
    )
    write_json(
        paths.state / "execution_policy_manifest.json",
        {
            "schema_version": "v2.paper_execution_policy_manifest.v1",
            "active_execution_policy_version": policy,
            "policies": {
                policy: {
                    "configuration": {},
                    "fingerprint": "c" * 64,
                    "registered_at": "2026-07-01T12:00:00+00:00",
                    "coverage_inception_date": "2026-07-01",
                }
            },
        },
    )
    rows = [
        _calendar_row("2026-07-16", "alpha", alpha_fingerprint, policy),
        _calendar_row("2026-07-17", "alpha", alpha_fingerprint, policy),
    ]

    missing = _missing_strategy_rows(paths, rows, [])

    assert missing == [
        f"2026-07-17:forward:beta:v1.0:{policy}:{beta_fingerprint}"
    ]


def test_truth_gate_uses_exact_identity_not_strategy_id_only(tmp_path: Path) -> None:
    paths = PaperOpsPaths.create(tmp_path / "paper_ops")
    policy = "paper-policy-v1"
    fingerprint = "a" * 64
    write_json(
        paths.state / "strategy_registry.json",
        [_registry_row("alpha", fingerprint, policy, version="v2.0")],
    )
    write_json(
        paths.state / "strategy_semantics_manifest.json",
        {
            "schema_version": "v2.strategy_semantics_manifest.v1",
            "strategies": {
                "alpha@v2.0": _manifest_entry(
                    fingerprint,
                    "2026-07-01T12:00:00+00:00",
                    "2026-07-01",
                )
            },
        },
    )
    _write_policy_manifest(paths, policy)
    rows = [
        _calendar_row(
            "2026-07-16",
            "alpha",
            fingerprint,
            policy,
            version="v1.0",
        )
    ]

    missing = _missing_strategy_rows(paths, rows, [])

    assert missing == [
        f"2026-07-16:forward:alpha:v2.0:{policy}:{fingerprint}"
    ]


def test_truth_gate_rejects_exact_pre_inception_forward_but_keeps_replay(
    tmp_path: Path,
) -> None:
    paths = PaperOpsPaths.create(tmp_path / "paper_ops")
    policy = "paper-policy-v1"
    fingerprint = "a" * 64
    write_json(
        paths.state / "strategy_registry.json",
        [_registry_row("alpha", fingerprint, policy)],
    )
    write_json(
        paths.state / "strategy_semantics_manifest.json",
        {
            "schema_version": "v2.strategy_semantics_manifest.v1",
            "strategies": {
                "alpha@v1.0": _manifest_entry(
                    fingerprint,
                    "2026-07-16T12:00:00+00:00",
                    "2026-07-17",
                    activation_policy="next_market_session_after_registration",
                )
            },
        },
    )
    _write_policy_manifest(paths, policy)
    forward = _calendar_row("2026-07-16", "alpha", fingerprint, policy)
    replay = {**forward, "mode": "replay"}

    forward_missing = _missing_strategy_rows(paths, [forward], [])
    replay_missing = _missing_strategy_rows(paths, [replay], [])

    assert forward_missing == [
        f"pre-inception forward row:2026-07-16:alpha:v1.0:{policy}:{fingerprint}"
    ]
    assert replay_missing == []


def test_truth_gate_detects_whole_missing_forward_market_session(
    tmp_path: Path,
) -> None:
    paths = PaperOpsPaths.create(tmp_path / "paper_ops")
    policy = "paper-policy-v1"
    fingerprint = "a" * 64
    write_json(
        paths.state / "strategy_registry.json",
        [_registry_row("alpha", fingerprint, policy)],
    )
    write_json(
        paths.state / "strategy_semantics_manifest.json",
        {
            "schema_version": "v2.strategy_semantics_manifest.v1",
            "strategies": {
                "alpha@v1.0": _manifest_entry(
                    fingerprint,
                    "2026-07-01T12:00:00+00:00",
                    "2026-07-01",
                )
            },
        },
    )
    _write_policy_manifest(paths, policy)
    rows = [
        _calendar_row("2026-07-14", "alpha", fingerprint, policy),
        _calendar_row("2026-07-16", "alpha", fingerprint, policy),
    ]

    missing = _missing_strategy_rows(paths, rows, [])

    assert missing == [
        f"2026-07-15:forward:alpha:v1.0:{policy}:{fingerprint}"
    ]


def test_truth_gate_keeps_acknowledged_terminal_session_missing_not_zero(
    tmp_path: Path,
) -> None:
    paths = PaperOpsPaths.create(tmp_path / "paper_ops")
    policy = "paper-policy-v1"
    fingerprint = "a" * 64
    write_json(
        paths.state / "strategy_registry.json",
        [_registry_row("alpha", fingerprint, policy)],
    )
    write_json(
        paths.state / "strategy_semantics_manifest.json",
        {
            "schema_version": "v2.strategy_semantics_manifest.v1",
            "strategies": {
                "alpha@v1.0": _manifest_entry(
                    fingerprint,
                    "2026-07-01T12:00:00+00:00",
                    "2026-07-01",
                )
            },
        },
    )
    _write_policy_manifest(paths, policy)
    rows = [
        _calendar_row("2026-07-14", "alpha", fingerprint, policy),
        _calendar_row("2026-07-16", "alpha", fingerprint, policy),
    ]

    missing = _missing_strategy_rows(
        paths,
        rows,
        [],
        acknowledged_session_dates={"2026-07-15"},
    )

    assert missing == []
    assert all(row["date"] != "2026-07-15" for row in rows)


def _registry_row(
    strategy_id: str,
    fingerprint: str,
    policy: str,
    *,
    version: str = "v1.0",
) -> dict[str, object]:
    return {
        "strategy_id": strategy_id,
        "strategy_version": version,
        "execution_policy_version": policy,
        "strategy_semantics_fingerprint": fingerprint,
    }


def _manifest_entry(
    fingerprint: str,
    registered_at: str,
    coverage_inception_date: str,
    *,
    activation_policy: str | None = None,
) -> dict[str, object]:
    output: dict[str, object] = {
        "configuration": {},
        "fingerprint": fingerprint,
        "registered_at": registered_at,
        "coverage_inception_date": coverage_inception_date,
    }
    if activation_policy is not None:
        output["activation_policy"] = activation_policy
    return output


def _calendar_row(
    row_date: str,
    strategy_id: str,
    fingerprint: str,
    policy: str,
    *,
    version: str = "v1.0",
) -> dict[str, str]:
    return {
        "date": row_date,
        "mode": "forward",
        "strategy_id": strategy_id,
        "strategy_version": version,
        "execution_policy_version": policy,
        "strategy_semantics_fingerprint": fingerprint,
    }


def _write_policy_manifest(paths: PaperOpsPaths, policy: str) -> None:
    write_json(
        paths.state / "execution_policy_manifest.json",
        {
            "schema_version": "v2.paper_execution_policy_manifest.v1",
            "active_execution_policy_version": policy,
            "policies": {
                policy: {
                    "configuration": {},
                    "fingerprint": "c" * 64,
                    "registered_at": "2026-07-01T12:00:00+00:00",
                    "coverage_inception_date": "2026-07-01",
                }
            },
        },
    )
