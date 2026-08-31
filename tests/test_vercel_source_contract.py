"""Hostile tests for exact Git identity and immutable Vercel entrypoints."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Vercel PowerShell source-boundary contracts require Windows PowerShell.",
)

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "vercel_source_contract.ps1"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _fixture(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "Dawnstrike Test")
    (repo / "api").mkdir()
    (repo / "api" / "health.py").write_bytes(b"HEALTH-COMMITTED\n")
    (repo / "api" / "readiness.py").write_bytes(b"READINESS-COMMITTED\n")
    (repo / "scripts").mkdir()
    shutil.copy2(HELPER, repo / "scripts" / HELPER.name)
    shutil.copy2(
        ROOT / "scripts" / "dawnstrike_job_process.ps1",
        repo / "scripts" / "dawnstrike_job_process.ps1",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD"), _git(repo, "rev-parse", "HEAD^{tree}")


def _powershell(command: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    windows_modules = (
        Path(environment.get("WINDIR", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "Modules"
    )
    environment["PSModulePath"] = os.pathsep.join(
        [str(windows_modules), environment.get("PSModulePath", "")]
    )
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )


def test_dirty_api_is_rejected_before_staging(tmp_path: Path) -> None:
    repo, commit, tree = _fixture(tmp_path)
    (repo / "api" / "health.py").write_bytes(b"ATTACKER-WORKTREE\n")
    command = (
        f". '{repo / 'scripts' / HELPER.name}'; "
        f"Assert-VercelGitSourceStable -Root '{repo}' -ExpectedSourceSha '{commit}' "
        f"-ExpectedSourceTree '{tree}'"
    )
    result = _powershell(command)
    assert result.returncode != 0
    assert "not clean" in result.stderr.lower()


def test_head_and_tree_race_is_rejected_after_identity_capture(tmp_path: Path) -> None:
    repo, commit, tree = _fixture(tmp_path)
    (repo / "api" / "health.py").write_bytes(b"NEW-COMMITTED\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "race")
    command = (
        f". '{repo / 'scripts' / HELPER.name}'; "
        f"Assert-VercelGitSourceStable -Root '{repo}' -ExpectedSourceSha '{commit}' "
        f"-ExpectedSourceTree '{tree}'"
    )
    result = _powershell(command)
    assert result.returncode != 0
    assert "head changed" in result.stderr.lower()


def test_staged_api_byte_mismatch_is_rejected(tmp_path: Path) -> None:
    repo, commit, tree = _fixture(tmp_path)
    stage = repo / "build" / "vercel-stage"
    (stage / "api" / "public").mkdir(parents=True)
    (stage / "public").mkdir(parents=True)
    (stage / "api" / "health.py").write_bytes(b"HEALTH-COMMITTED\n")
    (stage / "api" / "readiness.py").write_bytes(b"READINESS-COMMITTED\n")
    health_hash = hashlib.sha256(b"HEALTH-COMMITTED\n").hexdigest()
    readiness_hash = hashlib.sha256(b"READINESS-COMMITTED\n").hexdigest()
    root_manifest = stage / "vercel-source-manifest.json"
    static_manifest = stage / "public" / "vercel-source-manifest.json"
    function_manifest = stage / "api" / "public" / "vercel-source-manifest.json"
    command = (
        f". '{repo / 'scripts' / HELPER.name}'; "
        f"$m = [ordered]@{{schema_version='dawnstrike.vercel_source_manifest.v1'; "
        f"source_sha='{commit}'; source_tree='{tree}'; api_sha256=[ordered]@{{"
        f"'api/health.py'='{health_hash}'; 'api/readiness.py'='{readiness_hash}'}}}}; "
        f"$u = New-Object Text.UTF8Encoding($false); "
        f"[IO.File]::WriteAllText('{root_manifest}', "
        f"($m | ConvertTo-Json -Depth 8), $u); "
        f"Copy-Item '{root_manifest}' '{static_manifest}'; "
        f"Copy-Item '{root_manifest}' '{function_manifest}'; "
        f"[IO.File]::WriteAllBytes('{stage / 'api' / 'health.py'}', "
        "[Text.Encoding]::UTF8.GetBytes('TAMPERED-STAGE`n')); "
        f"Assert-VercelStagedSourceManifest -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )
    result = _powershell(command)
    assert result.returncode != 0
    assert "staged api bytes" in result.stderr.lower()


def test_git_blob_extraction_is_byte_exact_under_windows_powershell(tmp_path: Path) -> None:
    repo, commit, _tree = _fixture(tmp_path)
    destination = tmp_path / "extracted-health.py"
    command = (
        f". '{repo / 'scripts' / HELPER.name}'; "
        f"Write-VercelGitBlob -Root '{repo}' -Commit '{commit}' "
        f"-RelativePath 'api/health.py' -Destination '{destination}'"
    )
    result = _powershell(command)
    assert result.returncode == 0, result.stderr
    assert destination.read_bytes() == b"HEALTH-COMMITTED\n"


def test_postbuild_function_bundle_tamper_is_rejected(tmp_path: Path) -> None:
    repo, commit, tree = _fixture(tmp_path)
    stage = repo / "build" / "vercel-stage"
    (stage / "api" / "public").mkdir(parents=True)
    (stage / "public").mkdir(parents=True)
    (stage / ".vercel" / "output" / "static").mkdir(parents=True)
    (stage / ".vercel" / "output" / "functions" / "api" / "health.func").mkdir(
        parents=True
    )
    (stage / ".vercel" / "output" / "functions" / "api" / "readiness.func").mkdir(
        parents=True
    )
    (stage / "api" / "health.py").write_bytes(b"HEALTH-COMMITTED\n")
    (stage / "api" / "readiness.py").write_bytes(b"READINESS-COMMITTED\n")
    health_hash = hashlib.sha256(b"HEALTH-COMMITTED\n").hexdigest()
    readiness_hash = hashlib.sha256(b"READINESS-COMMITTED\n").hexdigest()
    root_manifest = stage / "vercel-source-manifest.json"
    static_manifest = stage / "public" / "vercel-source-manifest.json"
    function_manifest = stage / "api" / "public" / "vercel-source-manifest.json"
    output_static_manifest = (
        stage / ".vercel" / "output" / "static" / "vercel-source-manifest.json"
    )
    output_health = (
        stage / ".vercel" / "output" / "functions" / "api" / "health.func" / "health.py"
    )
    output_readiness = (
        stage / ".vercel" / "output" / "functions" / "api" / "readiness.func" / "readiness.py"
    )
    output_config = stage / ".vercel" / "output" / "config.json"
    command = (
        f". '{repo / 'scripts' / HELPER.name}'; "
        f"$m = [ordered]@{{schema_version='dawnstrike.vercel_source_manifest.v1'; "
        f"source_sha='{commit}'; source_tree='{tree}'; api_sha256=[ordered]@{{"
        f"'api/health.py'='{health_hash}'; 'api/readiness.py'='{readiness_hash}'}}}}; "
        f"$u = New-Object Text.UTF8Encoding($false); "
        f"$r = '{root_manifest}'; "
        f"[IO.File]::WriteAllText($r, ($m | ConvertTo-Json -Depth 8), $u); "
        f"Copy-Item $r '{static_manifest}'; Copy-Item $r '{function_manifest}'; "
        f"Copy-Item $r '{output_static_manifest}'; "
        f"[IO.File]::WriteAllText('{output_config}', "
        "'{\"version\":3,\"routes\":["
        "{\"src\":\"/api/health(?:/)?\",\"dest\":\"/api/health.py\"},"
        "{\"src\":\"/api/readiness(?:/)?\",\"dest\":\"/api/readiness.py\"},"
        "{\"handle\":\"filesystem\"}]}' , $u); "
        f"[IO.File]::WriteAllText('{output_health.parent / '.vc-config.json'}', "
        "'{\"handler\":\"api/health.py\"}', $u); "
        f"[IO.File]::WriteAllText('{output_readiness.parent / '.vc-config.json'}', "
        "'{\"handler\":\"api/readiness.py\"}', $u); "
        f"Copy-Item '{stage / 'api' / 'health.py'}' '{output_health}'; "
        f"Copy-Item '{stage / 'api' / 'readiness.py'}' '{output_readiness}'; "
        f"[IO.File]::WriteAllText('{output_static_manifest}', '{'{'}\"tampered\":true{'}'}', $u); "
        "$copyTamperRejected = $false; "
        "try { Assert-VercelBuiltPackage -StageRoot '"
        f"{stage}' -ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}' "
        "} catch { $copyTamperRejected = $true; "
        "if ($_.Exception.Message -notmatch 'static package') { throw } }; "
        "if (-not $copyTamperRejected) { throw 'Static package copy tamper was accepted.' }; "
        f"Copy-Item '{root_manifest}' '{output_static_manifest}' -Force; "
        f"[IO.File]::WriteAllBytes('{output_health}', "
        "[Text.Encoding]::UTF8.GetBytes('TAMPERED-PACKAGE`n')); "
        f"Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )
    result = _powershell(command)
    assert result.returncode != 0
    assert "prebuilt function bytes" in result.stderr.lower()


def _valid_built_package(tmp_path: Path) -> tuple[Path, str, str]:
    repo, commit, tree = _fixture(tmp_path)
    stage = repo / "build" / "vercel-stage"
    for path in (
        stage / "api" / "public",
        stage / "public",
        stage / ".vercel" / "output" / "static",
        stage / ".vercel" / "output" / "functions" / "api" / "health.func",
        stage / ".vercel" / "output" / "functions" / "api" / "readiness.func",
    ):
        path.mkdir(parents=True)
    health = b"HEALTH-COMMITTED\n"
    readiness = b"READINESS-COMMITTED\n"
    (stage / "api" / "health.py").write_bytes(health)
    (stage / "api" / "readiness.py").write_bytes(readiness)
    manifest = {
        "schema_version": "dawnstrike.vercel_source_manifest.v1",
        "source_sha": commit,
        "source_tree": tree,
        "api_sha256": {
            "api/health.py": hashlib.sha256(health).hexdigest(),
            "api/readiness.py": hashlib.sha256(readiness).hexdigest(),
        },
    }
    readiness_hash = manifest["api_sha256"]["api/readiness.py"]
    manifest_path = stage / "vercel-source-manifest.json"
    manifest_path.write_text(
        "{\n"
        f'    "schema_version":  "{manifest["schema_version"]}",\n'
        f'    "source_sha":  "{commit}",\n'
        f'    "source_tree":  "{tree}",\n'
        '    "api_sha256":  {\n'
        f'                       "api/health.py":  "{manifest["api_sha256"]["api/health.py"]}",\n'
        f'                       "api/readiness.py":  "{readiness_hash}"\n'
        "                   }\n"
        "}",
        encoding="utf-8",
    )
    for destination in (
        stage / "public" / "vercel-source-manifest.json",
        stage / "api" / "public" / "vercel-source-manifest.json",
        stage / ".vercel" / "output" / "static" / "vercel-source-manifest.json",
    ):
        shutil.copy2(manifest_path, destination)
    health_output = stage / ".vercel" / "output" / "functions" / "api" / "health.func"
    readiness_output = stage / ".vercel" / "output" / "functions" / "api" / "readiness.func"
    shutil.copy2(stage / "api" / "health.py", health_output / "health.py")
    shutil.copy2(stage / "api" / "readiness.py", readiness_output / "readiness.py")
    (health_output / ".vc-config.json").write_text(
        '{"handler":"api/health.py"}', encoding="utf-8"
    )
    (readiness_output / ".vc-config.json").write_text(
        '{"handler":"api/readiness.py"}', encoding="utf-8"
    )
    (stage / ".vercel" / "output" / "config.json").write_text(
        json.dumps(
            {
                "version": 3,
                "routes": [
                    {"src": "/api/health(?:/)?", "dest": "/api/health.py"},
                    {"src": "/api/readiness(?:/)?", "dest": "/api/readiness.py"},
                    {"handle": "filesystem"},
                ],
            },
            indent=4,
        ),
        encoding="utf-8",
    )
    return stage, commit, tree


def test_postbuild_extra_function_route_is_rejected(tmp_path: Path) -> None:
    stage, commit, tree = _valid_built_package(tmp_path)
    (stage / ".vercel" / "output" / "functions" / "api" / "attacker.func").mkdir()
    command = (
        f". '{stage.parent.parent / 'scripts' / HELPER.name}'; "
        f"Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )
    result = _powershell(command)
    assert result.returncode != 0
    assert "exactly two function routes" in result.stderr.lower()


def test_postbuild_extra_route_in_output_config_is_rejected(tmp_path: Path) -> None:
    stage, commit, tree = _valid_built_package(tmp_path)
    config_path = stage / ".vercel" / "output" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["routes"].insert(0, {"src": "/api/attacker", "dest": "/api/attacker.py"})
    config_path.write_text(json.dumps(config, indent=4), encoding="utf-8")
    command = (
        f". '{stage.parent.parent / 'scripts' / HELPER.name}'; "
        f"Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )
    result = _powershell(command)
    assert result.returncode != 0
    assert "unexpected route destination" in result.stderr.lower()


def test_postbuild_duplicate_expected_route_is_rejected(tmp_path: Path) -> None:
    stage, commit, tree = _valid_built_package(tmp_path)
    config_path = stage / ".vercel" / "output" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["routes"].insert(0, config["routes"][0])
    config_path.write_text(json.dumps(config, indent=4), encoding="utf-8")
    command = (
        f". '{stage.parent.parent / 'scripts' / HELPER.name}'; "
        f"Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )
    result = _powershell(command)
    assert result.returncode != 0
    assert "exactly the expected api routes" in result.stderr.lower()


def test_postbuild_extra_output_config_property_is_rejected(tmp_path: Path) -> None:
    stage, commit, tree = _valid_built_package(tmp_path)
    config_path = stage / ".vercel" / "output" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["attacker"] = {"dest": "/api/attacker.py"}
    config_path.write_text(json.dumps(config, indent=4), encoding="utf-8")
    command = (
        f". '{stage.parent.parent / 'scripts' / HELPER.name}'; "
        f"Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )
    result = _powershell(command)
    assert result.returncode != 0
    assert "unexpected properties" in result.stderr.lower()


def test_postbuild_duplicate_output_config_key_is_rejected(tmp_path: Path) -> None:
    stage, commit, tree = _valid_built_package(tmp_path)
    config_path = stage / ".vercel" / "output" / "config.json"
    config = config_path.read_text(encoding="utf-8")
    config_path.write_text(config[:-1] + ',"routes":[]}', encoding="utf-8")
    command = (
        f". '{stage.parent.parent / 'scripts' / HELPER.name}'; "
        f"Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )
    result = _powershell(command)
    assert result.returncode != 0
    assert "duplicate" in result.stderr.lower()


def test_postbuild_handler_redirect_is_rejected(tmp_path: Path) -> None:
    stage, commit, tree = _valid_built_package(tmp_path)
    handler_path = (
        stage / ".vercel" / "output" / "functions" / "api" / "health.func" / ".vc-config.json"
    )
    handler_path.write_text('{"handler":"api/attacker.py"}', encoding="utf-8")
    command = (
        f". '{stage.parent.parent / 'scripts' / HELPER.name}'; "
        f"Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )
    result = _powershell(command)
    assert result.returncode != 0
    assert "expected handler" in result.stderr.lower()


def test_postbuild_duplicate_handler_key_is_rejected(tmp_path: Path) -> None:
    stage, commit, tree = _valid_built_package(tmp_path)
    handler_path = (
        stage / ".vercel" / "output" / "functions" / "api" / "health.func" / ".vc-config.json"
    )
    handler_path.write_text(
        '{"handler":"api/health.py","handler":"api/attacker.py"}', encoding="utf-8"
    )
    command = (
        f". '{stage.parent.parent / 'scripts' / HELPER.name}'; "
        f"Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )
    result = _powershell(command)
    assert result.returncode != 0
    assert "duplicate" in result.stderr.lower()
