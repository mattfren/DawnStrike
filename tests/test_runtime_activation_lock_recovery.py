from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "scripts" / "runtime_activation_lock_contract.py"
PS = ROOT / "scripts" / "runtime_activation_lock.ps1"


def load_contract():
    spec = importlib.util.spec_from_file_location("runtime_lock_contract", CONTRACT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def payload() -> dict[str, object]:
    origin = "github.com/mattfren/dawnstrike"
    return {
        "schema_version": "dawnstrike.runtime_activation_lock.v2",
        "operation": "capture_task_hardening",
        "candidate_sha": "b" * 40,
        "candidate_tree": "c" * 40,
        "origin_identity": origin,
        "origin_identity_sha256": hashlib.sha256(origin.encode()).hexdigest(),
        "process_id": 123,
        "process_started_at_utc": "2026-08-31T12:00:00.0000000Z",
        "acquired_at_utc": "2026-08-31T12:00:01.0000000Z",
        "lock_token": "d" * 32,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def test_strict_lock_accepts_exact_contract_and_preserves_raw_hash():
    raw = json.dumps(payload(), separators=(",", ":")).encode()
    assert load_contract().validate(raw)["operation"] == "capture_task_hardening"
    assert hashlib.sha256(raw).hexdigest() != hashlib.sha256(raw + b"\n").hexdigest()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(research_only=False),
        lambda p: p.update(broker_execution_enabled=True),
        lambda p: p.update(process_id="123"),
        lambda p: p.update(origin_identity="https://token@example.invalid/repo"),
        lambda p: p.update(extra=True),
    ],
)
def test_strict_lock_rejects_wrong_types_safety_and_keys(mutation):
    value = payload()
    mutation(value)
    with pytest.raises(ValueError):
        load_contract().validate(json.dumps(value, separators=(",", ":")).encode())


def test_strict_lock_rejects_duplicate_keys():
    raw = json.dumps(payload(), separators=(",", ":"))
    raw = raw[:-1] + ',"lock_token":"' + "e" * 32 + '"}'
    with pytest.raises(ValueError, match="duplicate key"):
        load_contract().validate(raw.encode())


