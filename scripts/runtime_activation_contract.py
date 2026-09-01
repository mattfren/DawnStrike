"""Strict evidence and receipt contracts for governed runtime activation.

This module never changes the active runtime, Task Scheduler, or provider state.
It validates exact-SHA CI/SOL evidence, inspects SQLite read-only, and atomically
seals private activation/rollback receipts.  The Windows swap orchestration
lives in ``activate_dawnstrike_runtime.ps1`` and
``rollback_dawnstrike_runtime.ps1``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT_TEXT = str(_REPO_ROOT)
if _REPO_ROOT_TEXT in sys.path:
    sys.path.remove(_REPO_ROOT_TEXT)
sys.path.insert(0, _REPO_ROOT_TEXT)

from intraday_scanner.market_calendar import (  # noqa: E402
    MARKET_TIMEZONE,
    MarketSessionStatus,
    core_session_phase,
    market_session,
    next_market_day,
    session_for_timestamp,
)
from intraday_scanner.storage import migrations as _storage_migrations  # noqa: E402

_EXPECTED_MIGRATIONS = (_REPO_ROOT / "intraday_scanner" / "storage" / "migrations.py").resolve()
if Path(_storage_migrations.__file__).resolve() != _EXPECTED_MIGRATIONS:
    raise RuntimeError("activation contract did not load migrations from the exact candidate root")
CURRENT_SCHEMA_VERSION = _storage_migrations.CURRENT_SCHEMA_VERSION

CI_SCHEMA = "dawnstrike.runtime_activation_ci_evidence.v1"
SOL_SCHEMA = "dawnstrike.runtime_activation_sol_evidence.v1"
ACTIVATION_SCHEMA = "dawnstrike.runtime_activation_receipt.v2"
ACTIVATION_SCHEMA_LEGACY = "dawnstrike.runtime_activation_receipt.v1"
ROLLBACK_SCHEMA = "dawnstrike.runtime_rollback_receipt.v1"

_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACTIVATION_ID = re.compile(r"^[0-9a-f]{24}$")
_MARKET_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_GITHUB_RUN = re.compile(r"^https://github\.com/[^/?#]+/[^/?#]+/actions/runs/[1-9][0-9]*$")
_GITHUB_RUN_EXACT = re.compile(
    r"^https://github\.com/mattfren/DawnStrike/actions/runs/([1-9][0-9]*)$"
)
_GITHUB_REPOSITORY_ID = 1_275_588_712
_GITHUB_WORKFLOW_ID = 325_410_148
_GITHUB_ACTIONS_APP_ID = 15_368
_GITHUB_RELEASE_ACTOR_ID = 274_126_974
_GITHUB_API_ROOT = "https://api.github.com"
_GITHUB_COMMENT_HTML = re.compile(
    r"^https://github\.com/mattfren/DawnStrike/commit/([0-9a-f]{40})#commitcomment-([1-9][0-9]*)$"
)
_GITHUB_COMMENT_API = re.compile(
    r"^https://api\.github\.com/repos/mattfren/DawnStrike/comments/([1-9][0-9]*)$"
)
_CODEX_SHARE_URL = re.compile(r"^https://chatgpt\.com/share/[A-Za-z0-9_-]+$")
_CI_JOB_NAMES = frozenset(
    {
        *(f"Pytest shard {index} of 16" for index in range(16)),
        "Python and public-contract verification",
        "Dependency, static, and SBOM verification",
        "Windows schedule and secret verification",
    }
)
_FORBIDDEN_KEY_PARTS = ("secret", "password", "credential", "private_key", "token")
_MAX_EVIDENCE_AGE = timedelta(days=30)
_REPARSE_POINT = 0x400
_MORNING_START_ET = time(9, 0)
_PUBLIC_BOUNDARY_FILES = (
    "build-manifest.json",
    "release-manifest.json",
    "readiness.json",
    "stage-manifest.json",
    "data/calendar.json.manifest.json",
    "data/performance.json.manifest.json",
    "data/publication-set.json",
)
_FINALIZER_OUTPUT_FILES = (
    "daily-finalize-result.json",
    "non-session-terminal.json",
)
_DEPLOYMENT_OUTPUT_FILES = ("daily-deployment-result.json",)

_CI_KEYS = frozenset(
    {
        "schema_version",
        "candidate_sha",
        "candidate_tree",
        "conclusion",
        "status",
        "head_branch",
        "run_url",
        "checks_total",
        "checks_succeeded",
        "completed_at_utc",
        "research_only",
        "broker_execution_enabled",
        "evidence_sha256",
    }
)
_SOL_KEYS = frozenset(
    {
        "schema_version",
        "candidate_sha",
        "candidate_tree",
        "auditor_model",
        "verdict",
        "critical_findings",
        "high_findings",
        "completed_at_utc",
        "research_only",
        "broker_execution_enabled",
        "evidence_sha256",
    }
)
_SOL_AUTHORIZATION_KEYS = frozenset({"report_sha256", "codex_share_url"})
_ACTIVATION_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "activation_id",
        "market_date",
        "candidate_sha",
        "candidate_tree",
        "previous_sha",
        "previous_tree",
        "ci_evidence_sha256",
        "sol_evidence_sha256",
        "state_backup_id",
        "state_backup_db_sha256",
        "state_schema_version",
        "state_quick_check",
        "rollback_bundle_sha256",
        "task_count",
        "task_contract_sha256",
        "task_definition_contract_sha256",
        "task_action_contract_sha256",
        "task_paths_unchanged",
        "task_enablement_restored",
        "scheduler_backup_name",
        "scheduler_backup_manifest_sha256",
        "runtime_origin_sha256",
        "swap_contract",
        "stage_name",
        "rollback_checkout_name",
        "rollback_bundle_name",
        "prepared_at_utc",
        "completed_at_utc",
        "research_only",
        "broker_execution_enabled",
        "receipt_sha256",
    }
)
_ROLLBACK_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "activation_id",
        "market_date",
        "candidate_sha",
        "candidate_tree",
        "previous_sha",
        "previous_tree",
        "restored_sha",
        "ci_evidence_sha256",
        "sol_evidence_sha256",
        "state_backup_id",
        "state_backup_db_sha256",
        "state_schema_version",
        "state_quick_check",
        "rollback_bundle_sha256",
        "task_count",
        "task_contract_sha256",
        "task_definition_contract_sha256",
        "task_action_contract_sha256",
        "task_paths_unchanged",
        "task_enablement_restored",
        "scheduler_backup_name",
        "scheduler_backup_manifest_sha256",
        "runtime_origin_sha256",
        "swap_contract",
        "prepared_at_utc",
        "completed_at_utc",
        "research_only",
        "broker_execution_enabled",
        "receipt_sha256",
    }
)

# Receipts created for the one-percent sidecar carry an explicit state
# preparation proof and an auxiliary delayed-SIP task disposition.  Keep the
# historical key sets above accepted for older runtimes that do not declare
# the sidecar contract; activation itself requires the extended set whenever
# the candidate declaration is present.
_EXTENDED_RECEIPT_KEYS = frozenset(
    {
        "state_preparation_required",
        "state_preparation_contract",
        "state_preparation_receipt_sha256",
        "state_preparation_after_db_sha256",
        "state_preparation_after_wal_sha256",
        "state_preparation_after_shm_sha256",
        "state_preparation_after_logical_snapshot_sha256",
        "state_preparation_inventory_sha256",
        "state_preparation_backup_id",
        "state_preparation_backup_bundle_path",
        "state_preparation_backup_db_sha256",
        "state_preparation_backup_manifest_sha256",
        "state_preparation_backup_manifest_file_sha256",
        "state_backup_bundle_path",
        "state_backup_logical_snapshot_sha256",
        "state_backup_source_logical_snapshot_sha256",
        "state_backup_manifest_sha256",
        "auxiliary_capture_present",
        "auxiliary_capture_state_before",
        "auxiliary_capture_state_after",
        "auxiliary_capture_action",
        "auxiliary_capture_xml_sha256",
        "auxiliary_capture_xml_file_sha256",
        "auxiliary_capture_definition_contract_sha256",
        "auxiliary_capture_action_contract_sha256",
        "auxiliary_capture_backup_name",
        "auxiliary_capture_backup_manifest_sha256",
        "capture_hardening_receipt_relative_path",
        "capture_hardening_receipt_raw_sha256",
        "capture_hardening_receipt_sha256",
        "capture_hardening_xml_sha256",
        "capture_hardening_action_sha256",
        "capture_hardening_principal_sha256",
        "capture_hardening_trigger_sha256",
        "capture_hardening_settings_sha256",
        "capture_hardening_runner_before_sha256",
        "capture_hardening_runner_target_sha256",
    }
)
_ACTIVATION_RECEIPT_KEYS_EXTENDED = _ACTIVATION_RECEIPT_KEYS | _EXTENDED_RECEIPT_KEYS
_ROLLBACK_RECEIPT_KEYS_EXTENDED = _ROLLBACK_RECEIPT_KEYS | _EXTENDED_RECEIPT_KEYS
_CAPTURE_INTERPRETER_DECLARATION = {
    "capture_interpreter_path": (
        r"C:\Users\MattFields\AppData\Local\Programs\Python\Python313\python.exe"
    ),
    "capture_interpreter_version": "3.13.14",
    "capture_interpreter_sha256": (
        "ef8f51028ac5329641985112f8efb1c2d4c47c86b8011ddf7e6fae21e2b4e5a1"
    ),
    "capture_interpreter_signer_subject": (
        "CN=Python Software Foundation, O=Python Software Foundation, "
        "L=Beaverton, S=Oregon, C=US"
    ),
    "capture_interpreter_signer_thumbprint": (
        "9BA3C2E210C7E8296C5056515BFC0B0BBA78AC48"
    ),
}
_STATE_PREPARATION_DECLARATION_KEYS = frozenset(
    {
        "schema_version",
        "sidecar_contract",
        "sidecar_version",
        "legacy_schema_marker",
        "required_before_activation",
        "research_only",
        "broker_execution_enabled",
        "capture_interpreter_path",
        "capture_interpreter_version",
        "capture_interpreter_sha256",
        "capture_interpreter_signer_subject",
        "capture_interpreter_signer_thumbprint",
    }
)
_STATE_PREPARATION_DECLARATION_LEGACY_KEYS = frozenset(
    {
        "schema_version",
        "sidecar_contract",
        "sidecar_version",
        "legacy_schema_marker",
        "required_before_activation",
        "research_only",
        "broker_execution_enabled",
    }
    | _CAPTURE_INTERPRETER_DECLARATION.keys()
)


class ActivationContractError(ValueError):
    """A supplied activation artifact is invalid or unsafe."""


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def self_hash(payload: Mapping[str, Any], field: str) -> str:
    unsigned = {key: value for key, value in payload.items() if key != field}
    return hashlib.sha256(canonical_json(unsigned)).hexdigest()


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return False
    return path.is_symlink() or bool(attributes & _REPARSE_POINT)


def _assert_no_reparse_components(path: str | Path) -> Path:
    """Reject links/junctions in the complete path before any I/O boundary."""

    absolute = Path(os.path.abspath(Path(path).expanduser()))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        if (os.path.lexists(current) or current.is_symlink()) and _is_reparse_point(current):
            raise ActivationContractError(f"reparse-point path component is forbidden: {current}")
    return absolute


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ActivationContractError(f"duplicate JSON field is forbidden: {key}")
        result[key] = value
    return result


def seal_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a canonical self-hashed evidence object after strict validation."""

    sealed = dict(payload)
    sealed["evidence_sha256"] = self_hash(sealed, "evidence_sha256")
    validate_evidence(sealed, now=None, enforce_age=False)
    return sealed


