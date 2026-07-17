from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from intraday_scanner.dashboard import mover_strategy_calendar_page as page


def _fingerprint(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _write_latest(root: Path, *, report_directory: Path | None = None) -> Path:
    reports = root / "reports"
    reports.mkdir(parents=True)
    report = {
        "schema_version": "v2.mover_pattern_lab.v1.analysis",
        "research_only": True,
        "broker_execution_enabled": False,
        "strategy_daily_calendar": [],
    }
    fingerprint = _fingerprint(report)
    destination = report_directory or reports
    destination.mkdir(parents=True, exist_ok=True)
    report_path = destination / f"mover_pattern_analysis_{fingerprint[:16]}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    latest = {
        "schema_version": "v2.mover_pattern_lab.v1.analysis_latest",
        "analysis_fingerprint": fingerprint,
        "report_path": str(report_path.resolve()),
        "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
    }
    (reports / "mover_pattern_analysis_latest.json").write_text(
        json.dumps(latest, indent=2),
        encoding="utf-8",
    )
    return report_path


def test_mover_calendar_renders_only_verified_retained_analysis(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    _write_latest(tmp_path)
    markdown: list[str] = []
    captions: list[str] = []
    rendered: list[tuple[str, int, bool]] = []
    monkeypatch.setattr(page.st, "markdown", markdown.append)
    monkeypatch.setattr(page.st, "caption", captions.append)
    monkeypatch.setattr(
        page.components,
        "html",
        lambda value, *, height, scrolling: rendered.append(
            (value, height, scrolling)
        ),
    )

    assert page.render_mover_strategy_calendar(tmp_path) is True
    assert markdown == ["### Mover Pattern Lab"]
    assert "missing outcomes remain blank" in captions[0]
    assert len(rendered) == 1
    assert "Missing truth stays missing" in rendered[0][0]
    assert rendered[0][1:] == (920, True)


def test_mover_calendar_rejects_report_pointer_outside_output_root(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    _write_latest(tmp_path / "retained", report_directory=tmp_path / "outside")
    errors: list[str] = []
    monkeypatch.setattr(page.st, "error", errors.append)

    assert page.render_mover_strategy_calendar(tmp_path / "retained") is False
    assert errors == [
        "Mover strategy calendar blocked: "
        "mover calendar evidence manifest failed integrity checks"
    ]
