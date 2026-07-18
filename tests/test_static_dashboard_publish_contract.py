from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLISHER = ROOT / "scripts" / "publish_static_dashboard.ps1"
EOD_BATCH = ROOT / "scripts" / "run_paperops_fleet_eod.bat"


def _dashboard_payload() -> dict[str, object]:
    candidate = os.environ.get("DAWNSTRIKE_STATIC_DASHBOARD_CANDIDATE")
    path = Path(candidate) if candidate else ROOT / "assets" / "dashboard-data.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_static_asset_carries_daily_freshness_and_immutable_evidence_hashes() -> None:
    payload = _dashboard_payload()

    assert payload["schemaVersion"] == "dawnstrike.static-dashboard.v3"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(payload["latestRunDate"]))

    freshness = payload["freshness"]
    assert isinstance(freshness, dict)
    assert freshness["asOfDate"] == payload["latestRunDate"]
    assert freshness["statusAtGeneration"] in {"fresh", "stale"}
    assert freshness["deadlineAt"]

    evidence = payload["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["calendarTruthStatus"] == "passed"
    assert evidence["sourceBarTruthStatus"] == "passed"
    assert re.fullmatch(r"[0-9a-f]{64}", evidence["paperOpsCalendarSha256"])
    assert re.fullmatch(r"[0-9a-f]{64}", evidence["alphaDatabaseSha256"])
    assert evidence["paperOpsRunIds"]


def test_publisher_stages_and_tests_before_any_vercel_deployment() -> None:
    script = PUBLISHER.read_text(encoding="utf-8")

    export = script.index("intraday_scanner.dashboard.static_dashboard_export")
    contract = script.index("tests/test_static_dashboard_publish_contract.py", export)
    rendered_contract = script.index("tests/test_static_dashboard_contract.py", contract)
    deploy = script.index("vercel', 'deploy'", rendered_contract)
    assert export < contract < rendered_contract < deploy
    assert ".candidate.json" in script
    assert "[System.IO.File]::Replace($candidate, $output, $replacementBackup)" in script
    assert "--prod" in script
    assert "--force" not in script
    assert "vercel promote" not in script.lower()
    assert script.count("$ErrorActionPreference = 'Continue'") >= 2
    assert "$commandExitCode = $LASTEXITCODE" in script
    assert "$deployExit = $LASTEXITCODE" in script


def test_publisher_verifies_remote_date_evidence_and_complete_artifact_hash() -> None:
    script = PUBLISHER.read_text(encoding="utf-8")

    assert "Assert-DashboardPayload -Payload $remotePayload" in script
    assert "$remotePayload.evidence.paperOpsCalendarSha256" in script
    assert "$remotePayload.evidence.alphaDatabaseSha256" in script
    assert "$remoteSha256 -ne $artifactSha256" in script
    assert "assets/dashboard-data.json?artifact=$artifactSha256" in script
    assert "no deployment was attempted" in script


def test_publisher_is_derived_output_only_and_has_no_execution_path() -> None:
    script = PUBLISHER.read_text(encoding="utf-8").lower()

    forbidden = (
        "paper_ops run-day",
        "paper_ops reconcile",
        "update signal_outcomes",
        "insert into",
        "submit_order",
        "place_order",
        "brokeradapter",
    )
    assert all(token not in script for token in forbidden)
    assert "static_dashboard_export" in script
    assert "dashboard-data.json" in script


def test_eod_defaults_to_production_but_supports_explicit_safe_overrides() -> None:
    batch = EOD_BATCH.read_text(encoding="utf-8")

    assert (
        'if not defined DAWNSTRIKE_STATIC_DASHBOARD_PUBLISH_MODE set '
        '"DAWNSTRIKE_STATIC_DASHBOARD_PUBLISH_MODE=production"'
    ) in batch
    assert "-PublishTarget %DAWNSTRIKE_STATIC_DASHBOARD_PUBLISH_MODE%" in batch
    assert "stale hosted data was not silently accepted" in batch

    digest = batch.index("intraday_scanner.cli strategy-fleet-telegram")
    publish = batch.index("scripts\\publish_static_dashboard.ps1", digest)
    completion = batch.index("PaperOps fleet EOD completed", publish)
    assert digest < publish < completion


def test_browser_fetches_only_the_canonical_published_asset() -> None:
    javascript = (ROOT / "assets" / "dashboard.js").read_text(encoding="utf-8")

    assert 'fetch("/assets/dashboard-data.json", { cache: "no-store" })' in javascript
