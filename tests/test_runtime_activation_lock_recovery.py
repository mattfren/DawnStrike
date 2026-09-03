from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "scripts" / "runtime_activation_lock_contract.py"
PS = ROOT / "scripts" / "runtime_activation_lock.ps1"
WINDOWS_RUNTIME = pytest.mark.skipif(
    os.name != "nt",
    reason="runtime lock execution requires the governed Windows interpreter",
)


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


def test_strict_lock_cli_accepts_only_explicit_captured_bytes() -> None:
    raw = json.dumps(payload(), separators=(",", ":")).encode()
    encoded = base64.b64encode(raw).decode("ascii")
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", str(CONTRACT), "--captured-base64", encoded],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["raw_file_sha256"] == hashlib.sha256(raw).hexdigest()
    ambiguous = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-S",
            str(CONTRACT),
            str(CONTRACT),
            "--captured-base64",
            encoded,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert ambiguous.returncode != 0


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
    assert "RuntimeLockNative]::RenameNoReplace" in source
    assert "RuntimeLockNative]::MarkDelete" in source
    assert "Open-DawnstrikeRetainedRuntimeLockRoot" in source
    assert "Confirm-DawnstrikeGovernedRuntimeLock" in source
    assert "Get-DawnstrikeRuntimeLockHash $archive" in source
    assert "lock retained" in source
    assert "pscredential" not in source.lower()