def validate_evidence(
    payload: Mapping[str, Any],
    *,
    now: datetime | None = None,
    enforce_age: bool = True,
) -> dict[str, Any]:
    """Validate one CI or independent SOL evidence object."""

    _reject_sensitive_keys(payload)
    schema = payload.get("schema_version")
    if schema == CI_SCHEMA:
        _require_exact_keys(payload, _CI_KEYS, "CI evidence")
        _validate_common_evidence(payload, now=now, enforce_age=enforce_age)
        if payload.get("conclusion") != "SUCCESS" or payload.get("status") != "COMPLETED":
            raise ActivationContractError("CI evidence is not a completed success")
        if payload.get("head_branch") != "main":
            raise ActivationContractError("CI evidence is not bound to main")
        run_url = payload.get("run_url")
        if not isinstance(run_url, str) or not _GITHUB_RUN.fullmatch(run_url):
            raise ActivationContractError("CI run URL is invalid")
        total = payload.get("checks_total")
        succeeded = payload.get("checks_succeeded")
        if (
            not isinstance(total, int)
            or isinstance(total, bool)
            or total != 19
            or not isinstance(succeeded, int)
            or isinstance(succeeded, bool)
            or succeeded != total
        ):
            raise ActivationContractError("CI check totals do not prove complete success")
    elif schema == SOL_SCHEMA:
        actual_keys = frozenset(payload)
        if actual_keys not in {_SOL_KEYS, _SOL_KEYS | _SOL_AUTHORIZATION_KEYS}:
            raise ActivationContractError("SOL evidence fields do not match the strict contract")
        _validate_common_evidence(payload, now=now, enforce_age=enforce_age)
        if payload.get("auditor_model") != "gpt-5.6-sol":
            raise ActivationContractError("SOL evidence uses an unapproved auditor model")
        if payload.get("verdict") != "ZERO_CRITICAL_HIGH":
            raise ActivationContractError("SOL evidence verdict is not release-acceptable")
        if payload.get("critical_findings") != 0 or payload.get("high_findings") != 0:
            raise ActivationContractError("SOL evidence contains critical or high findings")
    else:
        raise ActivationContractError("unsupported activation evidence schema")
    return dict(payload)