def test_shared_powershell_contract_has_guarded_atomic_adoption():
    source = PS.read_text(encoding="utf-8")
    assert "Global\\DawnstrikeRuntimeActivationLockV2" in source
    assert "Test-DawnstrikeRuntimeLockOwnerDead" in source
    assert "return $true" in source
    assert "[IO.File]::Replace($temp,$path,$archive,$true)" in source
    assert "Get-DawnstrikeRuntimeLockHash $archive" in source
    assert "lock retained" in source
    assert "pscredential" not in source.lower()


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_executed_lock_acquire_and_release(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    module = str(PS).replace("'", "''")
    state_q = str(state).replace("'", "''")
    command = rf"""
. '{module}'
$approved=Get-DawnstrikeApprovedLockInterpreter
$python=$approved.path
$pythonSha=$approved.sha256
$lock=Enter-DawnstrikeGovernedRuntimeLock -StateRoot '{state_q}' -Operation runtime_activation `
  -CandidateSha '{"a" * 40}' -CandidateTree '{"b" * 40}' `
  -OriginIdentity 'github.com/mattfren/dawnstrike' -PythonPath $python -PythonSha256 $pythonSha
if(-not (Test-Path -LiteralPath $lock.path)){{throw 'lock absent'}}
$live=Get-DawnstrikeStrictRuntimeLock $lock.path $python $pythonSha
$activeRejected=$false
try{{
 $null=Adopt-DawnstrikeGovernedRuntimeLock -StateRoot '{state_q}' `
  -ExpectedToken $live.payload.lock_token -ExpectedFileSha256 $live.raw_file_sha256 `
  -ExpectedOperation runtime_activation -CandidateSha '{"a" * 40}' -CandidateTree '{"b" * 40}' `
  -OriginIdentity 'github.com/mattfren/dawnstrike' -PythonPath $python -PythonSha256 $pythonSha
}}catch{{$activeRejected=$_.Exception.Message -match 'still active'}}
if(-not $activeRejected){{throw 'active owner was not rejected'}}
Exit-DawnstrikeGovernedRuntimeLock $lock
if(Test-Path -LiteralPath $lock.path){{throw 'lock retained'}}
"OK"
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "OK" in result.stdout


def test_contract_rejects_non_dawnstrike_origin_even_with_matching_hash():
    value = payload()
    value["origin_identity"] = "github.com/example/dawnstrike"
    value["origin_identity_sha256"] = hashlib.sha256(
        str(value["origin_identity"]).encode()
    ).hexdigest()
    with pytest.raises(ValueError, match="origin identity"):
        load_contract().validate(json.dumps(value, separators=(",", ":")).encode())


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell unavailable")
def test_executed_huge_tampered_and_reparse_locks_are_preserved(tmp_path: Path):
    state = tmp_path / "state"
    locks = state / "locks"
    locks.mkdir(parents=True)
    module = str(PS).replace("'", "''")
    lock_q = str(locks / "dawnstrike-runtime-activation.lock").replace("'", "''")
    target_q = str(locks / "target.lock").replace("'", "''")
    command = rf"""
. '{module}'
$approved=Get-DawnstrikeApprovedLockInterpreter
$path='{lock_q}'
[IO.File]::WriteAllText($path,('x'*17000))
$before=Get-DawnstrikeRuntimeLockHash $path
$rejected=$false
try{{$null=Get-DawnstrikeStrictRuntimeLock $path $approved.path $approved.sha256}}
catch{{$rejected=$true}}
if(-not $rejected){{throw 'huge accepted'}}
if((Get-DawnstrikeRuntimeLockHash $path)-ne $before){{throw 'huge changed'}}
[IO.File]::WriteAllText($path,'{{"schema_version":"x","schema_version":"y"}}')
$before=Get-DawnstrikeRuntimeLockHash $path
$rejected=$false
try{{$null=Get-DawnstrikeStrictRuntimeLock $path $approved.path $approved.sha256}}
catch{{$rejected=$true}}
if(-not $rejected){{throw 'tamper accepted'}}
if((Get-DawnstrikeRuntimeLockHash $path)-ne $before){{throw 'tamper changed'}}
Remove-Item $path -Force
[IO.File]::WriteAllText('{target_q}','x')
try{{
    New-Item -ItemType SymbolicLink -Path $path -Target '{target_q}' `
        -ErrorAction Stop|Out-Null
}}catch{{'SKIP_REPARSE';exit 0}}
$rejected=$false
try{{$null=Get-DawnstrikeStrictRuntimeLock $path $approved.path $approved.sha256}}
catch{{$rejected=$true}}
if(-not $rejected){{throw 'reparse accepted'}}
if(-not ((Get-Item $path -Force).Attributes-band `
    [IO.FileAttributes]::ReparsePoint)){{throw 'reparse changed'}}
'OK'
"""
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "OK" in result.stdout or "SKIP_REPARSE" in result.stdout


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell unavailable")
def test_dead_child_lock_is_atomically_adopted_and_released(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    module = str(PS).replace("'", "''")
    state_q = str(state).replace("'", "''")
    common = rf"""
. '{module}'
$approved=Get-DawnstrikeApprovedLockInterpreter
$python=$approved.path
$pythonSha=$approved.sha256
"""
    child = (
        common
        + rf"""
$null=Enter-DawnstrikeGovernedRuntimeLock -StateRoot '{state_q}' -Operation runtime_activation `
 -CandidateSha '{"a" * 40}' -CandidateTree '{"b" * 40}' `
 -OriginIdentity 'github.com/mattfren/dawnstrike' `
 -PythonPath $python -PythonSha256 $pythonSha
$held=Enter-DawnstrikeRuntimeLockMutex
exit 137
"""
    )
    first = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", child],
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert first.returncode != 0
    adopt = (
        common
        + rf"""
$path=Join-Path '{state_q}' 'locks\dawnstrike-runtime-activation.lock'
$stale=Get-DawnstrikeStrictRuntimeLock $path $python $pythonSha
$lock=Adopt-DawnstrikeGovernedRuntimeLock -StateRoot '{state_q}' `
 -ExpectedToken $stale.payload.lock_token -ExpectedFileSha256 $stale.raw_file_sha256 `
 -ExpectedOperation runtime_activation -CandidateSha '{"a" * 40}' -CandidateTree '{"b" * 40}' `
 -OriginIdentity 'github.com/mattfren/dawnstrike' -PythonPath $python -PythonSha256 $pythonSha
if((Get-DawnstrikeRuntimeLockHash $lock.stale_archive) -ne $stale.raw_file_sha256){{
 throw 'archive mismatch'
}}
Exit-DawnstrikeGovernedRuntimeLock $lock
"OK"
"""
    )
    second = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", adopt],
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert second.returncode == 0, (second.stdout, second.stderr)
    assert "OK" in second.stdout


@pytest.mark.skipif(shutil.which("powershell") is None, reason="PowerShell unavailable")
def test_journal_adoption_survives_two_consecutive_crashes(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    module = str(PS.resolve()).replace("'", "''")
    state_q = str(state).replace("'", "''")
    create = rf"""
. '{module}'
$approved=Get-DawnstrikeApprovedLockInterpreter
$lock=Enter-DawnstrikeGovernedRuntimeLock -StateRoot '{state_q}' `
 -Operation runtime_activation -CandidateSha ('a'*40) -CandidateTree ('b'*40) `
 -OriginIdentity 'github.com/mattfren/dawnstrike' `
 -PythonPath $approved.path -PythonSha256 $approved.sha256
$lock|ConvertTo-Json -Compress
exit 137
"""
    created = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", create],
        text=True, capture_output=True, check=False,
    )
    assert created.returncode == 137, created.stderr
    handle = json.loads(created.stdout.strip().splitlines()[-1])
    empty = hashlib.sha256(b"").hexdigest()
    origin = "github.com/mattfren/dawnstrike"
    journal_input = state / "journal-input.json"
    journal = state / "receipts" / "runtime-operation" / "activation.json"
    data = {
        "schema_version": "dawnstrike.runtime_operation_journal.v1",
        "operation": "runtime_activation", "phase": "INIT", "sequence": 0,
        "candidate_sha": "a" * 40, "candidate_tree": "b" * 40,
        "current_sha": "e" * 40, "current_tree": "f" * 40,
        "previous_sha": "e" * 40, "previous_tree": "f" * 40,
        "origin_identity": origin,
        "origin_identity_sha256": hashlib.sha256(origin.encode()).hexdigest(),
        "state_root_sha256": hashlib.sha256(
            str(state.resolve()).rstrip("\\").lower().encode()
        ).hexdigest(),
        "lock_token": handle["token"],
        "lock_file_sha256": handle["bytes_sha256"],
        "prior_journal_file_sha256": empty,
        "prepared_receipt_relative_path": "receipts/runtime-activation/prepared.json",
        "prepared_receipt_sha256": empty,
        "complete_receipt_relative_path": "receipts/runtime-activation/complete.json",
        "complete_receipt_sha256": empty, "backup_contract_sha256": empty,
        "task_contract_sha256": "5" * 64,
        "runtime_stage_contract_sha256": empty,
        "recorded_at_utc": "2026-08-31T23:01:02.1234567Z",
        "research_only": True, "broker_execution_enabled": False,
        "adoption_state": "NONE", "old_lock_token": handle["token"],
        "old_lock_file_sha256": handle["bytes_sha256"],
        "next_lock_token": handle["token"],
        "next_lock_file_sha256": handle["bytes_sha256"],
        "old_lock_archive_relative_path": "NONE",
        "next_lock_relative_path": "NONE",
        "init_owner_process_id": 1234,
        "init_owner_started_at_utc": "2026-08-31T23:01:01.1234567Z",
    }
    journal_input.write_text(json.dumps(data), encoding="utf-8")
    subprocess.run(
        [
            "py", "-3.13", str(ROOT / "scripts" / "runtime_operation_journal.py"),
            "seal", str(journal_input), str(journal), "--state-root", str(state),
        ],
        check=True, capture_output=True, text=True,
    )

    def adopt(crash: str = "") -> subprocess.CompletedProcess[str]:
        crash_arg = f" -TestCrashPoint {crash}" if crash else ""
        journal_q = str(journal).replace("'", "''")
        command = rf"""
. '{module}'
$approved=Get-DawnstrikeApprovedLockInterpreter
$lock=Adopt-DawnstrikeGovernedRuntimeLockWithJournal -StateRoot '{state_q}' `
 -JournalPath '{journal_q}' -CandidateSha ('a'*40) -CandidateTree ('b'*40) `
 -OriginIdentity 'github.com/mattfren/dawnstrike' `
 -PythonPath $approved.path -PythonSha256 $approved.sha256{crash_arg}
$lock|ConvertTo-Json -Compress
"""
        environment = dict(os.environ)
        environment["DAWNSTRIKE_TEST_LOCK_JOURNAL"] = "1"
        return subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            text=True, capture_output=True, check=False, env=environment,
        )

    assert adopt("after_prepared").returncode == 137
    assert adopt("after_replace").returncode == 137
    recovered = adopt()
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    recovered_handle = json.loads(recovered.stdout.strip().splitlines()[-1])
    assert recovered_handle["token"] != handle["token"]
    lock_payload = json.loads(
        (state / "locks" / "dawnstrike-runtime-activation.lock").read_text()
    )
    # The standalone recovery process exits after returning, but the lock must
    # have been minted by that third process rather than either crashed child.
    assert lock_payload["lock_token"] == recovered_handle["token"]


@pytest.mark.skipif(shutil.which("powershell") is None, reason="PowerShell unavailable")
def test_operation_journal_enforces_adjacent_owned_transitions(tmp_path: Path) -> None:
    state_q = str(tmp_path / "state").replace("'", "''")
    module = str(PS.resolve()).replace("'", "''")
    command = rf"""
. '{module}'
$approved=Get-DawnstrikeApprovedLockInterpreter
$origin='github.com/mattfren/dawnstrike';$empty=Get-DawnstrikeSharedLockSha256Text ''
$lock=Enter-DawnstrikeGovernedRuntimeLock -StateRoot '{state_q}' `
 -Operation runtime_activation -CandidateSha ('a'*40) -CandidateTree ('b'*40) `
 -OriginIdentity $origin -PythonPath $approved.path -PythonSha256 $approved.sha256
$journal=Join-Path '{state_q}' 'receipts\runtime-operation\activation.json'
$common=@{{StateRoot='{state_q}';JournalPath=$journal;Lock=$lock;Operation='runtime_activation';
 CandidateSha=('a'*40);CandidateTree=('b'*40);CurrentSha=('e'*40);CurrentTree=('f'*40);
 PreviousSha=('e'*40);PreviousTree=('f'*40);OriginIdentity=$origin;
 PreparedReceiptRelativePath='receipts/runtime-activation/prepared.json';
 CompleteReceiptRelativePath='receipts/runtime-activation/complete.json';TaskContractSha256=('5'*64);
 PythonPath=$approved.path;PythonSha256=$approved.sha256}}
$null=Set-DawnstrikeRuntimeOperationJournalPhase @common -Phase INIT `
 -PreparedReceiptSha256 $empty -CompleteReceiptSha256 $empty `
 -BackupContractSha256 $empty -RuntimeStageContractSha256 $empty
$skip=$false
try{{$null=Set-DawnstrikeRuntimeOperationJournalPhase @common -Phase POST_SWAP `
 -PreparedReceiptSha256 ('1'*64) -CompleteReceiptSha256 $empty `
 -BackupContractSha256 ('2'*64) `
 -RuntimeStageContractSha256 ('3'*64)}}catch{{$skip=$true}}
$cross=$false;$bad=@{{}}+$common;$bad.Operation='runtime_rollback'
try{{$null=Set-DawnstrikeRuntimeOperationJournalPhase @bad -Phase PRE_SWAP `
 -PreparedReceiptSha256 ('1'*64) -CompleteReceiptSha256 $empty `
 -BackupContractSha256 ('2'*64) `
 -RuntimeStageContractSha256 ('3'*64)}}catch{{$cross=$true}}
$fake=[pscustomobject]@{{path=$lock.path;token=('0'*32);bytes_sha256=$lock.bytes_sha256}}
$mismatch=$false;$badLock=@{{}}+$common;$badLock.Lock=$fake
try{{$null=Set-DawnstrikeRuntimeOperationJournalPhase @badLock -Phase PRE_SWAP `
 -PreparedReceiptSha256 ('1'*64) -CompleteReceiptSha256 $empty `
 -BackupContractSha256 ('2'*64) `
 -RuntimeStageContractSha256 ('3'*64)}}catch{{$mismatch=$true}}
$null=Set-DawnstrikeRuntimeOperationJournalPhase @common -Phase PRE_SWAP `
 -PreparedReceiptSha256 ('1'*64) -CompleteReceiptSha256 $empty `
 -BackupContractSha256 ('2'*64) -RuntimeStageContractSha256 ('3'*64)
$raw=[IO.File]::ReadAllText($journal)
[IO.File]::WriteAllText($journal,$raw.Replace(('1'*64),('9'*64)))
$tamper=$false
try{{$null=Set-DawnstrikeRuntimeOperationJournalPhase @common -Phase POST_SWAP `
 -PreparedReceiptSha256 ('1'*64) -CompleteReceiptSha256 $empty `
 -BackupContractSha256 ('2'*64) `
 -RuntimeStageContractSha256 ('3'*64)}}catch{{$tamper=$true}}
if(-not($skip-and$cross-and$mismatch-and$tamper)){{throw 'hostile transition accepted'}}
Exit-DawnstrikeGovernedRuntimeLock $lock
'OK'
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        text=True, capture_output=True, check=False, timeout=60,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "OK" in result.stdout


@pytest.mark.skipif(shutil.which("powershell") is None, reason="PowerShell unavailable")
@pytest.mark.parametrize("crash", ["after_init", "after_lock"])
def test_journal_aware_enter_recovers_acquisition_crashes(
    tmp_path: Path, crash: str
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    module = str(PS.resolve()).replace("'", "''")
    state_q = str(state).replace("'", "''")
    journal_q = str(
        state / "receipts" / "runtime-operation" / "activation.json"
    ).replace("'", "''")

    def command(crash_point: str = "", cleanup: bool = False) -> str:
        crash_arg = f" -TestCrashPoint {crash_point}" if crash_point else ""
        cleanup_code = (
            "$token=$lock.token;Exit-DawnstrikeGovernedRuntimeLock $lock;"
            f"Remove-Item -LiteralPath '{journal_q}' -Force;"
            "$token"
            if cleanup
            else "$lock.token"
        )
        return rf"""
. '{module}'
$approved=Get-DawnstrikeApprovedLockInterpreter
$lock=Enter-DawnstrikeGovernedRuntimeLockWithJournal -StateRoot '{state_q}' `
 -JournalPath '{journal_q}' -Operation runtime_activation `
 -CandidateSha ('a'*40) -CandidateTree ('b'*40) `
 -CurrentSha ('e'*40) -CurrentTree ('f'*40) `
 -PreviousSha ('e'*40) -PreviousTree ('f'*40) `
 -OriginIdentity 'github.com/mattfren/dawnstrike' `
 -PreparedReceiptRelativePath 'receipts/runtime-activation/prepared.json' `
 -CompleteReceiptRelativePath 'receipts/runtime-activation/complete.json' `
 -TaskContractSha256 ('5'*64) -PythonPath $approved.path `
 -PythonSha256 $approved.sha256{crash_arg}
{cleanup_code}
"""

    environment = dict(os.environ)
    environment["DAWNSTRIKE_TEST_LOCK_JOURNAL"] = "1"
    crashed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command(crash)],
        text=True, capture_output=True, check=False, env=environment,
    )
    assert crashed.returncode != 0, (crashed.stdout, crashed.stderr)
    recovered = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command(cleanup=True)],
        text=True, capture_output=True, check=False, env=environment,
    )
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    assert not (state / "locks" / "dawnstrike-runtime-activation.lock").exists()
    assert not (state / "receipts" / "runtime-operation" / "activation.json").exists()


