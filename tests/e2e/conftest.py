"""Structured pytest reporting for the E2E rehearsal's evidence manifest.

Uses ``session.items`` / ``item.nodeid`` for collected identities and
``report.nodeid`` / ``report.when`` / ``report.outcome`` for execution
results - never a regex over human-formatted terminal output, so node ids
with brackets, spaces, or unicode (parametrized cases) survive intact.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

_MANIFEST_ENV = "DAWNSTRIKE_E2E_PYTEST_MANIFEST"


def pytest_collection_modifyitems(session: pytest.Session, config: pytest.Config, items: list) -> None:
    manifest_path = os.environ.get(_MANIFEST_ENV)
    if not manifest_path:
        return
    nodeids = [item.nodeid for item in items]
    Path(manifest_path).write_text(json.dumps({"collected_nodeids": nodeids}), encoding="utf-8")


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    manifest_path = os.environ.get(_MANIFEST_ENV)
    if not manifest_path:
        return
    p = Path(manifest_path)
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except json.JSONDecodeError:
        data = {}
    reports = data.setdefault("reports", [])
    reports.append(
        {
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "duration": report.duration,
        }
    )
    p.write_text(json.dumps(data), encoding="utf-8")
