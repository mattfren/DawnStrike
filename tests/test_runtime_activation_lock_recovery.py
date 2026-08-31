from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
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
$python=(Get-Command py.exe -CommandType Application).Source
$pythonSha=Get-DawnstrikeRuntimeLockHash $python
$lock=Enter-DawnstrikeGovernedRuntimeLock -StateRoot '{state_q}' -Operation runtime_activation `
  -CandidateSha '{"a" * 40}' -CandidateTree '{"b" * 40}' `
  -OriginIdentity 'github.com/mattfren/dawnstrike' -PythonPath $python -PythonSha256 $pythonSha
if(-not (Test-Path -LiteralPath $lock.path)){{throw 'lock absent'}}
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


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell unavailable")
def test_dead_child_lock_is_atomically_adopted_and_released(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    module = str(PS).replace("'", "''")
    state_q = str(state).replace("'", "''")
    common = rf"""
. '{module}'
$python=(Get-Command py.exe -CommandType Application).Source
$pythonSha=Get-DawnstrikeRuntimeLockHash $python
"""
    child = (
        common
        + rf"""
$null=Enter-DawnstrikeGovernedRuntimeLock -StateRoot '{state_q}' -Operation runtime_activation `
 -CandidateSha '{"a" * 40}' -CandidateTree '{"b" * 40}' `
 -OriginIdentity 'github.com/mattfren/dawnstrike' `
 -PythonPath $python -PythonSha256 $pythonSha
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