def validate_evidence_pair(
    ci_path: str | Path,
    sol_path: str | Path,
    *,
    candidate_sha: str,
    candidate_tree: str,
    now: datetime | None = None,
    require_live_github_ci: bool = False,
    require_live_github_owner_authorization: bool = False,
) -> dict[str, Any]:
    """Validate CI and SOL evidence against one exact commit and tree."""

    if not _GIT_SHA.fullmatch(candidate_sha):
        raise ActivationContractError("candidate SHA must be lowercase 40-hex")
    if not _GIT_SHA.fullmatch(candidate_tree):
        raise ActivationContractError("candidate tree must be lowercase 40-hex")
    ci = validate_evidence(_load_object(ci_path), now=now)
    sol = validate_evidence(_load_object(sol_path), now=now)
    if ci.get("schema_version") != CI_SCHEMA or sol.get("schema_version") != SOL_SCHEMA:
        raise ActivationContractError("both CI and SOL evidence are required")
    for label, value in (("CI", ci), ("SOL", sol)):
        if value.get("candidate_sha") != candidate_sha:
            raise ActivationContractError(f"{label} evidence candidate SHA mismatch")
        if value.get("candidate_tree") != candidate_tree:
            raise ActivationContractError(f"{label} evidence candidate tree mismatch")
    result = {
        "status": "PASS",
        "candidate_sha": candidate_sha,
        "candidate_tree": candidate_tree,
        "ci_evidence_sha256": ci["evidence_sha256"],
        "sol_evidence_sha256": sol["evidence_sha256"],
        "research_only": True,
        "broker_execution_enabled": False,
    }
    if require_live_github_ci:
        live = validate_live_github_ci(
            ci,
            candidate_sha=candidate_sha,
            candidate_tree=candidate_tree,
        )
        local_hash = str(result["ci_evidence_sha256"])
        authority_hash = str(live["github_authority_sha256"])
        result["ci_local_evidence_sha256"] = local_hash
        result["ci_github_authority_sha256"] = authority_hash
        result["ci_evidence_sha256"] = hashlib.sha256(
            f"{local_hash}:{authority_hash}".encode()
        ).hexdigest()
    if require_live_github_owner_authorization:
        owner = validate_live_github_owner_authorization(
            sol,
            candidate_sha=candidate_sha,
            candidate_tree=candidate_tree,
        )
        local_hash = str(result["sol_evidence_sha256"])
        authority_hash = str(owner["github_owner_authorization_sha256"])
        result["sol_local_evidence_sha256"] = local_hash
        result["github_owner_authorization_sha256"] = authority_hash
        result["sol_evidence_sha256"] = hashlib.sha256(
            f"{local_hash}:{authority_hash}".encode()
        ).hexdigest()
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise ActivationContractError("GitHub authority endpoint redirected unexpectedly")


