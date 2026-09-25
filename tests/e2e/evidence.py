"""Evidence recording for the synthetic E2E rehearsal.

Every artifact this module writes carries the run id, scenario id, and the
``SYNTHETIC_E2E`` classification, either as a schema field or an enclosing
manifest field. Nothing here regexes pytest's human-readable output - node
identities and outcomes come from ``pytest_runtest_logreport`` /
``session.items`` in ``tests/e2e/conftest.py``, which is structured data.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CLASSIFICATION = "SYNTHETIC_E2E"


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class TraceEvent:
    """One node in the linked observation -> ... -> rendered-status trace."""

    stage: str
    id: str
    at: str
    links: dict[str, str]
    detail: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "id": self.id,
            "at": self.at,
            "links": self.links,
            "detail": self.detail,
            "classification": CLASSIFICATION,
        }


@dataclass
class ScenarioEvidence:
    scenario_id: str
    run_id: str
    timeline: list[dict[str, Any]] = field(default_factory=list)
    trace: list[TraceEvent] = field(default_factory=list)
    accounting: list[dict[str, Any]] = field(default_factory=list)
    outcome: dict[str, Any] | None = None
    coverage: dict[str, list[str]] | None = None

    def record_event(self, kind: str, **detail: Any) -> None:
        self.timeline.append(
            {"kind": kind, "run_id": self.run_id, "scenario_id": self.scenario_id, **detail}
        )

    def record_trace(self, event: TraceEvent) -> None:
        self.trace.append(event)

    def record_accounting(self, comparisons: list[Any]) -> None:
        self.accounting.extend(c.as_dict() for c in comparisons)

    def as_dict(self) -> dict[str, Any]:
        return {
            "classification": CLASSIFICATION,
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "timeline": self.timeline,
            "trace": [t.as_dict() for t in self.trace],
            "accounting": self.accounting,
            "outcome": self.outcome,
            "coverage": self.coverage,
        }

    def write(self, artifacts_dir: Path) -> Path:
        out = artifacts_dir / f"{self.scenario_id}.json"
        out.write_text(json.dumps(self.as_dict(), indent=2, default=str), encoding="utf-8")
        return out


@dataclass
class RunManifest:
    run_id: str
    baseline_sha: str
    scenarios: dict[str, dict[str, Any]] = field(default_factory=dict)
    pytest_reports: list[dict[str, Any]] = field(default_factory=list)
    collected_nodeids: list[str] = field(default_factory=list)

    def record_report(self, *, nodeid: str, when: str, outcome: str, duration: float) -> None:
        self.pytest_reports.append(
            {"nodeid": nodeid, "when": when, "outcome": outcome, "duration": duration}
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "classification": CLASSIFICATION,
            "run_id": self.run_id,
            "baseline_sha": self.baseline_sha,
            "collected_nodeids": self.collected_nodeids,
            "scenarios": self.scenarios,
            "pytest_reports": self.pytest_reports,
        }

    def write(self, artifacts_dir: Path) -> Path:
        out = artifacts_dir / "manifest.json"
        out.write_text(json.dumps(self.as_dict(), indent=2, default=str), encoding="utf-8")
        return out
