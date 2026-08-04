from __future__ import annotations

import json
from pathlib import Path

from scripts.verify_public_artifact_security import scan_public_artifact


def test_safe_public_artifact_passes_security_gate(tmp_path: Path) -> None:
    artifact = tmp_path / "public"
    artifact.mkdir()
    (artifact / "status.json").write_text(
        '{"source_status":"HEALTHY","holdout_status":"INSUFFICIENT_EVIDENCE"}',
        encoding="utf-8",
    )

    assert scan_public_artifact(artifact) == []


def test_public_artifact_rejects_private_values(tmp_path: Path) -> None:
    artifact = tmp_path / "public"
    artifact.mkdir()
    (artifact / "unsafe.json").write_text(
        """{
          "database_path": "C:\\\\r\\\\dawnstrike-state\\\\shadow_real.sqlite",
          "holdout_evaluation_id": "v6eval-0123456789abcdef",
          "api_key": "not-a-real-key-123456789"
        }""",  # pragma: allowlist secret
        encoding="utf-8",
    )

    rules = {violation.rule for violation in scan_public_artifact(artifact)}

    assert {"local_or_runtime_path", "raw_holdout_identifier", "credential_value"} <= rules


def test_public_artifact_accepts_safe_scenario_text_and_https_source(tmp_path: Path) -> None:
    artifact = tmp_path / "public"
    data = artifact / "data"
    data.mkdir(parents=True)
    (artifact / "index.html").write_text(
        '<a href="/calendar">Calendar</a><a href="https://docs.example.com/help">Help</a>',
        encoding="utf-8",
    )
    (data / "scenarios.json").write_text(
        json.dumps(
            {
                "schema_version": "dawnstrike-scenarios-public-v1",
                "records": [
                    {
                        "headline": "Revenue grew 5% while margin stayed < 10%",
                        "source_url": "https://news.example.com/story?id=42",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert scan_public_artifact(artifact) == []


def test_public_artifact_rejects_unsafe_scenario_urls_and_raw_markup(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "public"
    data = artifact / "data"
    data.mkdir(parents=True)
    (data / "scenarios.json").write_text(
        json.dumps(
            {
                "schema_version": "dawnstrike-scenarios-public-v1",
                "records": [
                    {
                        "headline": "<img src=x onerror=alert(1)>",
                        "source_url": "javascript:alert(1)",
                    },
                    {
                        "headline": "Credentialed",
                        "source_url": "https://user:pass@example.com/story",  # pragma: allowlist secret
                    },
                    {"headline": "Protocol relative", "source_url": "//example.com/story"},
                    {"headline": "Missing host", "source_url": "https:///story"},
                    {"headline": "Control", "source_url": "https://example.com/\u0000story"},
                ],
            }
        ),
        encoding="utf-8",
    )

    rules = {violation.rule for violation in scan_public_artifact(artifact)}

    assert {
        "scenario_raw_html_markup",
        "scenario_source_url_credentials",
        "scenario_source_url_host_missing",
        "scenario_source_url_malformed",
        "scenario_source_url_not_https",
    } <= rules


def test_public_artifact_rejects_unsafe_static_link_attributes(tmp_path: Path) -> None:
    artifact = tmp_path / "public"
    artifact.mkdir()
    (artifact / "index.html").write_text(
        """
        <a href="javascript:alert(1)">unsafe</a>
        <a href="//example.com/protocol-relative">protocol relative</a>
        <a href="https://user&#58;pass@example.com/private">credentialed</a>
        <form action="http://example.com/submit"></form>
        """,
        encoding="utf-8",
    )

    rules = {violation.rule for violation in scan_public_artifact(artifact)}

    assert {"public_link_credentials", "public_link_not_https"} <= rules
