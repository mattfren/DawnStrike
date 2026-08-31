from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from intraday_scanner.storage.migrations import CURRENT_SCHEMA_VERSION, run_migrations
from scripts.runtime_activation_contract import (
    ACTIVATION_SCHEMA,
    CI_SCHEMA,
    ROLLBACK_SCHEMA,
    SOL_SCHEMA,
    ActivationContractError,
    inspect_state,
    load_receipt,
    seal_evidence,
    seal_receipt,
    self_hash,
    validate_evidence,
    validate_evidence_pair,
)

CANDIDATE_SHA = "a" * 40
CANDIDATE_TREE = "b" * 40
PREVIOUS_SHA = "c" * 40
PREVIOUS_TREE = "d" * 40


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _ci_payload(*, completed: datetime | None = None) -> dict[str, object]:
    return {
        "schema_version": CI_SCHEMA,
        "candidate_sha": CANDIDATE_SHA,
        "candidate_tree": CANDIDATE_TREE,
        "conclusion": "SUCCESS",
        "status": "COMPLETED",
        "head_branch": "main",
        "run_url": "https://github.com/example/dawnstrike/actions/runs/12345",
        "checks_total": 19,
        "checks_succeeded": 19,
        "completed_at_utc": (completed or _now()).isoformat().replace("+00:00", "Z"),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _sol_payload(*, completed: datetime | None = None) -> dict[str, object]:
    return {
        "schema_version": SOL_SCHEMA,
        "candidate_sha": CANDIDATE_SHA,
        "candidate_tree": CANDIDATE_TREE,
        "auditor_model": "gpt-5.6-sol",
        "verdict": "ZERO_CRITICAL_HIGH",
        "critical_findings": 0,
        "high_findings": 0,
        "completed_at_utc": (completed or _now()).isoformat().replace("+00:00", "Z"),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return completed.stdout.strip()


def _self_seal_unsafe(payload: dict[str, object]) -> dict[str, object]:
    value = dict(payload)
    value["evidence_sha256"] = self_hash(value, "evidence_sha256")
    return value


def _receipt_payload(
    *, schema: str = ACTIVATION_SCHEMA, status: str = "COMPLETE"
) -> dict[str, object]:
    activation_id = "e" * 24
    value: dict[str, object] = {
        "schema_version": schema,
        "status": status,
        "activation_id": activation_id,
        "market_date": "2026-08-31",
        "candidate_sha": CANDIDATE_SHA,
        "candidate_tree": CANDIDATE_TREE,
        "previous_sha": PREVIOUS_SHA,
        "previous_tree": PREVIOUS_TREE,
        "ci_evidence_sha256": "1" * 64,
        "sol_evidence_sha256": "2" * 64,
        "state_backup_id": f"runtime-activation-{activation_id}",
        "state_backup_db_sha256": "3" * 64,
        "state_schema_version": CURRENT_SCHEMA_VERSION,
        "state_quick_check": "ok",
        "rollback_bundle_sha256": "4" * 64,
        "task_count": 5,
        "task_contract_sha256": "5" * 64,
        "task_definition_contract_sha256": "9" * 64,
        "task_action_contract_sha256": "7" * 64,
        "task_paths_unchanged": True,
        "task_enablement_restored": status != "PREPARED",
        "scheduler_backup_name": (
            f"runtime-activation-{activation_id}"
            if schema == ACTIVATION_SCHEMA
            else f"runtime-rollback-{activation_id}"
        ),
        "scheduler_backup_manifest_sha256": "8" * 64,
        "runtime_origin_sha256": "6" * 64,
        "swap_contract": "same_volume_two_rename_with_immediate_restore",
        "prepared_at_utc": "2026-08-30T16:00:00Z",
        "completed_at_utc": "2026-08-30T16:01:00Z" if status != "PREPARED" else None,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    if schema == ACTIVATION_SCHEMA:
        value.update(
            {
                "stage_name": f"dawnstrike-runtime.stage-{activation_id}",
                "rollback_checkout_name": "previous-runtime",
                "rollback_bundle_name": "previous-runtime.bundle",
            }
        )
    else:
        value["restored_sha"] = PREVIOUS_SHA
    return value


def test_exact_ci_and_sol_evidence_pair_passes(tmp_path: Path) -> None:
    ci = tmp_path / "ci.json"
    sol = tmp_path / "sol.json"
    _write_json(ci, seal_evidence(_ci_payload()))
    _write_json(sol, seal_evidence(_sol_payload()))

    result = validate_evidence_pair(
        ci,
        sol,
        candidate_sha=CANDIDATE_SHA,
        candidate_tree=CANDIDATE_TREE,
        now=_now(),
    )

    assert result["status"] == "PASS"
    assert result["candidate_sha"] == CANDIDATE_SHA
    assert result["research_only"] is True
    assert result["broker_execution_enabled"] is False


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"candidate_sha": "f" * 40}, "candidate SHA mismatch"),
        ({"candidate_tree": "f" * 40}, "candidate tree mismatch"),
        ({"checks_succeeded": 18}, "check totals"),
        ({"checks_total": 18, "checks_succeeded": 18}, "check totals"),
        ({"conclusion": "FAILURE"}, "completed success"),
        ({"head_branch": "feature"}, "bound to main"),
        ({"broker_execution_enabled": True}, "enables broker execution"),
    ],
)
def test_ci_evidence_fails_closed_on_hostile_mutation(
    tmp_path: Path, mutation: dict[str, object], message: str
) -> None:
    ci_payload = _ci_payload()
    ci_payload.update(mutation)
    ci = tmp_path / "ci.json"
    sol = tmp_path / "sol.json"
    _write_json(ci, _self_seal_unsafe(ci_payload))
    _write_json(sol, seal_evidence(_sol_payload()))

    with pytest.raises(ActivationContractError, match=message):
        validate_evidence_pair(
            ci,
            sol,
            candidate_sha=CANDIDATE_SHA,
            candidate_tree=CANDIDATE_TREE,
            now=_now(),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        {"auditor_model": "gpt-5.6-luna"},
        {"verdict": "PASS_WITH_HIGH_FINDING"},
        {"critical_findings": 1},
        {"high_findings": 1},
        {"research_only": False},
    ],
)
def test_sol_evidence_rejects_nonfinal_or_unsafe_audit(
    mutation: dict[str, object],
) -> None:
    payload = _sol_payload()
    payload.update(mutation)
    with pytest.raises(ActivationContractError):
        validate_evidence(_self_seal_unsafe(payload), now=_now())


def test_evidence_rejects_stale_tampered_and_sensitive_inputs() -> None:
    stale_time = _now() - timedelta(days=31)
    stale = seal_evidence(_ci_payload(completed=stale_time))
    with pytest.raises(ActivationContractError, match="older than 30 days"):
        validate_evidence(stale, now=_now())

    tampered = seal_evidence(_ci_payload())
    tampered["checks_total"] = 20
    with pytest.raises(ActivationContractError, match="self-hash mismatch"):
        validate_evidence(tampered, now=_now())

    sensitive = _ci_payload()
    sensitive["api_token"] = "must-not-appear"
    sensitive["evidence_sha256"] = "0" * 64
    with pytest.raises(ActivationContractError, match="sensitive field"):
        validate_evidence(sensitive, now=_now())


def test_state_inspection_is_read_only_and_requires_exact_schema(tmp_path: Path) -> None:
    db = tmp_path / "shadow_real.sqlite"
    with sqlite3.connect(db) as connection:
        assert run_migrations(connection) == CURRENT_SCHEMA_VERSION
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    result = inspect_state(db)

    assert result["quick_check"] == "ok"
    assert result["schema_version"] == CURRENT_SCHEMA_VERSION
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert not db.with_name(f"{db.name}-wal").exists()
    assert not db.with_name(f"{db.name}-shm").exists()