@WINDOWS_RUNTIME
def test_executed_lock_acquire_and_release(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    module = str(PS).replace("'", "''")
    state_q = str(state).replace("'", "''")
    python_q = str(Path(sys.executable).resolve()).replace("'", "''")
    command = rf"""
. '{module}'
$script:DawnstrikeApprovedPythonPath='{python_q}'
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


@WINDOWS_RUNTIME
def test_executed_huge_tampered_and_reparse_locks_are_preserved(tmp_path: Path):
    state = tmp_path / "state"
    locks = state / "locks"
    locks.mkdir(parents=True)
    module = str(PS).replace("'", "''")
    python_q = str(Path(sys.executable).resolve()).replace("'", "''")
    lock_q = str(locks / "dawnstrike-runtime-activation.lock").replace("'", "''")
    target_q = str(locks / "target.lock").replace("'", "''")
    command = rf"""
. '{module}'
$script:DawnstrikeApprovedPythonPath='{python_q}'
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


@WINDOWS_RUNTIME
def test_dead_child_lock_is_atomically_adopted_and_released(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    module = str(PS).replace("'", "''")
    state_q = str(state).replace("'", "''")
    python_q = str(Path(sys.executable).resolve()).replace("'", "''")
    common = rf"""
. '{module}'
$script:DawnstrikeApprovedPythonPath='{python_q}'
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


@WINDOWS_RUNTIME
@pytest.mark.parametrize("mode", ["new", "adopted"])
def test_retained_runtime_lock_blocks_hostile_path_and_root_substitution(
    tmp_path: Path, mode: str
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    module = str(PS.resolve()).replace("'", "''")
    state_q = str(state).replace("'", "''")
    python_q = str(Path(sys.executable).resolve()).replace("'", "''")
    ready = tmp_path / "owner-ready"
    release = tmp_path / "owner-release"
    ready_q = str(ready).replace("'", "''")
    release_q = str(release).replace("'", "''")
    common = rf"""
. '{module}'
$script:DawnstrikeApprovedPythonPath='{python_q}'
$approved=Get-DawnstrikeApprovedLockInterpreter
"""
    if mode == "adopted":
        seed = common + rf"""
$null=Enter-DawnstrikeGovernedRuntimeLock -StateRoot '{state_q}' `
 -Operation runtime_activation -CandidateSha ('a'*40) -CandidateTree ('b'*40) `
 -OriginIdentity 'github.com/mattfren/dawnstrike' `
 -PythonPath $approved.path -PythonSha256 $approved.sha256
exit 137
"""
        seeded = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", seed],
            text=True,
            capture_output=True,
            check=False,
        )
        assert seeded.returncode == 137, (seeded.stdout, seeded.stderr)
        acquire = rf"""
$path=Join-Path '{state_q}' 'locks\dawnstrike-runtime-activation.lock'
$stale=Get-DawnstrikeStrictRuntimeLock $path $approved.path $approved.sha256
$lock=Adopt-DawnstrikeGovernedRuntimeLock -StateRoot '{state_q}' `
 -ExpectedToken $stale.payload.lock_token `
 -ExpectedFileSha256 $stale.raw_file_sha256 `
 -ExpectedOperation runtime_activation -CandidateSha ('a'*40) -CandidateTree ('b'*40) `
 -OriginIdentity 'github.com/mattfren/dawnstrike' `
 -PythonPath $approved.path -PythonSha256 $approved.sha256
"""
    else:
        acquire = rf"""
$lock=Enter-DawnstrikeGovernedRuntimeLock -StateRoot '{state_q}' `
 -Operation runtime_activation -CandidateSha ('a'*40) -CandidateTree ('b'*40) `
 -OriginIdentity 'github.com/mattfren/dawnstrike' `
 -PythonPath $approved.path -PythonSha256 $approved.sha256
"""
    owner_command = common + acquire + rf"""
if(-not $lock.retained -or $null-eq$lock.retained_handle -or $null-eq$lock.root_handle){{
 throw 'retained runtime handle identity is absent'
}}
[IO.File]::WriteAllText('{ready_q}',[string]$lock.token)
while(-not(Test-Path -LiteralPath '{release_q}')){{Start-Sleep -Milliseconds 25}}
$null=Confirm-DawnstrikeGovernedRuntimeLock $lock
Exit-DawnstrikeGovernedRuntimeLock $lock
'OWNER_OK'
"""
    owner = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", owner_command],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    for _ in range(400):
        if ready.exists() or owner.poll() is not None:
            break
        time.sleep(0.025)
    assert ready.exists(), owner.communicate(timeout=5)

    lock_path = state / "locks" / "dawnstrike-runtime-activation.lock"
    lock_root = lock_path.parent
    replacement = lock_root / "same-metadata-replacement.lock"
    lock_q = str(lock_path).replace("'", "''")
    root_q = str(lock_root).replace("'", "''")
    replacement_q = str(replacement).replace("'", "''")
    moved_root_q = str(state / "locks-moved").replace("'", "''")
    renamed_q = str(lock_root / "renamed-runtime.lock").replace("'", "''")
    backup_q = str(lock_root / "hostile-backup.lock").replace("'", "''")
    attacker_command = rf"""
$ErrorActionPreference='Stop'
$path='{lock_q}';$root='{root_q}';$replacement='{replacement_q}'
try{{
    $share=[IO.FileShare]::ReadWrite-bor[IO.FileShare]::Delete
    $reader=[IO.File]::Open($path,[IO.FileMode]::Open,[IO.FileAccess]::Read,$share)
    try{{
        $raw=[byte[]]::new([int]$reader.Length);$offset=0
        while($offset-lt$raw.Length){{
            $count=$reader.Read($raw,$offset,$raw.Length-$offset)
            if($count-le0){{throw 'short read'}}
            $offset+=$count
        }}
    }}finally{{$reader.Dispose()}}
    $raw[$raw.Length-1]=if($raw[$raw.Length-1]-eq[byte][char]'{{'){{[byte][char]'}}'}}else{{[byte][char]'{{'}}
    [IO.File]::WriteAllBytes($replacement,$raw)
    [IO.File]::SetLastWriteTimeUtc($replacement,(Get-Item -LiteralPath $path).LastWriteTimeUtc)
    $writeBlocked=$false
    try{{[IO.File]::WriteAllBytes($path,[IO.File]::ReadAllBytes($replacement))}}catch{{$writeBlocked=$true}}
    $timeBlocked=$false
    try{{[IO.File]::SetLastWriteTimeUtc($path,[DateTime]::UtcNow)}}catch{{$timeBlocked=$true}}
    $deleteBlocked=$false
    try{{Remove-Item -LiteralPath $path -Force -ErrorAction Stop}}catch{{$deleteBlocked=$true}}
    $renameBlocked=$false
    try{{[IO.File]::Move($path,'{renamed_q}')}}catch{{$renameBlocked=$true}}
    $replaceBlocked=$false
    try{{[IO.File]::Replace($replacement,$path,'{backup_q}',$true)}}catch{{$replaceBlocked=$true}}
    $rootBlocked=$false
    try{{[IO.Directory]::Move($root,'{moved_root_q}')}}catch{{$rootBlocked=$true}}
    $reacquireBlocked=$false
    try{{
     $stream=[IO.File]::Open($path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
     $stream.Dispose()
    }}catch{{$reacquireBlocked=$true}}
    if(-not($writeBlocked-and$timeBlocked-and$deleteBlocked-and$renameBlocked-and$replaceBlocked-and$rootBlocked-and$reacquireBlocked)){{
     throw 'retained runtime lock allowed hostile mutation'
    }}
    'ATTACKS_BLOCKED'
}}finally{{[IO.File]::WriteAllText('{release_q}','release')}}
"""
    attacked = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", attacker_command],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert attacked.returncode == 0, (attacked.stdout, attacked.stderr)
    assert "ATTACKS_BLOCKED" in attacked.stdout
    stdout, stderr = owner.communicate(timeout=30)
    assert owner.returncode == 0, (stdout, stderr)
    assert "OWNER_OK" in stdout
    assert not lock_path.exists()
    assert lock_root.is_dir()
    assert replacement.is_file()


@WINDOWS_RUNTIME
def test_journal_adoption_survives_two_consecutive_crashes(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    module = str(PS.resolve()).replace("'", "''")
    state_q = str(state).replace("'", "''")
    python_q = str(Path(sys.executable).resolve()).replace("'", "''")
    create = rf"""
. '{module}'
$script:DawnstrikeApprovedPythonPath='{python_q}'
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
    initial_journal_bytes = journal.read_bytes()
    initial_journal_hash = hashlib.sha256(initial_journal_bytes).hexdigest()

    def adopt(crash: str = "") -> subprocess.CompletedProcess[str]:
        crash_arg = f" -TestCrashPoint {crash}" if crash else ""
        journal_q = str(journal).replace("'", "''")
        command = rf"""
. '{module}'
$script:DawnstrikeApprovedPythonPath='{python_q}'
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
    prepared_payload = json.loads(journal.read_text(encoding="utf-8"))
    assert prepared_payload["prior_journal_file_sha256"] == initial_journal_hash
    initial_lineage = (
        journal.parent
        / "adoption-lineage"
        / f"adoption-predecessor-{initial_journal_hash}.json"
    )
    assert initial_lineage.read_bytes() == initial_journal_bytes
    assert adopt("after_archive").returncode == 137
    lock_root = state / "locks"
    assert not (lock_root / "dawnstrike-runtime-activation.lock").exists()
    prepared_after_archive = json.loads(journal.read_text(encoding="utf-8"))
    assert (state / prepared_after_archive["old_lock_archive_relative_path"]).is_file()
    assert (state / prepared_after_archive["next_lock_relative_path"]).is_file()
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
    final_payload = json.loads(journal.read_text(encoding="utf-8"))
    final_predecessor = final_payload["prior_journal_file_sha256"]
    final_lineage = (
        journal.parent
        / "adoption-lineage"
        / f"adoption-predecessor-{final_predecessor}.json"
    )
    assert hashlib.sha256(final_lineage.read_bytes()).hexdigest() == final_predecessor


@WINDOWS_RUNTIME
def test_compensated_journal_adoption_preserves_receipt_predecessor_and_records_lineage(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    module = str(PS.resolve()).replace("'", "''")
    state_q = str(state).replace("'", "''")
    python_q = str(Path(sys.executable).resolve()).replace("'", "''")
    create = rf"""
. '{module}'
$script:DawnstrikeApprovedPythonPath='{python_q}'
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
    compensation_predecessor = "c" * 64
    origin = "github.com/mattfren/dawnstrike"
    journal_input = state / "journal-input.json"
    journal = state / "receipts" / "runtime-operation" / "activation.json"
    data = {
        "schema_version": "dawnstrike.runtime_operation_journal.v2",
        "operation": "runtime_activation", "phase": "COMPENSATED", "sequence": 6,
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
        "prior_journal_file_sha256": compensation_predecessor,
        "prepared_receipt_relative_path": "receipts/runtime-activation/prepared.json",
        "prepared_receipt_sha256": "1" * 64,
        "complete_receipt_relative_path": "receipts/runtime-activation/complete.json",
        "complete_receipt_sha256": empty,
        "compensation_receipt_relative_path": (
            "receipts/runtime-activation/compensated.json"
        ),
        "compensation_receipt_sha256": "9" * 64,
        "backup_contract_sha256": "2" * 64,
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
    initial_journal_bytes = journal.read_bytes()
    initial_journal_hash = hashlib.sha256(initial_journal_bytes).hexdigest()

    def adopt(crash: str = "") -> subprocess.CompletedProcess[str]:
        crash_arg = f" -TestCrashPoint {crash}" if crash else ""
        journal_q = str(journal).replace("'", "''")
        command = rf"""
. '{module}'
$script:DawnstrikeApprovedPythonPath='{python_q}'
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
    first_adoption_hash = hashlib.sha256(journal.read_bytes()).hexdigest()
    assert json.loads(journal.read_text(encoding="utf-8"))[
        "prior_journal_file_sha256"
    ] == compensation_predecessor
    assert adopt("after_replace").returncode == 137
    assert hashlib.sha256(journal.read_bytes()).hexdigest() == first_adoption_hash
    recovered = adopt()
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)

    final_payload = json.loads(journal.read_text(encoding="utf-8"))
    assert final_payload["phase"] == "COMPENSATED"
    assert final_payload["adoption_state"] == "ADOPTED"
    assert final_payload["prior_journal_file_sha256"] == compensation_predecessor

    lineage = sorted((journal.parent / "adoption-lineage").glob("*.json"))
    assert len(lineage) == 3
    archived_hashes = set()
    for archived in lineage:
        archived_hash = archived.name.removeprefix(
            "adoption-predecessor-"
        ).removesuffix(".json")
        assert hashlib.sha256(archived.read_bytes()).hexdigest() == archived_hash
        archived_hashes.add(archived_hash)
    assert initial_journal_hash in archived_hashes
    assert first_adoption_hash in archived_hashes
    assert (
        journal.parent
        / "adoption-lineage"
        / f"adoption-predecessor-{initial_journal_hash}.json"
    ).read_bytes() == initial_journal_bytes


@WINDOWS_RUNTIME
def test_operation_journal_enforces_adjacent_owned_transitions(tmp_path: Path) -> None:
    state_q = str(tmp_path / "state").replace("'", "''")
    module = str(PS.resolve()).replace("'", "''")
    python_q = str(Path(sys.executable).resolve()).replace("'", "''")
    command = rf"""
. '{module}'
$script:DawnstrikeApprovedPythonPath='{python_q}'
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
$null=Set-DawnstrikeRuntimeOperationJournalPhase @common -Phase PRE_QUIESCE `
 -PreparedReceiptSha256 $empty -CompleteReceiptSha256 $empty `
 -BackupContractSha256 ('2'*64) -RuntimeStageContractSha256 ('3'*64)
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


@WINDOWS_RUNTIME
@pytest.mark.parametrize("crash", ["after_init", "after_lock"])
def test_journal_aware_enter_recovers_acquisition_crashes(
    tmp_path: Path, crash: str
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    module = str(PS.resolve()).replace("'", "''")
    state_q = str(state).replace("'", "''")
    python_q = str(Path(sys.executable).resolve()).replace("'", "''")
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
$script:DawnstrikeApprovedPythonPath='{python_q}'
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
    if crash == "after_init":
        for wrong in (
            command().replace("-CurrentSha ('e'*40)", "-CurrentSha ('d'*40)"),
            command().replace(
                "receipts/runtime-activation/prepared.json",
                "receipts/runtime-activation/other-prepared.json",
            ),
        ):
            mismatch = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", wrong],
                text=True, capture_output=True, check=False, env=environment,
            )
            assert mismatch.returncode != 0
            assert "identity is invalid" in mismatch.stderr
    recovered = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command(cleanup=True)],
        text=True, capture_output=True, check=False, env=environment,
    )
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    assert not (state / "locks" / "dawnstrike-runtime-activation.lock").exists()
    assert not (state / "receipts" / "runtime-operation" / "activation.json").exists()


@WINDOWS_RUNTIME
def test_journal_aware_enter_rejects_live_owner(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    module = str(PS.resolve()).replace("'", "''")
    state_q = str(state).replace("'", "''")
    python_q = str(Path(sys.executable).resolve()).replace("'", "''")
    journal_q = str(
        state / "receipts" / "runtime-operation" / "activation.json"
    ).replace("'", "''")
    base = rf"""
. '{module}'
$script:DawnstrikeApprovedPythonPath='{python_q}'
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


@WINDOWS_RUNTIME
def test_journal_aware_enter_enforces_daily_lock_and_task_hash(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    module = str(PS.resolve()).replace("'", "''")
    state_q = str(state).replace("'", "''")
    python_q = str(Path(sys.executable).resolve()).replace("'", "''")
    command = rf"""
. '{module}'
$script:DawnstrikeApprovedPythonPath='{python_q}'
$approved=Get-DawnstrikeApprovedLockInterpreter
$journal=Join-Path '{state_q}' 'receipts\runtime-operation\activation.json'
$common=@{{StateRoot='{state_q}';JournalPath=$journal;Operation='runtime_activation';
 CandidateSha=('a'*40);CandidateTree=('b'*40);CurrentSha=('e'*40);CurrentTree=('f'*40);
 PreviousSha=('e'*40);PreviousTree=('f'*40);OriginIdentity='github.com/mattfren/dawnstrike';
 PreparedReceiptRelativePath='receipts/runtime-activation/prepared.json';
 CompleteReceiptRelativePath='receipts/runtime-activation/complete.json';
 TaskContractSha256=('5'*64);PythonPath=$approved.path;PythonSha256=$approved.sha256}}
$locks=Join-Path '{state_q}' 'locks';New-Item $locks -ItemType Directory -Force|Out-Null
$daily=Join-Path $locks 'dawnstrike-daily-before.lock';[IO.File]::WriteAllText($daily,'test')
$before=$false
try{{$null=Enter-DawnstrikeGovernedRuntimeLockWithJournal @common}}
catch{{$before=$_.Exception.Message-match'daily run lock'}}
Remove-Item $daily -Force
$race=$false
try{{$null=Enter-DawnstrikeGovernedRuntimeLockWithJournal `
 @common -TestInjectDailyLockRace}}
