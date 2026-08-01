"""Build auditable V6 operator research packets from durable evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from intraday_scanner.services.alpha_v6_learning_service import run_alpha_v6_learning
from intraday_scanner.services.v6_learning_service import build_v6_failure_attribution
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def build_alpha_v6_research_packet(store: SQLiteScanStore, *, code_sha: str) -> dict[str, Any]:
    learning = run_alpha_v6_learning(store, code_sha=code_sha)
    attribution = build_v6_failure_attribution(store)
    return {
        "schema_version": "dawnstrike.alphaops_v6.research_packet.v1",
        "learning": learning,
        "failure_attribution": attribution,
        "performance_status": "WAITING_FOR_FORWARD_EVIDENCE",
        "research_only": True,
        "broker_execution_enabled": False,
    }


def write_alpha_v6_research_packet(
    store: SQLiteScanStore,
    *,
    code_sha: str,
    out_dir: str | Path,
) -> dict[str, Any]:
    packet = build_alpha_v6_research_packet(store, code_sha=code_sha)
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    failure_path = root / "alphaops_v6_failure_attribution.md"
    registry_path = root / "alphaops_v6_experiment_registry.md"
    failure_path.write_text(_failure_markdown(packet), encoding="utf-8")
    registry_path.write_text(_registry_markdown(packet), encoding="utf-8")
    return {
        **packet,
        "paths": {
            "failure_attribution": str(failure_path),
            "experiment_registry": str(registry_path),
        },
    }


def _failure_markdown(packet: dict[str, Any]) -> str:
    attribution = dict(packet.get("failure_attribution") or {})
    rows = list(attribution.get("breakdown") or [])
    lines = [
        "# AlphaOps V6 failure attribution",
        "",
        "This is a research-only, sourced-outcome report. Missing truth is excluded, never zero.",
        "",
        "| Setup / regime | Outcomes | Eligible returns | Mean net excess | Worst net excess |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| {group} | {outcome_count} | {eligible_return_count} | {mean} | {worst} |".format(
                group=row.get("group") or "unknown",
                outcome_count=row.get("outcome_count") or 0,
                eligible_return_count=row.get("eligible_return_count") or 0,
                mean=_value(row.get("mean_net_excess_return_pct")),
                worst=_value(row.get("worst_net_excess_return_pct")),
            )
        )
    if not rows:
        lines.append("| No sourced V6 outcomes yet | 0 | 0 | null | null |")
    lines.extend(
        [
            "",
            "`PERFORMANCE_STATUS=WAITING_FOR_FORWARD_EVIDENCE`",
            "",
            "No result is a strategy promotion or investment recommendation.",
        ]
    )
    return "\n".join(lines) + "\n"


def _registry_markdown(packet: dict[str, Any]) -> str:
    attribution = dict(packet.get("failure_attribution") or {})
    rows = list(attribution.get("proposed_experiments") or [])
    lines = [
        "# AlphaOps V6 experiment registry",
        "",
        (
            "Every listed experiment is one-change, forward-only, holdout-gated, "
            "and not applied automatically."
        ),
        "",
        "| Experiment | Group | Sample | Status | Hypothesis |",
        "|---|---|---:|---|---|",
    ]
    for row in rows:
        lines.append(
            "| {id} | {group} | {sample} | {status} | {hypothesis} |".format(
                id=row.get("experiment_id") or "unknown",
                group=row.get("group") or "unknown",
                sample=row.get("sample_size") or 0,
                status=row.get("status") or "unknown",
                hypothesis=str(row.get("hypothesis") or "").replace("|", "/"),
            )
        )
    if not rows:
        lines.append(
            "| none | — | 0 | WAITING_FOR_OUTCOMES | "
            "No data-supported change is registered. |"
        )
    lines.extend(
        [
            "",
            "Promotion is manual and remains blocked until the full forward-evidence gate passes.",
        ]
    )
    return "\n".join(lines) + "\n"


def _value(value: object) -> str:
    return "null" if value is None else str(value)


__all__ = ["build_alpha_v6_research_packet", "write_alpha_v6_research_packet"]