@pytest.mark.skipif(shutil.which("powershell") is None, reason="PowerShell unavailable")
def test_journal_aware_enter_rejects_live_owner(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    module = str(PS.resolve()).replace("'", "''")
    state_q = str(state).replace("'", "''")
    journal_q = str(
        state / "receipts" / "runtime-operation" / "activation.json"
    ).replace("'", "''")
    base = rf"""
. '{module}'
$approved=Get-DawnstrikeApprovedLockInterpreter
$lock=Enter-DawnstrikeGovernedRuntimeLockWithJournal -StateRoot '{state_q}' `
 -JournalPath '{journal_q}' -Operation runtime_activation `
 -CandidateSha ('a'*40) -CandidateTree ('b'*40) `
 -CurrentSha ('e'*40) -CurrentTree ('f'*40) `
 -PreviousSha ('e'*40) -PreviousTree ('f'*40) `
 -OriginIdentity 'github.com/mattfren/dawnstrike' `
 -PreparedReceiptRelativePath 'receipts/runtime-activation/prepared.json' `
 -CompleteReceiptRelativePath 'receipts/runtime-activation/complete.json' `
 -TaskContractSha256 ('5'*64) -PythonPath $approved.path `
 -PythonSha256 $approved.sha256
"""
    owner = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", base + "Start-Sleep 30"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    lock_path = state / "locks" / "dawnstrike-runtime-activation.lock"
    for _ in range(100):
        if lock_path.exists():
            break
        time.sleep(0.05)
    assert lock_path.exists()
    contender = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", base],
        text=True, capture_output=True, check=False, timeout=20,
    )
    assert contender.returncode != 0
    assert "still active" in contender.stderr
    owner.kill()
    owner.wait(timeout=10)