catch{{$race=$_.Exception.Message-match'appeared'}}
Remove-Item (Join-Path $locks 'dawnstrike-daily-injected.lock') -Force
$empty=$false;$bad=@{{}}+$common;$bad.TaskContractSha256=Get-DawnstrikeSharedLockSha256Text ''
try{{$null=Enter-DawnstrikeGovernedRuntimeLockWithJournal @bad}}
catch{{$empty=$_.Exception.Message-match'nonempty'}}
if(-not($before-and$race-and$empty)){{throw 'journal initializer guard failed'}}
$lockPath=Join-Path $locks 'dawnstrike-runtime-activation.lock'
if((Test-Path $journal)-or(Test-Path $lockPath)){{
 throw 'guard left mutation evidence'
}}
'OK'
"""
    environment = dict(os.environ)
    environment["DAWNSTRIKE_TEST_LOCK_JOURNAL"] = "1"
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        text=True, capture_output=True, check=False, env=environment,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "OK" in result.stdout


@WINDOWS_RUNTIME
@pytest.mark.parametrize("kind", ["journal", "lock"])
def test_journal_aware_enter_rejects_reparse_components(
    tmp_path: Path, kind: str
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    if kind == "journal":
        (state / "receipts").mkdir()
        link = state / "receipts" / "runtime-operation"
    else:
        link = state / "locks"
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError as symlink_error:
        junction = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "New-Item -ItemType Junction -Path "
                    f"'{str(link).replace(chr(39), chr(39) * 2)}' -Target "
                    f"'{str(target).replace(chr(39), chr(39) * 2)}' | Out-Null"
                ),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if junction.returncode != 0:
            pytest.skip(
                "directory symlink and junction unavailable: "
                f"{symlink_error}; {junction.stderr.strip()}"
            )
    module = str(PS.resolve()).replace("'", "''")
    state_q = str(state).replace("'", "''")
    python_q = str(Path(sys.executable).resolve()).replace("'", "''")
    journal_q = str(
        state / "receipts" / "runtime-operation" / "activation.json"
    ).replace("'", "''")
    command = rf"""