def test_state_inspection_rejects_missing_or_incompatible_schema(tmp_path: Path) -> None:
    missing = tmp_path / "shadow_real.sqlite"
    with sqlite3.connect(missing) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")
    with pytest.raises(ActivationContractError, match="schema_version table is missing"):
        inspect_state(missing)

    incompatible = tmp_path / "other" / "shadow_real.sqlite"
    incompatible.parent.mkdir()
    with sqlite3.connect(incompatible) as connection:
        connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_version VALUES (?)", (CURRENT_SCHEMA_VERSION - 1,))
    with pytest.raises(ActivationContractError, match="does not exactly match"):
        inspect_state(incompatible)


def test_activation_and_rollback_receipts_are_strict_self_hashed_and_atomic(
    tmp_path: Path,
) -> None:
    activation_path = tmp_path / "receipts" / "activation.json"
    activation = seal_receipt(_receipt_payload(), activation_path)
    assert load_receipt(activation_path) == activation
    assert activation["status"] == "COMPLETE"
    with pytest.raises(ActivationContractError, match="already exists"):
        seal_receipt(_receipt_payload(), activation_path)

    rollback_path = tmp_path / "receipts" / "rollback.json"
    rollback = seal_receipt(
        _receipt_payload(schema=ROLLBACK_SCHEMA, status="ROLLED_BACK"),
        rollback_path,
    )
    assert load_receipt(rollback_path) == rollback
    assert rollback["restored_sha"] == PREVIOUS_SHA
    assert not list(activation_path.parent.glob("*.tmp"))


@pytest.mark.parametrize(
    "mutation",
    [
        {"task_paths_unchanged": False},
        {"broker_execution_enabled": True},
        {"state_quick_check": "corrupt"},
        {"state_schema_version": CURRENT_SCHEMA_VERSION - 1},
        {"rollback_bundle_sha256": "not-a-hash"},
        {"stage_name": "..\\escape"},
        {"scheduler_backup_name": "runtime-activation-" + "f" * 24},
        {"state_backup_id": "runtime-activation-" + "f" * 24},
        {"market_date": "2026-99-99"},
        {"completed_at_utc": "2026-08-30T15:59:59Z"},
    ],
)
def test_activation_receipt_rejects_unsafe_or_mismatched_contract(
    tmp_path: Path, mutation: dict[str, object]
) -> None:
    payload = _receipt_payload()
    payload.update(mutation)
    with pytest.raises(ActivationContractError):
        seal_receipt(payload, tmp_path / "receipt.json")
    assert not (tmp_path / "receipt.json").exists()


def test_activation_receipt_rejects_extra_fields_and_tampering(tmp_path: Path) -> None:
    payload = _receipt_payload()
    payload["operator_secret"] = "forbidden"  # pragma: allowlist secret
    with pytest.raises(ActivationContractError, match="sensitive field"):
        seal_receipt(payload, tmp_path / "extra.json")

    path = tmp_path / "receipt.json"
    seal_receipt(_receipt_payload(), path)
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["candidate_sha"] = "f" * 40
    _write_json(path, stored)
    with pytest.raises(ActivationContractError, match="self-hash mismatch"):
        load_receipt(path)