def _github_api_object(path: str) -> tuple[Any, str]:
    if not path.startswith("/") or ".." in path or "//" in path:
        raise ActivationContractError("GitHub authority API path is invalid")
    request = urllib.request.Request(
        f"{_GITHUB_API_ROOT}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Dawnstrike-runtime-activation/1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=15) as response:
            if response.status != 200:
                raise ActivationContractError("GitHub authority response is not HTTP 200")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > 2_000_000:
                raise ActivationContractError("GitHub authority response is oversized")
            raw = response.read(2_000_001)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        if isinstance(exc, ActivationContractError):
            raise
        raise ActivationContractError("GitHub authority request failed") from exc
    if len(raw) > 2_000_000:
        raise ActivationContractError("GitHub authority response is oversized")
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActivationContractError("GitHub authority response is invalid JSON") from exc
    if not isinstance(value, (dict, list)):
        raise ActivationContractError("GitHub authority response must be an object or array")
    return value, hashlib.sha256(raw).hexdigest()


def validate_live_github_ci(
    ci: Mapping[str, Any], *, candidate_sha: str, candidate_tree: str
) -> dict[str, Any]:
    """Reprove exact CI from GitHub's live, pinned Actions authority."""

    match = _GITHUB_RUN_EXACT.fullmatch(str(ci.get("run_url") or ""))
    if match is None:
        raise ActivationContractError("CI evidence is not from the governed GitHub repository")
    run_id = int(match.group(1))
    run, run_raw_sha = _github_api_object(
        f"/repos/mattfren/DawnStrike/actions/runs/{run_id}"
    )
    jobs, jobs_raw_sha = _github_api_object(
        f"/repos/mattfren/DawnStrike/actions/runs/{run_id}/jobs?per_page=100"
    )
    commit, commit_raw_sha = _github_api_object(
        f"/repos/mattfren/DawnStrike/git/commits/{candidate_sha}"
    )
    if not isinstance(run, dict) or not isinstance(jobs, dict) or not isinstance(commit, dict):
        raise ActivationContractError("live GitHub CI responses are not objects")
    exact_run = {
        "id": run_id,
        "workflow_id": _GITHUB_WORKFLOW_ID,
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "run_attempt": 1,
        "head_branch": "main",
        "head_sha": candidate_sha,
        "status": "completed",
        "conclusion": "success",
        "repository_id": _GITHUB_REPOSITORY_ID,
        "head_repository_id": _GITHUB_REPOSITORY_ID,
        "actor_id": _GITHUB_RELEASE_ACTOR_ID,
        "triggering_actor_id": _GITHUB_RELEASE_ACTOR_ID,
    }
    observed_run = {
        "id": run.get("id"),
        "workflow_id": run.get("workflow_id"),
        "path": run.get("path"),
        "event": run.get("event"),
        "run_attempt": run.get("run_attempt"),
        "head_branch": run.get("head_branch"),
        "head_sha": run.get("head_sha"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "repository_id": (run.get("repository") or {}).get("id"),
        "head_repository_id": (run.get("head_repository") or {}).get("id"),
        "actor_id": (run.get("actor") or {}).get("id"),
        "triggering_actor_id": (run.get("triggering_actor") or {}).get("id"),
    }
    if observed_run != exact_run:
        raise ActivationContractError("live GitHub CI run identity is invalid")
    if ci.get("completed_at_utc") != run.get("updated_at"):
        raise ActivationContractError("CI evidence completion time does not match GitHub")
    job_rows = jobs.get("jobs")
    if jobs.get("total_count") != 19 or not isinstance(job_rows, list) or len(job_rows) != 19:
        raise ActivationContractError("live GitHub CI job count is not exact")
    names = [item.get("name") for item in job_rows if isinstance(item, dict)]
    if len(names) != 19 or len(set(names)) != 19 or set(names) != _CI_JOB_NAMES:
        raise ActivationContractError("live GitHub CI job names are not exact")
    if any(
        item.get("status") != "completed"
        or item.get("conclusion") != "success"
        or item.get("run_attempt") != 1
        for item in job_rows
        if isinstance(item, dict)
    ):
        raise ActivationContractError("live GitHub CI contains an unsuccessful job")
    if (
        commit.get("sha") != candidate_sha
        or (commit.get("tree") or {}).get("sha") != candidate_tree
    ):
        raise ActivationContractError("GitHub candidate commit/tree identity mismatch")
    authority = {
        "repository_id": _GITHUB_REPOSITORY_ID,
        "workflow_id": _GITHUB_WORKFLOW_ID,
        "actions_app_id": _GITHUB_ACTIONS_APP_ID,
        "run_id": run_id,
        "run_attempt": 1,
        "run_response_sha256": run_raw_sha,
        "jobs_response_sha256": jobs_raw_sha,
        "commit_response_sha256": commit_raw_sha,
    }
    return {
        **authority,
        "github_authority_sha256": hashlib.sha256(
            json.dumps(authority, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _owner_authorization_body(
    sol: Mapping[str, Any], *, candidate_sha: str, candidate_tree: str
) -> str:
    """Build the exact body that the owner must publish in the commit comment.

    This is an authorization binding, not a cryptographic identity assertion:
    the remote OWNER comment authorizes an independently reviewed report whose
    content remains identified by its report hash and immutable Codex share.
    """

    report_sha256 = sol.get("report_sha256")
    codex_share_url = sol.get("codex_share_url")
    if not _SHA256.fullmatch(str(report_sha256 or "")):
        raise ActivationContractError("SOL report SHA-256 is required for owner authorization")
    if not isinstance(codex_share_url, str) or not _CODEX_SHARE_URL.fullmatch(codex_share_url):
        raise ActivationContractError("SOL Codex share URL is not immutable")
    body = {
        "authorization": "OWNER_RELEASE_AUTHORIZATION",
        "auditor_model": sol.get("auditor_model"),
        "broker_execution_enabled": False,
        "candidate_sha": candidate_sha,
        "candidate_tree": candidate_tree,
        "codex_share_url": codex_share_url,
        "critical_findings": 0,
        "high_findings": 0,
        "report_sha256": report_sha256,
        "research_only": True,
        "verdict": "ZERO_CRITICAL_HIGH",
    }
    # Commit-comment bodies are JSON values, not files; omit the file-oriented
    # trailing newline used by ``canonical_json`` while retaining its exact
    # key ordering and separator rules.
    return canonical_json(body).decode("utf-8").rstrip("\n")


def validate_live_github_owner_authorization(
    sol: Mapping[str, Any], *, candidate_sha: str, candidate_tree: str
) -> dict[str, Any]:
    """Require a live, exact-commit OWNER comment for the SOL report.

    GitHub's API is contacted without proxies and redirects.  The returned
    authority hash binds the raw commit/comment responses into the activation
    evidence hash, so a locally fabricated SOL cannot authorize activation.
    This proves owner authorization of an independently reviewed report; it
    does not prove cryptographic identity of the Sol model.
    """

    if not _GIT_SHA.fullmatch(candidate_sha) or not _GIT_SHA.fullmatch(candidate_tree):
        raise ActivationContractError("owner authorization candidate identity is invalid")
    expected_body = _owner_authorization_body(
        sol, candidate_sha=candidate_sha, candidate_tree=candidate_tree
    )
    comments, comments_raw_sha = _github_api_object(
        f"/repos/mattfren/DawnStrike/commits/{candidate_sha}/comments?per_page=100"
    )
    commit, commit_raw_sha = _github_api_object(
        f"/repos/mattfren/DawnStrike/git/commits/{candidate_sha}"
    )
    if (
        not isinstance(commit, dict)
        or commit.get("sha") != candidate_sha
        or (commit.get("tree") or {}).get("sha") != candidate_tree
    ):
        raise ActivationContractError("GitHub owner authorization commit/tree mismatch")
    if not isinstance(comments, list):
        raise ActivationContractError("GitHub commit comments response is not an array")
    matches: list[dict[str, Any]] = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        if comment.get("body") == expected_body:
            matches.append(comment)
    if len(matches) != 1:
        raise ActivationContractError("GitHub owner authorization comment is not unique")
    comment = matches[0]
    comment_id = comment.get("id")
    if type(comment_id) is not int or comment_id < 1:
        raise ActivationContractError("GitHub owner authorization comment id is invalid")
    html_url = comment.get("html_url")
    expected_html_url = (
        f"https://github.com/mattfren/DawnStrike/commit/{candidate_sha}"
        f"#commitcomment-{comment_id}"
    )
    if html_url != expected_html_url or _GITHUB_COMMENT_HTML.fullmatch(str(html_url)) is None:
        raise ActivationContractError("GitHub owner authorization comment URL is invalid")
    api_url = comment.get("url")
    expected_api_url = f"https://api.github.com/repos/mattfren/DawnStrike/comments/{comment_id}"
    if api_url != expected_api_url or _GITHUB_COMMENT_API.fullmatch(str(api_url)) is None:
        raise ActivationContractError("GitHub owner authorization API URL is invalid")
    if comment.get("commit_id") != candidate_sha:
        raise ActivationContractError("GitHub owner authorization comment commit is invalid")
    if comment.get("author_association") != "OWNER":
        raise ActivationContractError("GitHub owner authorization author is not OWNER")
    user = comment.get("user")
    if not isinstance(user, dict) or user.get("id") != _GITHUB_RELEASE_ACTOR_ID:
        raise ActivationContractError("GitHub owner authorization actor is invalid")
    created = comment.get("created_at")
    updated = comment.get("updated_at")
    if not isinstance(created, str) or created != updated:
        raise ActivationContractError("GitHub owner authorization timestamps are not immutable")
    try:
        _parse_utc(created)
    except ActivationContractError as exc:
        raise ActivationContractError(
            "GitHub owner authorization timestamp is invalid"
        ) from exc
    if comment.get("body") != expected_body:
        raise ActivationContractError("GitHub owner authorization body is not canonical")
    authority = {
        "repository_id": _GITHUB_REPOSITORY_ID,
        "owner_actor_id": _GITHUB_RELEASE_ACTOR_ID,
        "candidate_sha": candidate_sha,
        "candidate_tree": candidate_tree,
        "comment_id": comment_id,
        "comment_html_url": html_url,
        "comment_api_url": api_url,
        "comments_response_sha256": comments_raw_sha,
        "commit_response_sha256": commit_raw_sha,
    }
    return {
        **authority,
        "github_owner_authorization_sha256": hashlib.sha256(
            canonical_json(authority)
        ).hexdigest(),
    }


def inspect_state(db_path: str | Path) -> dict[str, Any]:
    """Inspect the durable database without creating or migrating it."""

    path = _assert_no_reparse_components(db_path)
    try:
        before = path.lstat()
    except OSError as exc:
        raise ActivationContractError("durable state database is missing or unsafe") from exc
    if _is_reparse_point(path) or path.name != "shadow_real.sqlite" or not path.is_file():
        raise ActivationContractError("durable state database is missing or unsafe")
    uri = f"file:{quote(path.as_posix(), safe='/:')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=30)
        try:
            connection.execute("PRAGMA query_only = ON")
            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            if table is None:
                raise ActivationContractError("Dawnstrike schema_version table is missing")
            row = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()
            if row is None or row[0] is None:
                raise ActivationContractError("Dawnstrike schema_version table is empty")
            schema_version = int(row[0])
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ActivationContractError("SQLite state inspection failed") from exc
    if quick_check != "ok":
        raise ActivationContractError("SQLite quick_check is not ok")
    if schema_version != CURRENT_SCHEMA_VERSION:
        raise ActivationContractError(
            "durable state schema does not exactly match the candidate runtime"
        )
    _assert_no_reparse_components(path)
    try:
        after = path.lstat()
    except OSError as exc:
        raise ActivationContractError("durable state database changed during inspection") from exc
    if (
        _is_reparse_point(path)
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise ActivationContractError("durable state database changed during inspection")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    _assert_no_reparse_components(path)
    try:
        final = path.lstat()
    except OSError as exc:
        raise ActivationContractError("durable state database changed during inspection") from exc
    if (
        _is_reparse_point(path)
        or final.st_size != before.st_size
        or final.st_mtime_ns != before.st_mtime_ns
    ):
        raise ActivationContractError("durable state database changed during inspection")
    return {
        "status": "PASS",
        "database_name": path.name,
        "main_file_sha256": digest,
        "main_file_hash_semantics": "observational_main_database_only_wal_may_be_pending",
        "schema_version": schema_version,
        "candidate_schema_version": CURRENT_SCHEMA_VERSION,
        "quick_check": quick_check,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def seal_receipt(payload: Mapping[str, Any], output_path: str | Path) -> dict[str, Any]:
    """Validate and atomically write one activation or rollback receipt."""

    sealed = dict(payload)
    sealed["receipt_sha256"] = self_hash(sealed, "receipt_sha256")
    validate_receipt(sealed)
    _atomic_write(Path(output_path), sealed)
    return sealed


def validate_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a self-hashed private activation/rollback receipt."""

    _reject_sensitive_keys(payload)
    schema = payload.get("schema_version")
    if schema not in {ACTIVATION_SCHEMA, ACTIVATION_SCHEMA_LEGACY, ROLLBACK_SCHEMA}:
        raise ActivationContractError("unsupported runtime receipt schema")
    extended = "state_preparation_contract" in payload
    expected_keys = (
        _ACTIVATION_RECEIPT_KEYS_EXTENDED
        if schema in {ACTIVATION_SCHEMA, ACTIVATION_SCHEMA_LEGACY} and extended
        else _ROLLBACK_RECEIPT_KEYS_EXTENDED
        if schema == ROLLBACK_SCHEMA and extended
        else _ACTIVATION_RECEIPT_KEYS
        if schema in {ACTIVATION_SCHEMA, ACTIVATION_SCHEMA_LEGACY}
        else _ROLLBACK_RECEIPT_KEYS
    )
    _require_exact_keys(payload, expected_keys, "runtime receipt")
    if payload.get("receipt_sha256") != self_hash(payload, "receipt_sha256"):
        raise ActivationContractError("runtime receipt self-hash mismatch")
    if not _ACTIVATION_ID.fullmatch(str(payload.get("activation_id") or "")):
        raise ActivationContractError("runtime receipt activation id is invalid")
    if not _GIT_SHA.fullmatch(str(payload.get("candidate_sha") or "")):
        raise ActivationContractError("runtime receipt candidate SHA is invalid")
    if not _GIT_SHA.fullmatch(str(payload.get("candidate_tree") or "")):
        raise ActivationContractError("runtime receipt candidate tree is invalid")
    if not _GIT_SHA.fullmatch(str(payload.get("previous_sha") or "")):
        raise ActivationContractError("runtime receipt previous SHA is invalid")
    if not _GIT_SHA.fullmatch(str(payload.get("previous_tree") or "")):
        raise ActivationContractError("runtime receipt previous tree is invalid")
    market_date = str(payload.get("market_date") or "")
    if not _MARKET_DATE.fullmatch(market_date):
        raise ActivationContractError("runtime receipt market date is invalid")
    try:
        if date.fromisoformat(market_date).isoformat() != market_date:
            raise ValueError
    except ValueError as exc:
        raise ActivationContractError("runtime receipt market date is invalid") from exc
    for field in (
        "ci_evidence_sha256",
        "sol_evidence_sha256",
        "state_backup_db_sha256",
        "rollback_bundle_sha256",
        "task_contract_sha256",
        "task_definition_contract_sha256",
        "task_action_contract_sha256",
        "runtime_origin_sha256",
        "scheduler_backup_manifest_sha256",
    ):
        if not _SHA256.fullmatch(str(payload.get(field) or "")):
            raise ActivationContractError(f"runtime receipt {field} is invalid")
    if payload.get("state_quick_check") != "ok":
        raise ActivationContractError("runtime receipt state quick_check is invalid")
    schema_version = payload.get("state_schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != CURRENT_SCHEMA_VERSION
    ):
        raise ActivationContractError("runtime receipt state schema is incompatible")
    if payload.get("task_count") != 5:
        raise ActivationContractError("runtime receipt task count is invalid")
    if payload.get("task_paths_unchanged") is not True:
        raise ActivationContractError("runtime receipt does not preserve task paths")
    if payload.get("research_only") is not True:
        raise ActivationContractError("runtime receipt is not research-only")
    if payload.get("broker_execution_enabled") is not False:
        raise ActivationContractError("runtime receipt enables broker execution")
    if extended:
        _validate_extended_receipt(payload)
    backup_id = payload.get("state_backup_id")
    expected_backup_id = "runtime-activation-" + str(payload.get("activation_id"))
    if not isinstance(backup_id, str) or backup_id != expected_backup_id:
        raise ActivationContractError("runtime receipt backup id is invalid")
    scheduler_backup_name = payload.get("scheduler_backup_name")
    if not isinstance(scheduler_backup_name, str) or not re.fullmatch(
        r"runtime-(?:activation|rollback)-[0-9a-f]{24}", scheduler_backup_name
    ):
        raise ActivationContractError("runtime receipt scheduler backup name is invalid")
    activation_id = str(payload.get("activation_id"))
    if not scheduler_backup_name.endswith("-" + activation_id):
        raise ActivationContractError("runtime receipt scheduler backup id is invalid")
    if schema == ACTIVATION_SCHEMA and scheduler_backup_name != (
        "runtime-activation-" + activation_id
    ):
        raise ActivationContractError("activation scheduler backup name is invalid")
    prepared_at = _parse_utc(payload.get("prepared_at_utc"))
    completed_at = payload.get("completed_at_utc")
    if schema == ACTIVATION_SCHEMA:
        if payload.get("status") not in {"PREPARED", "COMPLETE"}:
            raise ActivationContractError("activation receipt status is invalid")
        if payload.get("swap_contract") != "same_volume_two_rename_with_immediate_restore":
            raise ActivationContractError("activation swap contract is invalid")
        if payload.get("stage_name") != (
            "dawnstrike-runtime.stage-" + str(payload.get("activation_id"))
        ):
            raise ActivationContractError("activation stage name is invalid")
        if payload.get("rollback_checkout_name") != "previous-runtime":
            raise ActivationContractError("activation rollback checkout name is invalid")
        if payload.get("rollback_bundle_name") != "previous-runtime.bundle":
            raise ActivationContractError("activation rollback bundle name is invalid")
        if payload.get("status") == "PREPARED" and completed_at is not None:
            raise ActivationContractError("prepared activation receipt has a completion time")
        if (
            payload.get("status") == "PREPARED"
            and payload.get("task_enablement_restored") is not False
        ):
            raise ActivationContractError("prepared activation receipt has invalid task state")
        if payload.get("status") == "COMPLETE":
            if _parse_utc(completed_at) < prepared_at:
                raise ActivationContractError("activation completion predates preparation")
            if payload.get("task_enablement_restored") is not True:
                raise ActivationContractError("complete activation did not restore task enablement")
    else:
        if payload.get("status") != "ROLLED_BACK":
            raise ActivationContractError("rollback receipt status is invalid")
        if payload.get("restored_sha") != payload.get("previous_sha"):
            raise ActivationContractError("rollback receipt restored SHA mismatch")
        if payload.get("swap_contract") != "same_volume_two_rename_with_immediate_restore":
            raise ActivationContractError("rollback swap contract is invalid")
        if payload.get("task_enablement_restored") is not True:
            raise ActivationContractError("rollback did not restore task enablement")
        if _parse_utc(completed_at) < prepared_at:
            raise ActivationContractError("rollback completion predates preparation")
    return dict(payload)


def _validate_extended_receipt(payload: Mapping[str, Any]) -> None:
    """Validate the sidecar and auxiliary-task portion of a runtime receipt."""

    if payload.get("state_preparation_required") is not True:
        raise ActivationContractError("sidecar runtime receipt does not require state preparation")
    if payload.get("state_preparation_contract") != ("dawnstrike.account_capture_trial_sidecar.v1"):
        raise ActivationContractError("runtime state-preparation contract is invalid")
    for field in (
        "state_preparation_receipt_sha256",
        "state_preparation_after_db_sha256",
        "state_preparation_after_wal_sha256",
        "state_preparation_after_shm_sha256",
        "state_preparation_after_logical_snapshot_sha256",
        "state_preparation_inventory_sha256",
        "auxiliary_capture_xml_sha256",
        "auxiliary_capture_xml_file_sha256",
        "auxiliary_capture_definition_contract_sha256",
        "auxiliary_capture_action_contract_sha256",
        "auxiliary_capture_backup_manifest_sha256",
    ):
        if not _SHA256.fullmatch(str(payload.get(field) or "")):
            raise ActivationContractError(f"runtime receipt {field} is invalid")
    for field in (
        "state_preparation_backup_db_sha256",
        "state_preparation_backup_manifest_sha256",
        "state_preparation_backup_manifest_file_sha256",
        "state_backup_logical_snapshot_sha256",
        "state_backup_source_logical_snapshot_sha256",
        "state_backup_manifest_sha256",
    ):
        if not _SHA256.fullmatch(str(payload.get(field) or "")):
            raise ActivationContractError(f"runtime receipt {field} is invalid")
    backup_id = payload.get("state_preparation_backup_id")
    backup_path = payload.get("state_preparation_backup_bundle_path")
    if (
        not isinstance(backup_id, str)
        or not re.fullmatch(r"state-preparation-[0-9a-f]{16}-[0-9a-f]{16}", backup_id)
        or not isinstance(backup_path, str)
        or not Path(backup_path).is_absolute()
        or Path(backup_path).name != backup_id
    ):
        raise ActivationContractError(
            "runtime receipt state-preparation backup identity is invalid"
        )
    direct_backup_id = payload.get("state_backup_id")
    direct_backup_path = payload.get("state_backup_bundle_path")
    if (
        not isinstance(direct_backup_id, str)
        or not re.fullmatch(r"runtime-activation-[0-9a-f]{24}", direct_backup_id)
        or not isinstance(direct_backup_path, str)
        or not Path(direct_backup_path).is_absolute()
        or Path(direct_backup_path).name != direct_backup_id
    ):
        raise ActivationContractError("runtime receipt durable-state backup identity is invalid")
    if payload.get("auxiliary_capture_present") not in {True, False}:
        raise ActivationContractError("runtime receipt auxiliary capture presence is invalid")
    before = payload.get("auxiliary_capture_state_before")
    after = payload.get("auxiliary_capture_state_after")
    action = payload.get("auxiliary_capture_action")
    if payload.get("auxiliary_capture_present") is True:
        relative = payload.get("capture_hardening_receipt_relative_path")
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not relative.lower().endswith(".json")
        ):
            raise ActivationContractError("runtime receipt hardening receipt path is invalid")
        for field in (
            "capture_hardening_receipt_raw_sha256",
            "capture_hardening_receipt_sha256",
            "capture_hardening_xml_sha256",
            "capture_hardening_action_sha256",
            "capture_hardening_principal_sha256",
            "capture_hardening_trigger_sha256",
            "capture_hardening_settings_sha256",
            "capture_hardening_runner_before_sha256",
            "capture_hardening_runner_target_sha256",
        ):
            if not _SHA256.fullmatch(str(payload.get(field) or "")):
                raise ActivationContractError(f"runtime receipt {field} is invalid")
        if before not in {"Ready", "Disabled"}:
            raise ActivationContractError("runtime receipt auxiliary capture state is invalid")
        if payload.get("schema_version") == ACTIVATION_SCHEMA:
            if after != "Disabled" or action != "DISABLED_UNTIL_EXACT_SHA_REBIND":
                raise ActivationContractError("activation auxiliary capture disposition is invalid")
        elif after not in {"Ready", "Disabled"} or action != "RESTORED_EXACT":
            raise ActivationContractError("rollback auxiliary capture disposition is invalid")
        backup_name = payload.get("auxiliary_capture_backup_name")
        if not isinstance(backup_name, str) or not re.fullmatch(
            r"runtime-(?:activation|rollback)-[0-9a-f]{24}", backup_name
        ):
            raise ActivationContractError("runtime receipt auxiliary backup name is invalid")
    else:
        if before != "ABSENT" or after != "ABSENT" or action != "ABSENT_ALLOWED":
            raise ActivationContractError(
                "runtime receipt has an inconsistent absent auxiliary task"
            )
        if payload.get("auxiliary_capture_backup_name") != "NONE":
            raise ActivationContractError("absent auxiliary task must not have a backup name")


def load_receipt(path: str | Path) -> dict[str, Any]:
    return validate_receipt(_load_object(path))


def _validate_common_evidence(
    payload: Mapping[str, Any],
    *,
    now: datetime | None,
    enforce_age: bool,
) -> None:
    if not _GIT_SHA.fullmatch(str(payload.get("candidate_sha") or "")):
        raise ActivationContractError("evidence candidate SHA is invalid")
    if not _GIT_SHA.fullmatch(str(payload.get("candidate_tree") or "")):
        raise ActivationContractError("evidence candidate tree is invalid")
    if payload.get("research_only") is not True:
        raise ActivationContractError("activation evidence is not research-only")
    if payload.get("broker_execution_enabled") is not False:
        raise ActivationContractError("activation evidence enables broker execution")
    if payload.get("evidence_sha256") != self_hash(payload, "evidence_sha256"):
        raise ActivationContractError("activation evidence self-hash mismatch")
    completed = _parse_utc(payload.get("completed_at_utc"))
    if enforce_age:
        reference = (now or datetime.now(UTC)).astimezone(UTC)
        if completed > reference + timedelta(minutes=5):
            raise ActivationContractError("activation evidence completion is in the future")
        if reference - completed > _MAX_EVIDENCE_AGE:
            raise ActivationContractError("activation evidence is older than 30 days")


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ActivationContractError("evidence completion must be RFC3339 UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ActivationContractError("evidence completion is invalid") from exc
    return parsed.astimezone(UTC)


def _require_exact_keys(payload: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    actual = frozenset(payload)
    if actual != expected:
        raise ActivationContractError(f"{label} fields do not match the strict contract")


def validate_state_preparation_declaration(
    payload: Mapping[str, Any],
    *,
    require_interpreter_identity: bool = False,
) -> dict[str, Any]:
    """Validate the candidate's exact additive sidecar declaration.

    PowerShell's ``ConvertFrom-Json`` keeps the last duplicate property.  The
    activation boundary therefore validates the raw JSON through this strict
    Python loader before PowerShell consumes any fields.
    """

    expected_keys = (
        _STATE_PREPARATION_DECLARATION_KEYS
        if require_interpreter_identity
        else _STATE_PREPARATION_DECLARATION_LEGACY_KEYS
    )
    _require_exact_keys(payload, expected_keys, "state preparation declaration")
    if (
        payload.get("schema_version") != "dawnstrike.state_preparation_contract.v1"
        or payload.get("sidecar_contract") != "dawnstrike.account_capture_trial_sidecar.v1"
        or type(payload.get("sidecar_version")) is not int
        or payload.get("sidecar_version") != 1
        or type(payload.get("legacy_schema_marker")) is not int
        or payload.get("legacy_schema_marker") != 30
        or type(payload.get("required_before_activation")) is not bool
        or payload.get("required_before_activation") is not True
        or any(
            payload.get(field) != expected
            for field, expected in _CAPTURE_INTERPRETER_DECLARATION.items()
        )
        or type(payload.get("research_only")) is not bool
        or payload.get("research_only") is not True
        or type(payload.get("broker_execution_enabled")) is not bool
        or payload.get("broker_execution_enabled") is not False
    ):
        raise ActivationContractError("state preparation declaration violates the sidecar contract")
    if not require_interpreter_identity:
        return dict(payload)
    interpreter_path = payload.get("capture_interpreter_path")
    if (
        not isinstance(interpreter_path, str)
        or not os.path.isabs(interpreter_path)
        or "\n" in interpreter_path
        or "\r" in interpreter_path
        or not interpreter_path.lower().endswith("\\python.exe")
    ):
        raise ActivationContractError("capture interpreter path is invalid")
    if not isinstance(payload.get("capture_interpreter_version"), str) or not re.fullmatch(
        r"3\.13\.\d+", payload["capture_interpreter_version"]
    ):
        raise ActivationContractError("capture interpreter version is invalid")
    if not _SHA256.fullmatch(str(payload.get("capture_interpreter_sha256") or "")):
        raise ActivationContractError("capture interpreter SHA-256 is invalid")
    if payload.get("capture_interpreter_signer_subject") != (
        "CN=Python Software Foundation, O=Python Software Foundation, L=Beaverton, S=Oregon, C=US"
    ):
        raise ActivationContractError("capture interpreter signer subject is invalid")
    if not re.fullmatch(
        r"[0-9A-F]{40}", str(payload.get("capture_interpreter_signer_thumbprint") or "")
    ):
        raise ActivationContractError("capture interpreter signer thumbprint is invalid")
    return dict(payload)


def _reject_sensitive_keys(value: object, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower()
            if any(part in normalized for part in _FORBIDDEN_KEY_PARTS):
                raise ActivationContractError(f"sensitive field is forbidden at {path}")
            _reject_sensitive_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_sensitive_keys(item, f"{path}[{index}]")


def _load_object(path: str | Path) -> dict[str, Any]:
    source = _assert_no_reparse_components(path)
    try:
        before = source.lstat()
    except OSError as exc:
        raise ActivationContractError("activation JSON input is missing or unsafe") from exc
    if _is_reparse_point(source) or not source.is_file():
        raise ActivationContractError("activation JSON input is missing or unsafe")
    try:
        raw = source.read_bytes()
        _assert_no_reparse_components(source)
        after = source.lstat()
        if (
            _is_reparse_point(source)
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
        ):
            raise ActivationContractError("activation JSON input changed during read")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except ActivationContractError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActivationContractError("activation JSON input is invalid") from exc
    if not isinstance(value, dict):
        raise ActivationContractError("activation JSON input must be an object")
    return value


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path = _assert_no_reparse_components(path)
    _assert_no_reparse_components(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_reparse_components(path.parent)
    if os.path.lexists(path):
        raise ActivationContractError("activation output already exists")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        _assert_no_reparse_components(temporary)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json(payload))
            handle.flush()
            os.fsync(handle.fileno())
        _assert_no_reparse_components(path.parent)
        _assert_no_reparse_components(path)
        os.link(temporary, path)
        _assert_no_reparse_components(path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_summary(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


def activation_boundary(
    market_date: str,
    *,
    now: datetime,
    state_root: str | Path | None = None,
    runtime_root: str | Path | None = None,
) -> dict[str, Any]:
    """Authorize a pre-Morning activation for exactly one governed session.

    This is deliberately read-only.  Activation is a release-boundary
    operation, so a caller may only name the session that is currently in the
    overnight pre-Morning window (or the next session after a completed
    session/closed day).  Existing authoritative finalizer/public evidence for
    that target date is a hard stop: replacing the runtime after that evidence
    exists would make the runtime SHA disagree with frozen daily artifacts.
    """

    errors: list[str] = []
    normalized = str(market_date).strip()
    try:
        requested = date.fromisoformat(normalized)
    except ValueError:
        requested = None
        errors.append("market_date_invalid")
    if requested is not None and requested.isoformat() != normalized:
        errors.append("market_date_invalid")

    observed = now
    if observed.tzinfo is None or observed.utcoffset() is None:
        errors.append("activation_clock_must_include_timezone")
        observed = observed.replace(tzinfo=UTC)
    observed = observed.astimezone(UTC)
    current: dict[str, Any] = {}
    expected_date: date | None = None
    if requested is not None and not errors:
        try:
            session = session_for_timestamp(observed)
            current = session.to_dict()
            market_day = date.fromisoformat(session.market_date)
            local_et = observed.astimezone(MARKET_TIMEZONE)
            morning = datetime.combine(
                market_day,
                _MORNING_START_ET,
                tzinfo=MARKET_TIMEZONE,
            )
            if session.status != MarketSessionStatus.CLOSED and local_et < morning:
                expected_date = market_day
                window = "PRE_MORNING"
            elif core_session_phase(observed) in {"after_core_session", "market_closed"}:
                anchor = market_day + timedelta(days=1)
                expected_date = next_market_day(anchor)
                window = "POST_SESSION_NEXT_SESSION"
            else:
                anchor = market_day + timedelta(days=1)
                expected_date = next_market_day(anchor)
                window = "ACTIVE_SESSION_BLOCKED"
                errors.append("activation_requires_next_session_pre_morning_window")
        except Exception as exc:
            errors.append(f"calendar_unavailable:{type(exc).__name__}")
            window = "UNAVAILABLE"
    else:
        window = "UNAVAILABLE"

    if requested is not None and not errors:
        try:
            target = market_session(requested)
            if not target.is_trading_day:
                errors.append("activation_target_is_not_open_session")
        except Exception as exc:
            errors.append(f"calendar_target_unavailable:{type(exc).__name__}")
        if expected_date is None or requested != expected_date:
            errors.append("activation_target_is_not_governed_next_session")

    artifacts: list[str] = []
    artifact_errors: list[str] = []
    if requested is not None and state_root is not None:
        state = Path(state_root)
        artifacts, artifact_errors = _existing_authoritative_activation_artifacts(
            state,
            runtime_root=Path(runtime_root) if runtime_root is not None else None,
            market_date=normalized,
        )
        errors.extend(artifact_errors)
        if artifacts:
            errors.append("target_date_has_authoritative_finalizer_or_public_artifacts")

    return {
        "status": "PASS" if not errors else "BLOCKED",
        "ready": not errors,
        "market_date": normalized,
        "current_market_date": current.get("market_date"),
        "current_session_status": current.get("status"),
        "current_session_reason": current.get("reason"),
        "expected_market_date": expected_date.isoformat() if expected_date else None,
        "window": window,
        "calendar_id": current.get("calendar_id"),
        "calendar_authority": current.get("calendar_authority"),
        "authoritative_artifacts": artifacts,
        "errors": list(dict.fromkeys(errors)),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _existing_authoritative_activation_artifacts(
    state_root: Path,
    *,
    runtime_root: Path | None,
    market_date: str,
) -> tuple[list[str], list[str]]:
    """Read target-date finalizer/public evidence without changing state."""

    artifacts: list[str] = []
    errors: list[str] = []
    output_root = state_root / "outputs" / "daily_finalize" / market_date
    for name in _FINALIZER_OUTPUT_FILES:
        path = output_root / name
        if os.path.lexists(path):
            if not path.is_file() or _is_reparse_point(path):
                errors.append(f"unsafe_finalizer_artifact:{path.name}")
            else:
                artifacts.append(str(path))

    database = state_root / "shadow_real.sqlite"
    if database.exists():
        try:
            artifacts.extend(_target_database_artifacts(database, market_date))
        except (OSError, sqlite3.Error):
            errors.append("authoritative_state_unreadable")

    if runtime_root is not None:
        deployment_root = runtime_root / "build"
        for name in _DEPLOYMENT_OUTPUT_FILES:
            path = deployment_root / name
            if not os.path.lexists(path):
                continue
            try:
                deployment = _read_boundary_object(path)
            except (ActivationContractError, OSError):
                errors.append(f"unreadable_deployment_artifact:{name}")
                artifacts.append(str(path))
                continue
            deployment_date = _artifact_market_date(deployment)
            if not deployment_date:
                errors.append("deployment_market_date_missing")
                artifacts.append(str(path))
            elif deployment_date == market_date:
                artifacts.append(str(path))

        public_root = runtime_root / "build" / "public"
        parsed: dict[str, dict[str, Any]] = {}
        for relative in _PUBLIC_BOUNDARY_FILES:
            path = public_root / relative
            if not os.path.lexists(path):
                continue
            try:
                parsed[relative] = _read_boundary_object(path)
            except (ActivationContractError, OSError):
                errors.append(f"unreadable_public_artifact:{relative}")
        build_date = str((parsed.get("build-manifest.json") or {}).get("market_date") or "")
        if "build-manifest.json" in parsed and not build_date:
            errors.append("public_build_market_date_missing")
            artifacts.append(str(public_root / "build-manifest.json"))
        for relative, payload in parsed.items():
            if build_date == market_date or _artifact_market_date(payload) == market_date:
                artifacts.append(str(public_root / relative))

    return list(dict.fromkeys(artifacts)), list(dict.fromkeys(errors))


def _target_database_artifacts(database: Path, market_date: str) -> list[str]:
    path = _assert_no_reparse_components(database)
    uri = "file:" + quote(str(path), safe="/\\:") + "?mode=ro"
    found: list[str] = []
    with sqlite3.connect(uri, uri=True) as connection:
        connection.execute("PRAGMA query_only = ON")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        queries = {
            "daily_finalize_runs": (
                "SELECT 1 FROM daily_finalize_runs WHERE market_date = ? LIMIT 1"
            ),
            "daily_runs": "SELECT 1 FROM daily_runs WHERE market_date = ? LIMIT 1",
            "public_snapshot_manifests": (
                "SELECT 1 FROM public_snapshot_manifests WHERE market_date = ? LIMIT 1"
            ),
            "public_snapshot_versions": (
                "SELECT 1 FROM public_snapshot_versions WHERE market_date = ? LIMIT 1"
            ),
            "public_calendar_manifests": (
                "SELECT 1 FROM public_calendar_manifests WHERE market_date = ? LIMIT 1"
            ),
            "public_calendar_versions": (
                "SELECT 1 FROM public_calendar_versions WHERE market_date = ? LIMIT 1"
            ),
        }
        for table, query in queries.items():
            if table in tables and connection.execute(query, (market_date,)).fetchone():
                found.append(f"sqlite:{table}:{market_date}")
    return found


def _read_boundary_object(path: Path) -> dict[str, Any]:
    source = _assert_no_reparse_components(path)
    before = source.stat()
    if not source.is_file() or _is_reparse_point(source):
        raise ActivationContractError("activation public artifact is unsafe")
    try:
        value = json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ActivationContractError) as exc:
        raise ActivationContractError("activation public artifact is invalid") from exc
    after = source.stat()
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise ActivationContractError("activation public artifact changed during read")
    if not isinstance(value, dict):
        raise ActivationContractError("activation public artifact must be an object")
    return value


def _artifact_market_date(payload: Mapping[str, Any]) -> str:
    for field in ("market_date", "as_of_market_date", "date"):
        value = payload.get(field)
        if isinstance(value, str) and _MARKET_DATE.fullmatch(value):
            return value
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    evidence = subparsers.add_parser("validate-evidence")
    evidence.add_argument("--ci", required=True)
    evidence.add_argument("--sol", required=True)
    evidence.add_argument("--candidate-sha", required=True)
    evidence.add_argument("--candidate-tree", required=True)
    evidence.add_argument("--require-live-github-ci", action="store_true")
    evidence.add_argument(
        "--require-live-github-owner-authorization", action="store_true"
    )

    evidence_seal = subparsers.add_parser("seal-evidence")
    evidence_seal.add_argument("--input", required=True)
    evidence_seal.add_argument("--output", required=True)

    state = subparsers.add_parser("inspect-state")
    state.add_argument("--db-path", required=True)

    seal = subparsers.add_parser("seal-receipt")
    seal.add_argument("--input", required=True)
    seal.add_argument("--output", required=True)

    verify = subparsers.add_parser("verify-receipt")
    verify.add_argument("--receipt", required=True)
    verify.add_argument("--expected-status", choices=("PREPARED", "COMPLETE", "ROLLED_BACK"))

    declaration = subparsers.add_parser("validate-state-preparation-declaration")
    declaration.add_argument("--input", required=True)

    boundary = subparsers.add_parser("validate-activation-boundary")
    boundary.add_argument("--market-date", required=True)
    boundary.add_argument("--now-utc", required=True)
    boundary.add_argument("--state-root", default=None)
    boundary.add_argument("--runtime-root", default=None)

    args = parser.parse_args(argv)
    try:
        if args.command == "validate-evidence":
            result = validate_evidence_pair(
                args.ci,
                args.sol,
                candidate_sha=args.candidate_sha,
                candidate_tree=args.candidate_tree,
                require_live_github_ci=args.require_live_github_ci,
                require_live_github_owner_authorization=(
                    args.require_live_github_owner_authorization
                ),
            )
        elif args.command == "seal-evidence":
            result = seal_evidence(_load_object(args.input))
            _atomic_write(Path(args.output), result)
        elif args.command == "inspect-state":
            result = inspect_state(args.db_path)
        elif args.command == "seal-receipt":
            result = seal_receipt(_load_object(args.input), args.output)
        elif args.command == "validate-state-preparation-declaration":
            result = validate_state_preparation_declaration(
                _load_object(args.input), require_interpreter_identity=True
            )
        elif args.command == "validate-activation-boundary":
            try:
                observed = datetime.fromisoformat(args.now_utc.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ActivationContractError("activation boundary clock is invalid") from exc
            result = activation_boundary(
                args.market_date,
                now=observed,
                state_root=args.state_root,
                runtime_root=args.runtime_root,
            )
            if result["ready"] is not True:
                print(_json_summary(result))
                return 4
        else:
            result = load_receipt(args.receipt)
            if args.expected_status and result.get("status") != args.expected_status:
                raise ActivationContractError("runtime receipt status mismatch")
    except (ActivationContractError, OSError) as exc:
        print(_json_summary({"status": "FAIL", "error": str(exc)}))
        return 2
    print(_json_summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