. '{module}'
$script:DawnstrikeApprovedPythonPath='{python_q}'
$approved=Get-DawnstrikeApprovedLockInterpreter
$null=Enter-DawnstrikeGovernedRuntimeLockWithJournal -StateRoot '{state_q}' `
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
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert "reparse" in result.stderr.lower() or "governed receipt root" in result.stderr


@WINDOWS_RUNTIME
def test_adopted_lock_transitions_preserve_original_init_owner(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    module = str(PS.resolve()).replace("'", "''")
    state_q = str(state).replace("'", "''")
    python_q = str(Path(sys.executable).resolve()).replace("'", "''")
    journal = state / "receipts" / "runtime-operation" / "activation.json"
    journal_q = str(journal).replace("'", "''")
    acquire = rf"""
. '{module}'
$script:DawnstrikeApprovedPythonPath='{python_q}'
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
    environment = dict(os.environ)
    environment["DAWNSTRIKE_TEST_LOCK_JOURNAL"] = "1"
    crashed = subprocess.run(
        [
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            acquire.replace("-PythonSha256 $approved.sha256", (
                "-PythonSha256 $approved.sha256 -TestCrashPoint after_lock"
            )),
        ],
        text=True, capture_output=True, check=False, env=environment,
    )
    assert crashed.returncode != 0
    initial = json.loads(journal.read_text())
    original_pid = initial["init_owner_process_id"]
    recovery = acquire + rf"""
$empty=Get-DawnstrikeSharedLockSha256Text ''
$null=Set-DawnstrikeRuntimeOperationJournalPhase -StateRoot '{state_q}' `
 -JournalPath '{journal_q}' -Lock $lock -Operation runtime_activation -Phase PRE_QUIESCE `
 -CandidateSha ('a'*40) -CandidateTree ('b'*40) `
 -CurrentSha ('e'*40) -CurrentTree ('f'*40) `
 -PreviousSha ('e'*40) -PreviousTree ('f'*40) `
 -OriginIdentity 'github.com/mattfren/dawnstrike' `
 -PreparedReceiptRelativePath 'receipts/runtime-activation/prepared.json' `
 -PreparedReceiptSha256 $empty `
 -CompleteReceiptRelativePath 'receipts/runtime-activation/complete.json' `
 -CompleteReceiptSha256 $empty -BackupContractSha256 ('2'*64) `
 -TaskContractSha256 ('5'*64) -RuntimeStageContractSha256 ('3'*64) `
 -PythonPath $approved.path -PythonSha256 $approved.sha256