def test_windows_activation_scripts_preserve_nonpublishing_fail_closed_boundary() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    rollback = Path("scripts/rollback_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    combined = activation + rollback

    assert "refs/remotes/origin/main" in activation
    assert "merge-base" in activation and "--is-ancestor" in activation
    assert "state_disaster_recovery.py" in activation
    assert '"bundle", "create"' in activation
    assert "same_volume_two_rename_with_immediate_restore" in combined
    assert "Get-DawnstrikeTaskContract" in combined
    assert "Enter-DawnstrikeDailyRunLock" in combined
    assert "Archive-DawnstrikeReceiptBoundStaleLocks" in rollback
    assert "prepared_receipt_file_sha256" in combined
    assert "Get-DawnstrikeLockOwnerState" in combined
    assert ".archived." in combined
    assert "broker_execution_enabled = $false" in combined
    assert "research_only = $true" in combined
    assert "publish_vercel" not in combined.lower()
    assert "telegram" not in combined.lower()
    assert "Remove-Item -LiteralPath $runtime" not in combined
    assert "Directory]::Move" in combined
    assert "scheduler-backups" in combined
    assert "Disable-ScheduledTask" in combined
    assert "Enable-ScheduledTask" in combined
    contract_validation = rollback.index("$contractGit = Get-DawnstrikeGitContract")
    contract_execution = rollback.index("$activation = Invoke-DawnstrikeContractCli")
    assert contract_validation < contract_execution
    disable_call = activation.index("            Disable-DawnstrikeCanonicalTasks")
    activation_swap = activation.index("[System.IO.Directory]::Move($runtime, $rollbackCheckout)")
    enable_call = activation.index("            Enable-DawnstrikeCanonicalTasks", activation_swap)
    assert disable_call < activation_swap < enable_call
    rollback_disable = rollback.index("            Disable-DawnstrikeCanonicalTasks")
    rollback_swap = rollback.index("[System.IO.Directory]::Move($runtime, $deactivatedCandidate)")
    rollback_enable = rollback.index("        Enable-DawnstrikeCanonicalTasks", rollback_swap)
    assert rollback_disable < rollback_swap < rollback_enable


def _write_stale_activation_lock_fixture(
    tmp_path: Path,
    *,
    activation_id: str = "e" * 24,
    process_id: int = 2_147_483_647,
    process_started_at: str = "2020-01-01T00:00:00.0000000Z",
    activation_binding: str = "BOUND",
    daily_binding: str = "BOUND",
    owner: str = "runtime_activation",
) -> tuple[Path, Path]:
    state = tmp_path / "state"
    locks = state / "locks"
    receipt = state / "receipts" / "runtime-activation" / (
        f"runtime-activation-{activation_id}.prepared.json"
    )
    locks.mkdir(parents=True)
    receipt.parent.mkdir(parents=True)
    receipt.write_text(
        json.dumps(
            {
                "status": "PREPARED",
                "activation_id": activation_id,
                "market_date": "2026-08-31",
                "receipt_sha256": "a" * 64,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    receipt_file_sha = hashlib.sha256(receipt.read_bytes()).hexdigest()
    common = {
        "process_id": process_id,
        "process_started_at_utc": process_started_at,
        "owner": owner,
        "activation_id": activation_id,
        "prepared_receipt_name": receipt.name,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    activation_common = {
        **common,
        "prepared_receipt_sha256": "a" * 64 if activation_binding == "BOUND" else None,
        "prepared_receipt_file_sha256": receipt_file_sha if activation_binding == "BOUND" else None,
        "receipt_binding_status": activation_binding,
    }
    daily_common = {
        **common,
        "prepared_receipt_sha256": "a" * 64 if daily_binding == "BOUND" else None,
        "prepared_receipt_file_sha256": receipt_file_sha if daily_binding == "BOUND" else None,
        "receipt_binding_status": daily_binding,
    }
    activation = {
        "schema_version": "dawnstrike.runtime_activation_lock.v2",
        "lock_token": "b" * 32,
        **activation_common,
    }
    daily = {
        "schema_version": "dawnstrike.daily_run_lock.v4",
        "lock_token": "c" * 32,
        "market_date": "2026-08-31",
        **daily_common,
    }
    _write_json(locks / "dawnstrike-runtime-activation.lock", activation)
    _write_json(locks / "dawnstrike-daily-2026-08-31.lock", daily)
    return state, receipt


def _run_stale_lock_recovery(
    state: Path,
    receipt: Path,
    *,
    active_owner: bool = False,
    duplicate_owner: bool = False,
    mutate_receipt_after_hash: bool = False,
) -> dict[str, object]:
    activation_script = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace(
        "'", "''"
    )
    stage_script = str(Path("scripts/invoke_dawnstrike_stage.ps1").resolve()).replace(
        "'", "''"
    )
    state_text = str(state).replace("'", "''")
    receipt_text = str(receipt).replace("'", "''")
    owner_setup = ""
    if active_owner or duplicate_owner:
        owner_setup = rf"""
    $owner = Get-Process -Id $PID
    $ownerStart = $owner.StartTime.ToUniversalTime().ToString('o')
    foreach ($name in @(
        'dawnstrike-runtime-activation.lock',
        'dawnstrike-daily-2026-08-31.lock'
    )) {{
        $path = Join-Path '{state_text}' ('locks\\' + $name)
        if ({'$true' if duplicate_owner else '$false'}) {{
            $raw = Get-Content -LiteralPath $path -Raw
            $raw = [regex]::Replace(
                $raw,
                '"process_started_at_utc"\s*:\s*"[^"]+"',
                ('"process_started_at_utc":"2020-01-01T00:00:00.0000000Z",' +
                 '"process_started_at_utc":"' + $ownerStart + '"'),
                1
            )
            [System.IO.File]::WriteAllText($path, $raw)
        }} else {{
            $payload = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
            $payload.process_id = $PID
            $payload.process_started_at_utc = $ownerStart
            $payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $path
        }}
    }}
    """
    command = rf"""
    . '{activation_script}'
    . '{stage_script}'
    {owner_setup}
    $receipt = [pscustomobject]@{{
        status='PREPARED'; activation_id=('e' * 24); market_date='2026-08-31';
        receipt_sha256=('a' * 64)
    }}
    $ok = $false
    $message = ''
    try {{
        $preparedReceiptFileSha256 = Get-DawnstrikeSha256File '{receipt_text}'
        if ({'$true' if mutate_receipt_after_hash else '$false'}) {{
            $raw = Get-Content -LiteralPath '{receipt_text}' -Raw
            $raw = $raw.Replace('"market_date": "2026-08-31"', '"market_date": "2026-08-30"')
            [System.IO.File]::WriteAllText('{receipt_text}', $raw)
        }}
        $result = Archive-DawnstrikeReceiptBoundStaleLocks `
            -StateRoot '{state_text}' -ActivationReceiptPath '{receipt_text}' -Receipt $receipt `
            -PreparedReceiptFileSha256 $preparedReceiptFileSha256
        $ok = $true
    }} catch {{ $message = $_.Exception.Message }}
    [pscustomobject]@{{
        ok=$ok; message=$message;
        current_activation=Test-Path -LiteralPath (
            Join-Path '{state_text}' 'locks\\dawnstrike-runtime-activation.lock'
        ) -PathType Leaf
        current_daily=Test-Path -LiteralPath (
            Join-Path '{state_text}' 'locks\\dawnstrike-daily-2026-08-31.lock'
        ) -PathType Leaf
        archives=@(Get-ChildItem -LiteralPath (Join-Path '{state_text}' 'locks') `
            -Filter '*.archived.*' -File -Force -ErrorAction SilentlyContinue).Count
    }} | ConvertTo-Json -Compress
    """
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _run_normal_stage_acquire(state: Path) -> dict[str, object]:
    stage_script = str(Path("scripts/invoke_dawnstrike_stage.ps1").resolve()).replace(
        "'", "''"
    )
    state_text = str(state).replace("'", "''")
    command = rf"""
    . '{stage_script}'
    $result = Enter-DawnstrikeDailyRunLock `
        -StateRoot '{state_text}' -MarketDate '2026-08-31' -Owner 'alphaops_morning'
    [pscustomobject]@{{
        acquired=[bool]$result.acquired; reason=[string]$result.reason;
        current_daily=Test-Path -LiteralPath (
            Join-Path '{state_text}' 'locks\dawnstrike-daily-2026-08-31.lock'
        ) -PathType Leaf
        current_activation=Test-Path -LiteralPath (
            Join-Path '{state_text}' 'locks\dawnstrike-runtime-activation.lock'
        ) -PathType Leaf
        archives=@(Get-ChildItem -LiteralPath (Join-Path '{state_text}' 'locks') `
            -Filter '*.archived.*' -File -Force -ErrorAction SilentlyContinue).Count
    }} | ConvertTo-Json -Compress
    """
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _run_complete_stale_lock_recovery(
    state: Path, prepared: Path, complete: Path, *, active_owner: bool = False
) -> dict[str, object]:
    activation_script = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace(
        "'", "''"
    )
    stage_script = str(Path("scripts/invoke_dawnstrike_stage.ps1").resolve()).replace(
        "'", "''"
    )
    state_text = str(state).replace("'", "''")
    prepared_text = str(prepared).replace("'", "''")
    complete_text = str(complete).replace("'", "''")
    owner_setup = ""
    if active_owner:
        owner_setup = rf"""
    $owner = Get-Process -Id $PID
    $ownerStart = $owner.StartTime.ToUniversalTime().ToString('o')
    foreach ($name in @(
        'dawnstrike-runtime-activation.lock'
        'dawnstrike-daily-2026-08-31.lock'
    )) {{
        $path = Join-Path '{state_text}' ('locks\\' + $name)
        $payload = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
        $payload.process_id = $PID
        $payload.process_started_at_utc = $ownerStart
        $payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $path
    }}
    """
    command = rf"""
    . '{activation_script}'
    . '{stage_script}'
    {owner_setup}
    $prepared = Get-Content -LiteralPath '{prepared_text}' -Raw | ConvertFrom-Json
    $complete = Get-Content -LiteralPath '{complete_text}' -Raw | ConvertFrom-Json
    $ok = $false
    $message = ''
    try {{
        $result = Archive-DawnstrikeReceiptBoundStaleLocks `
            -StateRoot '{state_text}' -ActivationReceiptPath '{complete_text}' `
            -Receipt $complete -PreparedReceipt $prepared `
            -PreparedReceiptFileSha256 (Get-DawnstrikeSha256File '{prepared_text}')
        $ok = $true
    }} catch {{ $message = $_.Exception.Message }}
    [pscustomobject]@{{
        ok=$ok; message=$message;
        current_activation=Test-Path -LiteralPath (
            Join-Path '{state_text}' 'locks\\dawnstrike-runtime-activation.lock'
        ) -PathType Leaf
        current_daily=Test-Path -LiteralPath (
            Join-Path '{state_text}' 'locks\\dawnstrike-daily-2026-08-31.lock'
        ) -PathType Leaf
        archives=@(
            Get-ChildItem -LiteralPath (Join-Path '{state_text}' 'locks') `
                -Filter '*.archived.*' -File -Force -ErrorAction SilentlyContinue
        ).Count
    }} | ConvertTo-Json -Compress
    """
    completed_process = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed_process.returncode == 0, (
        completed_process.stdout,
        completed_process.stderr,
    )
    return json.loads(completed_process.stdout.strip().splitlines()[-1])


def _run_bind_then_exit(state: Path, receipt: Path, bound_kind: str) -> None:
    activation_script = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace(
        "'", "''"
    )
    stage_script = str(Path("scripts/invoke_dawnstrike_stage.ps1").resolve()).replace(
        "'", "''"
    )
    state_text = str(state).replace("'", "''")
    receipt_text = str(receipt).replace("'", "''")
    command = rf"""
    . '{activation_script}'
    . '{stage_script}'
    $prepared = Get-Content -LiteralPath '{receipt_text}' -Raw | ConvertFrom-Json
    $name = Split-Path -Leaf '{receipt_text}'
    $activationLock = Enter-DawnstrikeRuntimeActivationLock `
        -StateRoot '{state_text}' -ActivationId $prepared.activation_id -PreparedReceiptName $name
    $dailyLock = Enter-DawnstrikeDailyRunLock `
        -StateRoot '{state_text}' -MarketDate $prepared.market_date -Owner 'runtime_activation' `
        -ActivationId $prepared.activation_id -PreparedReceiptName $name
    Set-DawnstrikeReceiptBoundLock `
        -LockPath $activationLock.path -LockToken $activationLock.token `
        -ActivationId $prepared.activation_id `
        -PreparedReceiptPath '{receipt_text}' `
        -PreparedReceiptFileSha256 (Get-DawnstrikeSha256File '{receipt_text}') -Receipt $prepared
    if ('{bound_kind}' -eq 'daily') {{
        Set-DawnstrikeReceiptBoundLock `
            -LockPath $dailyLock.lock_path -LockToken $dailyLock.lock_token `
            -ActivationId $prepared.activation_id `
            -PreparedReceiptPath '{receipt_text}' `
            -PreparedReceiptFileSha256 (Get-DawnstrikeSha256File '{receipt_text}') `
            -Receipt $prepared
    }}
    exit 0
    """
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
@pytest.mark.parametrize("kill_point", ["pre_rename", "between_renames", "post_swap_pre_restore"])
def test_receipt_bound_stale_lock_recovery_kill_points(tmp_path: Path, kill_point: str) -> None:
    state, receipt = _write_stale_activation_lock_fixture(tmp_path)
    runtime = tmp_path / "runtime"
    rollback = state / "runtime-rollbacks" / ("e" * 24)
    runtime.mkdir()
    (runtime / "boundary.txt").write_text(kill_point, encoding="utf-8")
    if kill_point == "between_renames":
        rollback.mkdir(parents=True)
        runtime.rename(rollback / "previous-runtime")
    elif kill_point == "post_swap_pre_restore":
        rollback.mkdir(parents=True)
        (rollback / "previous-runtime").mkdir()
        (rollback / "previous-runtime" / "previous.txt").write_text("previous", encoding="utf-8")

    result = _run_stale_lock_recovery(state, receipt)

    assert result == {
        "ok": True,
        "message": "",
        "current_activation": False,
        "current_daily": False,
        "archives": 2,
    }
    assert _run_stale_lock_recovery(state, receipt) == result


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
@pytest.mark.parametrize(
    "hostile_case", ["live_owner", "mismatched_owner", "mismatched_receipt", "tampered_receipt"]
)
def test_receipt_bound_stale_lock_recovery_rejects_hostile_state(
    tmp_path: Path, hostile_case: str
) -> None:
    state, receipt = _write_stale_activation_lock_fixture(tmp_path)
    if hostile_case == "mismatched_owner":
        daily = state / "locks" / "dawnstrike-daily-2026-08-31.lock"
        payload = json.loads(daily.read_text(encoding="utf-8"))
        payload["process_started_at_utc"] = "2021-01-01T00:00:00.0000000Z"
        _write_json(daily, payload)
    elif hostile_case == "mismatched_receipt":
        activation = state / "locks" / "dawnstrike-runtime-activation.lock"
        payload = json.loads(activation.read_text(encoding="utf-8"))
        payload["activation_id"] = "f" * 24
        _write_json(activation, payload)
    elif hostile_case == "tampered_receipt":
        receipt.write_text(receipt.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")

    result = _run_stale_lock_recovery(state, receipt, active_owner=hostile_case == "live_owner")

    assert result["ok"] is False
    assert result["current_activation"] is True
    assert result["current_daily"] is True
    assert result["archives"] == 0


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_receipt_bound_recovery_rejects_receipt_mutation_after_hash_capture(
    tmp_path: Path,
) -> None:
    state, receipt = _write_stale_activation_lock_fixture(tmp_path)
    result = _run_stale_lock_recovery(state, receipt, mutate_receipt_after_hash=True)

    assert result["ok"] is False
    assert "changed" in result["message"]
    assert result["current_activation"] is True
    assert result["current_daily"] is True
    assert result["archives"] == 0


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
@pytest.mark.parametrize("owner_case", ["runtime_rollback", "mixed", "spoofed"])
def test_receipt_bound_recovery_enforces_allowlisted_pair_owner(
    tmp_path: Path, owner_case: str
) -> None:
    state, receipt = _write_stale_activation_lock_fixture(
        tmp_path, owner="runtime_rollback" if owner_case != "spoofed" else "spoofed"
    )
    if owner_case == "mixed":
        daily = state / "locks" / "dawnstrike-daily-2026-08-31.lock"
        payload = json.loads(daily.read_text(encoding="utf-8"))
        payload["owner"] = "runtime_activation"
        _write_json(daily, payload)

    result = _run_stale_lock_recovery(state, receipt)

    if owner_case == "runtime_rollback":
        assert result["ok"] is True
        assert result["current_activation"] is False
        assert result["current_daily"] is False
        assert result["archives"] == 2
    else:
        assert result["ok"] is False
        assert result["current_activation"] is True
        assert result["current_daily"] is True
        assert result["archives"] == 0


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_receipt_bound_recovery_rejects_duplicate_live_owner_identity(
    tmp_path: Path,
) -> None:
    state, receipt = _write_stale_activation_lock_fixture(tmp_path)
    result = _run_stale_lock_recovery(state, receipt, duplicate_owner=True)

    assert result["ok"] is False
    assert "valid json" in result["message"].lower()
    assert result["current_activation"] is True
    assert result["current_daily"] is True
    assert result["archives"] == 0


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_complete_receipt_recovery_archives_dead_pair_idempotently(tmp_path: Path) -> None:
    state, prepared = _write_stale_activation_lock_fixture(tmp_path)
    complete = prepared.with_name(prepared.name.replace(".prepared.json", ".json"))
    seal_receipt(_receipt_payload(status="COMPLETE"), complete)

    result = _run_complete_stale_lock_recovery(state, prepared, complete)

    assert result == {
        "ok": True,
        "message": "",
        "current_activation": False,
        "current_daily": False,
        "archives": 2,
    }
    assert _run_complete_stale_lock_recovery(state, prepared, complete) == result


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_complete_receipt_recovery_rejects_live_pair(tmp_path: Path) -> None:
    state, prepared = _write_stale_activation_lock_fixture(tmp_path)
    complete = prepared.with_name(prepared.name.replace(".prepared.json", ".json"))
    seal_receipt(_receipt_payload(status="COMPLETE"), complete)

    result = _run_complete_stale_lock_recovery(state, prepared, complete, active_owner=True)

    assert result["ok"] is False
    assert result["current_activation"] is True
    assert result["current_daily"] is True
    assert result["archives"] == 0


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
@pytest.mark.parametrize("junction_component", ["receipt_root", "lock_root"])
def test_receipt_bound_recovery_rejects_descendant_junction_escape(
    tmp_path: Path, junction_component: str
) -> None:
    state, receipt = _write_stale_activation_lock_fixture(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    if junction_component == "receipt_root":
        real_root = receipt.parent
        held_root = state / "receipts" / "runtime-activation-held"
        real_root.rename(held_root)
        external_receipt = external / receipt.name
        shutil.copy2(held_root / receipt.name, external_receipt)
        target = real_root
    else:
        real_root = state / "locks"
        held_root = state / "locks-held"
        real_root.rename(held_root)
        target = real_root
        external_lock_root = external / "locks"
        shutil.copytree(held_root, external_lock_root)
        external = external_lock_root
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"New-Item -ItemType Junction -Path '{target}' -Target '{external}' | Out-Null",
        ],
        check=True,
        capture_output=True,
    )

    result = _run_stale_lock_recovery(state, receipt)

    assert result["ok"] is False
    assert result["archives"] == 0


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
@pytest.mark.parametrize(
    ("activation_binding", "daily_binding"),
    [("UNBOUND", "UNBOUND"), ("BOUND", "UNBOUND"), ("UNBOUND", "BOUND")],
)
def test_receipt_bound_stale_lock_recovery_completes_partial_binding_transition(
    tmp_path: Path, activation_binding: str, daily_binding: str
) -> None:
    state, receipt = _write_stale_activation_lock_fixture(
        tmp_path,
        activation_binding=activation_binding,
        daily_binding=daily_binding,
    )

    result = _run_stale_lock_recovery(state, receipt)

    assert result == {
        "ok": True,
        "message": "",
        "current_activation": False,
        "current_daily": False,
        "archives": 2,
    }
    assert _run_stale_lock_recovery(state, receipt) == result


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
@pytest.mark.parametrize("bound_kind", ["activation", "daily"])
def test_receipt_bound_stale_lock_recovery_after_each_individual_bind(
    tmp_path: Path, bound_kind: str
) -> None:
    state, receipt = _write_stale_activation_lock_fixture(tmp_path)
    for lock in state.glob("locks/*.lock"):
        lock.unlink()

    _run_bind_then_exit(state, receipt, bound_kind)
    result = _run_stale_lock_recovery(state, receipt)

    assert result["ok"] is True
    assert result["current_activation"] is False
    assert result["current_daily"] is False
    assert result["archives"] == 2


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
@pytest.mark.parametrize(
    "pair_case", ["paired", "transition_unbound", "missing_activation", "mismatched_activation"]
)
def test_normal_stage_never_evicts_receipt_bound_activation_daily_lock(
    tmp_path: Path, pair_case: str
) -> None:
    state, _receipt = _write_stale_activation_lock_fixture(
        tmp_path,
        daily_binding="UNBOUND" if pair_case == "transition_unbound" else "BOUND",
    )
    activation = state / "locks" / "dawnstrike-runtime-activation.lock"
    if pair_case == "missing_activation":
        activation.unlink()
    elif pair_case == "mismatched_activation":
        payload = json.loads(activation.read_text(encoding="utf-8"))
        payload["activation_id"] = "f" * 24
        _write_json(activation, payload)

    result = _run_normal_stage_acquire(state)

    assert result == {
        "acquired": False,
        "reason": "receipt_bound_activation_requires_rollback",
        "current_daily": True,
        "current_activation": pair_case != "missing_activation",
        "archives": 0,
    }


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_task_contract_requires_exact_ready_or_explicit_disabled(tmp_path: Path) -> None:
    script = Path("scripts/activate_dawnstrike_runtime.ps1").resolve()
    safe = str(script).replace("'", "''")
    command = rf"""
. '{safe}'
$global:MockTaskState = 'Ready'
$global:MockDefinition = 'morning-v1'
function Get-ScheduledTask {{
    [CmdletBinding()] param([string]$TaskName)
    [pscustomobject]@{{
        State=$global:MockTaskState; TaskPath='\';
        Actions=@([pscustomobject]@{{
            Execute='powershell.exe';
            Arguments='-RuntimeRoot "C:\runtime" -StateRoot "C:\state"';
            WorkingDirectory='C:\runtime'
        }})
    }}
}}
function Export-ScheduledTask {{
    [CmdletBinding()] param([string]$TaskName,[string]$TaskPath)
    $enabled = if ($global:MockTaskState -eq 'Disabled') {{ 'false' }} else {{ 'true' }}
    "<Task><Name>$TaskName</Name><Description>$global:MockDefinition</Description><Settings><Enabled>$enabled</Enabled></Settings></Task>"
}}
$ready = Get-DawnstrikeTaskContract -RuntimeRoot 'C:\runtime' -StateRoot 'C:\state'
$blocked = @{{}}
foreach ($state in @('Running', 'Queued', 'Unknown', 'Disabled')) {{
    $global:MockTaskState = $state
    $didBlock = $false
    try {{ $null = Get-DawnstrikeTaskContract -RuntimeRoot 'C:\runtime' -StateRoot 'C:\state' }}
        catch {{ $didBlock = $_.Exception.Message -match 'not in an approved exact state' }}
    $blocked[$state] = $didBlock
}}
$global:MockTaskState = 'Disabled'
$disabled = Get-DawnstrikeTaskContract `
    -RuntimeRoot 'C:\runtime' -StateRoot 'C:\state' -AllowDisabled
$global:MockDefinition = 'hostile-trigger-drift'
$mutated = Get-DawnstrikeTaskContract `
    -RuntimeRoot 'C:\runtime' -StateRoot 'C:\state' -AllowDisabled
$result = [pscustomobject]@{{
    ready=$ready
    blocked=$blocked
    disabled=$disabled
    mutated=$mutated
}}
$result | ConvertTo-Json -Depth 5 -Compress
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["ready"] is not None, (result.stdout, result.stderr)
    assert payload["ready"]["task_count"] == 5
    assert len(payload["ready"]["task_contract_sha256"]) == 64
    assert payload["blocked"] == {
        "Disabled": True,
        "Queued": True,
        "Running": True,
        "Unknown": True,
    }
    assert payload["disabled"]["disabled_count"] == 5
    assert payload["disabled"]["enabled_count"] == 0
    assert (
        payload["disabled"]["task_definition_contract_sha256"]
        == payload["ready"]["task_definition_contract_sha256"]
    )
    assert payload["disabled"]["task_contract_sha256"] != payload["ready"]["task_contract_sha256"]
    assert (
        payload["mutated"]["task_definition_contract_sha256"]
        != payload["ready"]["task_definition_contract_sha256"]
    )


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_git_contract_rejects_ignored_executable_artifact(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(checkout)],
        check=True,
        capture_output=True,
    )
    _git(checkout, "config", "user.email", "activation-test@example.invalid")
    _git(checkout, "config", "user.name", "Activation Test")
    (checkout / ".gitignore").write_text("*.pyc\n.pytest_cache/\n", encoding="utf-8")
    (checkout / "safe.txt").write_text("safe\n", encoding="utf-8")
    _git(checkout, "add", ".gitignore", "safe.txt")
    _git(checkout, "commit", "-m", "safe")
    (checkout / "hostile.pyc").write_bytes(b"hostile ignored executable")
    (checkout / ".pytest_cache").mkdir()
    (checkout / ".pytest_cache" / "README.md").write_text("inert\n", encoding="utf-8")

    activation = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace("'", "''")
    runner = str(Path("scripts/dawnstrike_job_process.ps1").resolve()).replace("'", "''")
    root = str(checkout).replace("'", "''")
    command = rf"""
. '{activation}'
. '{runner}'
$gitPath = (@(Get-Command git.exe -CommandType Application -ErrorAction Stop)[0]).Source
$blocked = $false
try {{ $null = Get-DawnstrikeGitContract -GitPath $gitPath -Root '{root}' -TimeoutSeconds 30 }}
catch {{ $blocked = $_.Exception.Message -match 'ignored executable' }}
$blocked | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout.strip().splitlines()[-1]) is True


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_backup_root_validation_is_absolute_isolated_and_drive_root_safe() -> None:
    script = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace("'", "''")
    command = rf"""
. '{script}'
$result = [ordered]@{{}}
try {{
    Ensure-DawnstrikeActivationRoot 'relative-backups' 'BackupRoot' | Out-Null
    $result.relative = 'FAIL'
}}
catch {{ $result.relative = 'BLOCKED' }}
try {{
    Ensure-DawnstrikeActivationRoot 'C:\\' 'BackupRoot' | Out-Null
    $result.drive_root = 'PASS'
}}
catch {{ $result.drive_root = 'FAIL' }}
try {{
    Assert-DawnstrikeRootIsolation `
        'C:\\r\\dawnstrike-state\\nested' @('C:\\r\\dawnstrike-state') 'BackupRoot'
    $result.contained = 'FAIL'
}}
catch {{ $result.contained = 'BLOCKED' }}
$future = Join-Path $env:TEMP ('activation-future-' + $PID)
try {{
    $futurePath = Get-DawnstrikeFutureActivationRoot $future 'BackupRoot'
    $result.future = -not (Test-Path -LiteralPath $futurePath)
}}
catch {{ $result.future = $false }}
$result | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "relative": "BLOCKED",
        "drive_root": "PASS",
        "contained": "BLOCKED",
        "future": True,
    }


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_disposable_activation_and_rollback_preserve_exact_runtime_and_state(
    tmp_path: Path,
) -> None:
    source = Path.cwd()
    candidate = tmp_path / "candidate"
    runtime = tmp_path / "dawnstrike-runtime"
    state = tmp_path / "state"
    backup = tmp_path / "backups"
    remote = tmp_path / "origin.git"
    candidate.mkdir()
    runtime.mkdir()
    state.mkdir()

    (candidate / "scripts").mkdir()
    for name in (
        "activate_dawnstrike_runtime.ps1",
        "rollback_dawnstrike_runtime.ps1",
        "runtime_activation_contract.py",
        "dawnstrike_job_process.ps1",
        "invoke_dawnstrike_stage.ps1",
        "state_disaster_recovery.py",
    ):
        shutil.copy2(source / "scripts" / name, candidate / "scripts" / name)
    shutil.copytree(
        source / "intraday_scanner",
        candidate / "intraday_scanner",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    shutil.copy2(source / ".gitignore", candidate / ".gitignore")
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(candidate)],
        check=True,
        capture_output=True,
    )
    _git(candidate, "config", "user.email", "activation-test@example.invalid")
    _git(candidate, "config", "user.name", "Activation Test")
    _git(candidate, "add", ".")
    _git(candidate, "commit", "-m", "candidate")
    _git(candidate, "remote", "add", "origin", str(remote))
    _git(candidate, "push", "-u", "origin", "main")
    candidate_sha = _git(candidate, "rev-parse", "HEAD")
    candidate_tree = _git(candidate, "rev-parse", "HEAD^{tree}")

    subprocess.run(
        ["git", "init", "--initial-branch=main", str(runtime)],
        check=True,
        capture_output=True,
    )
    _git(runtime, "config", "user.email", "activation-test@example.invalid")
    _git(runtime, "config", "user.name", "Activation Test")
    (runtime / "previous.txt").write_text("previous-runtime\n", encoding="utf-8")
    _git(runtime, "add", "previous.txt")
    _git(runtime, "commit", "-m", "previous")
    _git(runtime, "remote", "add", "origin", str(remote))
    previous_sha = _git(runtime, "rev-parse", "HEAD")

    db = state / "shadow_real.sqlite"
    with sqlite3.connect(db) as connection:
        run_migrations(connection)
    db_hash_before = hashlib.sha256(db.read_bytes()).hexdigest()
    evidence_root = state / "evidence"
    evidence_root.mkdir()
    ci_payload = _ci_payload()
    ci_payload["candidate_sha"] = candidate_sha
    ci_payload["candidate_tree"] = candidate_tree
    sol_payload = _sol_payload()
    sol_payload["candidate_sha"] = candidate_sha
    sol_payload["candidate_tree"] = candidate_tree
    ci = evidence_root / "ci.json"
    sol = evidence_root / "sol.json"
    _write_json(ci, seal_evidence(ci_payload))
    _write_json(sol, seal_evidence(sol_payload))

    activation_script = str(
        (candidate / "scripts" / "activate_dawnstrike_runtime.ps1").resolve()
    ).replace("'", "''")
    rollback_script = str(
        (candidate / "scripts" / "rollback_dawnstrike_runtime.ps1").resolve()
    ).replace("'", "''")
    values = {
        "candidate": str(candidate).replace("'", "''"),
        "runtime": str(runtime).replace("'", "''"),
        "state": str(state).replace("'", "''"),
        "backup": str(backup).replace("'", "''"),
        "ci": str(ci).replace("'", "''"),
        "sol": str(sol).replace("'", "''"),
    }
    command = rf"""
. '{activation_script}'
$global:MockRuntime = '{values["runtime"]}'
$global:MockState = '{values["state"]}'
$global:MockTaskStates = @{{}}
$global:TaskEvents = @()
foreach ($name in $script:DawnstrikeCanonicalTaskNames) {{
    $global:MockTaskStates[$name] = 'Ready'
}}
function Get-ScheduledTask {{
    [CmdletBinding()] param([string]$TaskName)
    [pscustomobject]@{{
        State=$global:MockTaskStates[$TaskName]; TaskPath='\';
        Actions=@([pscustomobject]@{{
            Execute='powershell.exe';
            Arguments=(
                '-RuntimeRoot "' + $global:MockRuntime +
                '" -StateRoot "' + $global:MockState + '"'
            );
            WorkingDirectory=$global:MockRuntime
        }})
    }}
}}
function Export-ScheduledTask {{
    [CmdletBinding()] param([string]$TaskName,[string]$TaskPath)
    $enabled = if ($global:MockTaskStates[$TaskName] -eq 'Disabled') {{ 'false' }} else {{ 'true' }}
    "<Task><Name>$TaskName</Name><Runtime>$global:MockRuntime</Runtime><State>$global:MockState</State><Settings><Enabled>$enabled</Enabled></Settings></Task>"
}}
function Disable-ScheduledTask {{
    [CmdletBinding()] param([string]$TaskName,[string]$TaskPath)
    $global:MockTaskStates[$TaskName] = 'Disabled'
    $global:TaskEvents += ('disable:' + $TaskName)
    [pscustomobject]@{{ TaskName=$TaskName }}
}}
function Enable-ScheduledTask {{
    [CmdletBinding()] param([string]$TaskName,[string]$TaskPath)
    $global:MockTaskStates[$TaskName] = 'Ready'
    $global:TaskEvents += ('enable:' + $TaskName)
    [pscustomobject]@{{ TaskName=$TaskName }}
}}
$activated = Invoke-DawnstrikeRuntimeActivation `
  -ExpectedSha '{candidate_sha}' -MarketDate '2026-08-31' `
  -CiEvidencePath '{values["ci"]}' -SolEvidencePath '{values["sol"]}' `
  -CandidateRoot '{values["candidate"]}' -RuntimeRoot '{values["runtime"]}' `
  -StateRoot '{values["state"]}' -BackupRoot '{values["backup"]}' `
  -BackupRetention 5 -ProcessTimeoutSeconds 120
$bundlePath = Join-Path `
    '{values["state"]}' `
    ('runtime-rollbacks\' + $activated.activation_id + '\previous-runtime.bundle')
$heldBundlePath = $bundlePath + '.held'
[System.IO.File]::Move($bundlePath, $heldBundlePath)
$activationMissingBundleBlocked = $false
try {{
    $null = Invoke-DawnstrikeRuntimeActivation `
      -ExpectedSha '{candidate_sha}' -MarketDate '2026-08-31' `
      -CiEvidencePath '{values["ci"]}' -SolEvidencePath '{values["sol"]}' `
      -CandidateRoot '{values["candidate"]}' -RuntimeRoot '{values["runtime"]}' `
      -StateRoot '{values["state"]}' -BackupRoot '{values["backup"]}' `
      -BackupRetention 5 -ProcessTimeoutSeconds 120
}}
catch {{ $activationMissingBundleBlocked = $true }}
finally {{ [System.IO.File]::Move($heldBundlePath, $bundlePath) }}
$activatedAgain = Invoke-DawnstrikeRuntimeActivation `
  -ExpectedSha '{candidate_sha}' -MarketDate '2026-08-31' `
  -CiEvidencePath '{values["ci"]}' -SolEvidencePath '{values["sol"]}' `
  -CandidateRoot '{values["candidate"]}' -RuntimeRoot '{values["runtime"]}' `
  -StateRoot '{values["state"]}' -BackupRoot '{values["backup"]}' `
  -BackupRetention 5 -ProcessTimeoutSeconds 120
$receiptName = 'runtime-activation-' + $activated.activation_id + '.json'
$receiptForRollback = Join-Path `
    '{values["state"]}' ('receipts\runtime-activation\' + $receiptName)
. '{rollback_script}'
$rolledBack = Invoke-DawnstrikeRuntimeRollback `
  -ActivationReceipt $receiptForRollback -ContractRoot '{values["candidate"]}' `
  -RuntimeRoot '{values["runtime"]}' -StateRoot '{values["state"]}' `
  -BackupRoot '{values["backup"]}' `
  -ProcessTimeoutSeconds 120
$stateBundlePath = Join-Path '{values["backup"]}' $activated.state_backup_id
$heldStateBundlePath = $stateBundlePath + '.held'
[System.IO.Directory]::Move($stateBundlePath, $heldStateBundlePath)
$rollbackMissingBackupBlocked = $false
try {{
    $null = Invoke-DawnstrikeRuntimeRollback `
      -ActivationReceipt $receiptForRollback `
      -ContractRoot '{values["candidate"]}' `
      -RuntimeRoot '{values["runtime"]}' -StateRoot '{values["state"]}' `
      -BackupRoot '{values["backup"]}' `
      -ProcessTimeoutSeconds 120
}}
catch {{ $rollbackMissingBackupBlocked = $true }}
finally {{ [System.IO.Directory]::Move($heldStateBundlePath, $stateBundlePath) }}
$rolledBackAgain = Invoke-DawnstrikeRuntimeRollback `
  -ActivationReceipt $receiptForRollback -ContractRoot '{values["candidate"]}' `
  -RuntimeRoot '{values["runtime"]}' -StateRoot '{values["state"]}' `
  -BackupRoot '{values["backup"]}' `
  -ProcessTimeoutSeconds 120
$output = [pscustomobject]@{{
    activated=$activated
    activated_again=$activatedAgain
    rolled_back=$rolledBack
    rolled_back_again=$rolledBackAgain
    activation_missing_bundle_blocked=$activationMissingBundleBlocked
    rollback_missing_backup_blocked=$rollbackMissingBackupBlocked
    task_states=$global:MockTaskStates
    task_events=$global:TaskEvents
}}
$output | ConvertTo-Json -Depth 12 -Compress
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=source,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["activated"]["status"] == "COMPLETE"
    assert payload["activated"]["candidate_sha"] == candidate_sha
    assert payload["activated_again"]["receipt_sha256"] == payload["activated"]["receipt_sha256"]
    assert payload["activation_missing_bundle_blocked"] is True
    assert payload["rolled_back"]["status"] == "ROLLED_BACK"
    assert payload["rolled_back"]["restored_sha"] == previous_sha
    assert (
        payload["rolled_back_again"]["receipt_sha256"] == payload["rolled_back"]["receipt_sha256"]
    )
    assert payload["rollback_missing_backup_blocked"] is True
    assert set(payload["task_states"].values()) == {"Ready"}
    assert len(payload["task_events"]) == 20
    assert all(event.startswith("disable:") for event in payload["task_events"][:5])
    assert all(event.startswith("enable:") for event in payload["task_events"][5:10])
    assert all(event.startswith("disable:") for event in payload["task_events"][10:15])
    assert all(event.startswith("enable:") for event in payload["task_events"][15:20])
    assert _git(runtime, "rev-parse", "HEAD") == previous_sha
    assert not _git(runtime, "status", "--porcelain=v1", "--untracked-files=all")
    assert hashlib.sha256(db.read_bytes()).hexdigest() == db_hash_before
    assert not list((state / "locks").glob("*.lock"))
    assert (
        backup / f"runtime-activation-{payload['activated']['activation_id']}" / "receipt.json"
    ).is_file()
    activation_scheduler_backup = (
        state / "scheduler-backups" / payload["activated"]["scheduler_backup_name"]
    )
    rollback_scheduler_backup = (
        state / "scheduler-backups" / payload["rolled_back"]["scheduler_backup_name"]
    )
    assert (activation_scheduler_backup / "manifest.json").is_file()
    assert len(list(activation_scheduler_backup.glob("*.xml"))) == 5
    assert (rollback_scheduler_backup / "manifest.json").is_file()
    assert len(list(rollback_scheduler_backup.glob("*.xml"))) == 5


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
@pytest.mark.parametrize(
    "runtime_contract_mode", ["previous_pre_first", "candidate_missing_checkout"]
)
def test_rollback_entrypoint_handles_pre_first_and_rejects_later_missing_checkout(
    tmp_path: Path, runtime_contract_mode: str
) -> None:
    source = Path.cwd()
    candidate = tmp_path / "candidate"
    runtime = tmp_path / "dawnstrike-runtime"
    state = tmp_path / "state"
    backup = tmp_path / "backups"
    candidate.mkdir()
    runtime.mkdir()
    state.mkdir()

    (candidate / "scripts").mkdir()
    for name in (
        "activate_dawnstrike_runtime.ps1",
        "rollback_dawnstrike_runtime.ps1",
        "runtime_activation_contract.py",
        "dawnstrike_job_process.ps1",
        "invoke_dawnstrike_stage.ps1",
        "state_disaster_recovery.py",
    ):
        shutil.copy2(source / "scripts" / name, candidate / "scripts" / name)
    shutil.copytree(
        source / "intraday_scanner",
        candidate / "intraday_scanner",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    shutil.copy2(source / ".gitignore", candidate / ".gitignore")
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(candidate)],
        check=True,
        capture_output=True,
    )
    _git(candidate, "config", "user.email", "activation-test@example.invalid")
    _git(candidate, "config", "user.name", "Activation Test")
    _git(candidate, "add", ".")
    _git(candidate, "commit", "-m", "candidate")
    origin = "https://github.com/example/dawnstrike.git"
    _git(candidate, "remote", "add", "origin", origin)
    candidate_sha = _git(candidate, "rev-parse", "HEAD")
    candidate_tree = _git(candidate, "rev-parse", "HEAD^{tree}")

    subprocess.run(
        ["git", "init", "--initial-branch=main", str(runtime)],
        check=True,
        capture_output=True,
    )
    _git(runtime, "config", "user.email", "activation-test@example.invalid")
    _git(runtime, "config", "user.name", "Activation Test")
    (runtime / "previous.txt").write_text("previous-runtime\n", encoding="utf-8")
    _git(runtime, "add", "previous.txt")
    _git(runtime, "commit", "-m", "previous")
    _git(runtime, "remote", "add", "origin", origin)
    previous_sha = _git(runtime, "rev-parse", "HEAD")
    previous_tree = _git(runtime, "rev-parse", "HEAD^{tree}")

    db = state / "shadow_real.sqlite"
    with sqlite3.connect(db) as connection:
        run_migrations(connection)
    backup_result = subprocess.run(
        [
            "py",
            str(candidate / "scripts" / "state_disaster_recovery.py"),
            "backup",
            "--source-db",
            str(db),
            "--backup-root",
            str(backup),
            "--state-root",
            str(state),
            "--retention",
            "5",
            "--source-sha",
            previous_sha,
            "--backup-id",
            "runtime-activation-" + "e" * 24,
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    state_backup = json.loads(backup_result.stdout)

    rollback_root = state / "runtime-rollbacks" / ("e" * 24)
    rollback_root.mkdir(parents=True)
    bundle = rollback_root / "previous-runtime.bundle"
    _git(runtime, "bundle", "create", str(bundle), "HEAD")
    bundle_sha = hashlib.sha256(bundle.read_bytes()).hexdigest()
    scheduler_backup = state / "scheduler-backups" / ("runtime-activation-" + "e" * 24)
    scheduler_backup.mkdir(parents=True)
    manifest = scheduler_backup / "manifest.json"
    _write_json(manifest, {"test": "pre-first-rename"})
    manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
    receipt_payload = _receipt_payload(status="PREPARED")
    receipt_payload.update(
        {
            "candidate_sha": candidate_sha,
            "candidate_tree": candidate_tree,
            "previous_sha": previous_sha,
            "previous_tree": previous_tree,
            "rollback_bundle_sha256": bundle_sha,
            "state_backup_id": state_backup["backup_id"],
            "state_backup_db_sha256": state_backup["backup_db_sha256"],
            "state_schema_version": state_backup["schema_version"],
            "state_quick_check": state_backup["quick_check"],
            "scheduler_backup_manifest_sha256": manifest_sha,
            "runtime_origin_sha256": hashlib.sha256(origin.encode()).hexdigest(),
        }
    )
    receipt = state / "receipts" / "runtime-activation" / (
        "runtime-activation-" + "e" * 24 + ".prepared.json"
    )
    seal_receipt(receipt_payload, receipt)

    activation_script = str(
        (candidate / "scripts" / "activate_dawnstrike_runtime.ps1").resolve()
    ).replace("'", "''")
    stage_script = str(
        (candidate / "scripts" / "invoke_dawnstrike_stage.ps1").resolve()
    ).replace("'", "''")
    receipt_text = str(receipt).replace("'", "''")
    state_text = str(state).replace("'", "''")
    seed_command = rf"""
    . '{activation_script}'
    . '{stage_script}'
    $prepared = Get-Content -LiteralPath '{receipt_text}' -Raw | ConvertFrom-Json
    $name = Split-Path -Leaf '{receipt_text}'
    $activationLock = Enter-DawnstrikeRuntimeActivationLock `
        -StateRoot '{state_text}' -ActivationId $prepared.activation_id -PreparedReceiptName $name
    $dailyLock = Enter-DawnstrikeDailyRunLock `
        -StateRoot '{state_text}' -MarketDate $prepared.market_date -Owner 'runtime_activation' `
        -ActivationId $prepared.activation_id -PreparedReceiptName $name
    Set-DawnstrikeReceiptBoundLock `
        -LockPath $activationLock.path -LockToken $activationLock.token `
        -ActivationId $prepared.activation_id `
        -PreparedReceiptPath '{receipt_text}' `
        -PreparedReceiptFileSha256 (Get-DawnstrikeSha256File '{receipt_text}') -Receipt $prepared
    Set-DawnstrikeReceiptBoundLock `
        -LockPath $dailyLock.lock_path -LockToken $dailyLock.lock_token `
        -ActivationId $prepared.activation_id `
        -PreparedReceiptPath '{receipt_text}' `
        -PreparedReceiptFileSha256 (Get-DawnstrikeSha256File '{receipt_text}') -Receipt $prepared
    """
    seeded = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", seed_command],
        cwd=source,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert seeded.returncode == 0, (seeded.stdout, seeded.stderr)

    rollback_script = str(
        (candidate / "scripts" / "rollback_dawnstrike_runtime.ps1").resolve()
    ).replace("'", "''")
    candidate_text = str(candidate).replace("'", "''")
    runtime_text = str(runtime).replace("'", "''")
    backup_text = str(backup).replace("'", "''")
    command = rf"""
    . '{rollback_script}'
    $global:MockTasksEnabled = $false
    $runtimeContractMode = '{runtime_contract_mode}'
    function Get-DawnstrikeGitContract {{
        [CmdletBinding()] param(
            [string]$GitPath,[string]$Root,[int]$TimeoutSeconds,
            [string]$ExpectedCommit=''
        )
        if ([string]::Equals(
            [System.IO.Path]::GetFullPath($Root).TrimEnd('\'),
            [System.IO.Path]::GetFullPath('{candidate_text}').TrimEnd('\'),
            [System.StringComparison]::OrdinalIgnoreCase
        )) {{
            return [pscustomobject]@{{ head='{candidate_sha}'; tree='{candidate_tree}' }}
        }}
        if ($runtimeContractMode -eq 'candidate_missing_checkout') {{
            return [pscustomobject]@{{ head='{candidate_sha}'; tree='{candidate_tree}' }}
        }}
        return [pscustomobject]@{{ head='{previous_sha}'; tree='{previous_tree}' }}
    }}
    function Get-DawnstrikeGitValue {{
        [CmdletBinding()] param(
            [string]$GitPath,[string]$Root,[string[]]$Arguments,
            [string]$Label,[int]$TimeoutSeconds
        )
        return '{origin}'
    }}
    function Get-DawnstrikeTaskContract {{
        [CmdletBinding()] param([string]$RuntimeRoot,[string]$StateRoot,[switch]$AllowDisabled)
        $enabled = [bool]$global:MockTasksEnabled
        return [pscustomobject]@{{
            task_count=5; task_contract_sha256=('5' * 64);
            task_definition_contract_sha256=('9' * 64);
            task_action_contract_sha256=('7' * 64);
            enabled_count=if ($enabled) {{ 5 }} else {{ 0 }};
            disabled_count=if ($enabled) {{ 0 }} else {{ 5 }}
        }}
    }}
    function Assert-DawnstrikeTaskXmlBackup {{
        [CmdletBinding()] param(
            [string]$StateRoot,[string]$BackupName,[string]$ExpectedManifestSha256,
            [string]$ExpectedTaskContractSha256,[string]$ExpectedTaskDefinitionContractSha256,
            [string]$ExpectedTaskActionContractSha256
        )
    }}
    function Enable-DawnstrikeCanonicalTasks {{ $global:MockTasksEnabled = $true }}
    try {{
        $first = Invoke-DawnstrikeRuntimeRollback `
            -ActivationReceipt '{receipt_text}' -ContractRoot '{candidate_text}' `
            -RuntimeRoot '{runtime_text}' -StateRoot '{state_text}' `
            -BackupRoot '{backup_text}' -ProcessTimeoutSeconds 120
    }} catch {{
        [pscustomobject]@{{
            error=$_.Exception.Message;
            current_activation=Test-Path -LiteralPath (
                Join-Path '{state_text}' 'locks\dawnstrike-runtime-activation.lock'
            ) -PathType Leaf
            current_daily=Test-Path -LiteralPath (
                Join-Path '{state_text}' 'locks\dawnstrike-daily-2026-08-31.lock'
            ) -PathType Leaf
            archives=@(Get-ChildItem -LiteralPath (Join-Path '{state_text}' 'locks') `
                -Filter '*.archived.*' -File -Force -ErrorAction SilentlyContinue).Count
        }} |
            ConvertTo-Json -Compress
        exit 0
    }}
    $second = Invoke-DawnstrikeRuntimeRollback `
        -ActivationReceipt '{receipt_text}' -ContractRoot '{candidate_text}' `
        -RuntimeRoot '{runtime_text}' -StateRoot '{state_text}' `
        -BackupRoot '{backup_text}' -ProcessTimeoutSeconds 120
    [pscustomobject]@{{
        first=$first; second=$second; tasks_enabled=[bool]$global:MockTasksEnabled;
        current_activation=Test-Path -LiteralPath (
            Join-Path '{state_text}' 'locks\dawnstrike-runtime-activation.lock'
        ) -PathType Leaf
        current_daily=Test-Path -LiteralPath (
            Join-Path '{state_text}' 'locks\dawnstrike-daily-2026-08-31.lock'
        ) -PathType Leaf
        archives=@(Get-ChildItem -LiteralPath (Join-Path '{state_text}' 'locks') `
            -Filter '*.archived.*' -File -Force -ErrorAction SilentlyContinue).Count
    }} | ConvertTo-Json -Depth 12 -Compress
    """
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=source,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    if runtime_contract_mode == "candidate_missing_checkout":
        assert "first" not in payload
        assert "Previous runtime checkout is missing" in payload["error"]
        assert payload["current_activation"] is True
        assert payload["current_daily"] is True
        assert payload["archives"] == 0
    else:
        assert payload["first"]["status"] == "ROLLED_BACK"
        assert payload["first"]["restored_sha"] == previous_sha
        assert payload["second"]["receipt_sha256"] == payload["first"]["receipt_sha256"]
        assert payload["tasks_enabled"] is True
        assert payload["current_activation"] is False
        assert payload["current_daily"] is False
        assert payload["archives"] == 2
        assert _git(runtime, "rev-parse", "HEAD") == previous_sha