$null=Set-DawnstrikeRuntimeOperationJournalPhase -StateRoot '{state_q}' `
 -JournalPath '{journal_q}' -Lock $lock -Operation runtime_activation -Phase PRE_SWAP `
 -CandidateSha ('a'*40) -CandidateTree ('b'*40) `
 -CurrentSha ('e'*40) -CurrentTree ('f'*40) `
 -PreviousSha ('e'*40) -PreviousTree ('f'*40) `
 -OriginIdentity 'github.com/mattfren/dawnstrike' `
 -PreparedReceiptRelativePath 'receipts/runtime-activation/prepared.json' `
 -PreparedReceiptSha256 ('1'*64) `
 -CompleteReceiptRelativePath 'receipts/runtime-activation/complete.json' `
 -CompleteReceiptSha256 $empty -BackupContractSha256 ('2'*64) `
 -TaskContractSha256 ('5'*64) -RuntimeStageContractSha256 ('3'*64) `
 -PythonPath $approved.path -PythonSha256 $approved.sha256
$phase=Get-DawnstrikeStrictRuntimeOperationJournal `
 '{journal_q}' $approved.path $approved.sha256
[pscustomobject]@{{init_pid=$phase.payload.init_owner_process_id;lock_pid=$PID}} `
 | ConvertTo-Json -Compress
Exit-DawnstrikeGovernedRuntimeLock $lock
Remove-Item '{journal_q}' -Force
"""
    recovered = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", recovery],
        text=True, capture_output=True, check=False, env=environment,
    )
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    result = json.loads(recovered.stdout.strip().splitlines()[-1])
    assert result["init_pid"] == original_pid
    assert result["lock_pid"] != original_pid
