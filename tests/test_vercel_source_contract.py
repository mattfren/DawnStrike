"""Hostile tests for exact Git identity and immutable Vercel entrypoints."""

# PowerShell fixture commands are intentionally kept as single command-line
# fragments; their long lines are not production code.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.generate_vercel_runtime_authority import generate as generate_runtime_authority

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Vercel PowerShell source-boundary contracts require Windows PowerShell.",
)

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "vercel_source_contract.ps1"
PRODUCTION_GIT_PATH = r"C:\Program Files\Dawnstrike\Git-2.55.0.5\cmd\git.exe"
PRODUCTION_GIT_SHA256 = "78211c7ed73988da93a6d8a33d47ec6187f464d7ea2a9a00c182bbd7a1ecf30f"
PRODUCTION_GIT_SUBJECT = (
    "CN=Johannes Schindelin, O=Johannes Schindelin, L=Bruehl, C=DE"
)
PRODUCTION_GIT_THUMBPRINT = "2A1E97CBF0DFCDA15B0DA0AC9745014F989D4AD0"
PRODUCTION_RUNTIME_AUTHORITY_SHA256 = (
    "3629e094ef8d8c7f64e4ebd0111e76af9e848fab03cd61355d215f2d387cc4a4"
)
PRODUCTION_RUNTIME_AUTHORITY_CONTRACT_SHA256 = (
    "26d187b155c3b30486457cef3a3bd3c0830a1bf882a039c76a04e3059de198e3"
)


def _helper_for_host() -> str:
    """Retarget the copied fixture only to the signed Git on this test host."""

    discovered = shutil.which("git")
    assert discovered is not None
    git_path = Path(discovered).resolve(strict=True)
    environment = os.environ.copy()
    environment["DAWNSTRIKE_TEST_GIT"] = str(git_path)
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
    signature = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference='Stop'; "
            "$s=Get-AuthenticodeSignature -LiteralPath $env:DAWNSTRIKE_TEST_GIT; "
            "[ordered]@{status=[string]$s.Status;subject=[string]$s.SignerCertificate.Subject;"
            "thumbprint=[string]$s.SignerCertificate.Thumbprint}|ConvertTo-Json -Compress",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    )
    identity = json.loads(signature.stdout)
    assert identity["status"] == "Valid"
    source = HELPER.read_text(encoding="utf-8")
    replacements = {
        PRODUCTION_GIT_PATH: str(git_path),
        PRODUCTION_GIT_SHA256: hashlib.sha256(git_path.read_bytes()).hexdigest(),
        PRODUCTION_GIT_SUBJECT: identity["subject"],
        PRODUCTION_GIT_THUMBPRINT: identity["thumbprint"],
    }
    for production, host in replacements.items():
        assert production in source
        source = source.replace(production, host)
    return source


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
    (repo / "scripts" / HELPER.name).write_text(_helper_for_host(), encoding="utf-8")
    shutil.copy2(
        ROOT / "scripts" / "dawnstrike_job_process.ps1",
        repo / "scripts" / "dawnstrike_job_process.ps1",
    )
    shutil.copy2(
        ROOT / "scripts" / "powershell_module_boundary.ps1",
        repo / "scripts" / "powershell_module_boundary.ps1",
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


def _install_fixture_runtime_authority(repo: Path, stage: Path) -> None:
    payload = generate_runtime_authority(stage)
    raw = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
    authority_path = repo / "config" / "vercel_generated_runtime_authority.v1.json"
    authority_path.parent.mkdir()
    authority_path.write_bytes(raw)

    helper_path = repo / "scripts" / HELPER.name
    helper = helper_path.read_text(encoding="utf-8")
    authority_sha256 = hashlib.sha256(raw).hexdigest()
    assert helper.count(PRODUCTION_RUNTIME_AUTHORITY_SHA256) == 1
    assert helper.count(PRODUCTION_RUNTIME_AUTHORITY_CONTRACT_SHA256) == 1
    helper = helper.replace(PRODUCTION_RUNTIME_AUTHORITY_SHA256, authority_sha256)
    helper = helper.replace(
        PRODUCTION_RUNTIME_AUTHORITY_CONTRACT_SHA256,
        payload["authority_contract_sha256"],
    )
    helper_path.write_text(helper, encoding="utf-8")


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
    (stage / "api").mkdir(parents=True)
    (stage / "function_public").mkdir(parents=True)
    (stage / "public").mkdir(parents=True)
    (stage / "api" / "health.py").write_bytes(b"HEALTH-COMMITTED\n")
    (stage / "api" / "readiness.py").write_bytes(b"READINESS-COMMITTED\n")
    health_hash = hashlib.sha256(b"HEALTH-COMMITTED\n").hexdigest()
    readiness_hash = hashlib.sha256(b"READINESS-COMMITTED\n").hexdigest()
    root_manifest = stage / "vercel-source-manifest.json"
    static_manifest = stage / "public" / "vercel-source-manifest.json"
    function_manifest = stage / "function_public" / "vercel-source-manifest.json"
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


def test_generated_pip_record_launcher_rows_are_canonicalized(tmp_path: Path) -> None:
    repo, _commit, _tree = _fixture(tmp_path)
    stage = repo / "build" / "vercel-stage"
    record = (
        stage
        / ".vercel"
        / "python"
        / ".venv"
        / "Lib"
        / "site-packages"
        / "pip-26.2.1.dist-info"
        / "RECORD"
    )
    record.parent.mkdir(parents=True)
    launcher_hash = "A" * 43
    record.write_text(
        "pip/__init__.py,sha256=fixture,1\n"
        f"../../Scripts/pip.exe,sha256={launcher_hash},46080\n"
        f"../../Scripts/pip3.13.exe,sha256={launcher_hash},46080\n"
        f"../../Scripts/pip3.exe,sha256={launcher_hash},46080\n",
        encoding="utf-8",
        newline="\n",
    )
    command = (
        f". '{repo / 'scripts' / HELPER.name}'; "
        f"Normalize-VercelGeneratedPipRecord -StageRoot '{stage}'; "
        f"Normalize-VercelGeneratedPipRecord -StageRoot '{stage}'"
    )
    result = _powershell(command)
    assert result.returncode == 0, result.stderr
    assert record.read_text(encoding="utf-8") == (
        "pip/__init__.py,sha256=fixture,1\n"
        "../../Scripts/pip.exe,,\n"
        "../../Scripts/pip3.13.exe,,\n"
        "../../Scripts/pip3.exe,,\n"
    )


def test_postbuild_function_bundle_tamper_is_rejected(tmp_path: Path) -> None:
    stage, commit, tree = _valid_built_package(tmp_path)
    helper = stage.parent.parent / "scripts" / HELPER.name
    command = (
        f". '{helper}'; Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )

    output_static_manifest = (
        stage / ".vercel" / "output" / "static" / "vercel-source-manifest.json"
    )
    output_static_manifest.write_text('{"tampered":true}', encoding="utf-8")
    static_result = _powershell(command)
    assert static_result.returncode != 0
    assert "static package" in static_result.stderr.lower()

    shutil.copy2(stage / "vercel-source-manifest.json", output_static_manifest)
    (stage / "api" / "health.py").write_bytes(b"TAMPERED-PACKAGE\n")
    function_result = _powershell(command)
    assert function_result.returncode != 0
    assert "immutable vercel source manifest" in function_result.stderr.lower()


def _valid_built_package(tmp_path: Path) -> tuple[Path, str, str]:
    repo, commit, tree = _fixture(tmp_path)
    stage = repo / "build" / "vercel-stage"
    for path in (
        stage / "api",
        stage / "function_public",
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
    manifest_path = stage / "vercel-source-manifest.json"
    manifest_path.write_bytes(json.dumps(manifest, indent=2).replace("\n", "\r\n").encode("utf-8"))
    for destination in (
        stage / "public" / "vercel-source-manifest.json",
        stage / "function_public" / "vercel-source-manifest.json",
        stage / ".vercel" / "output" / "static" / "vercel-source-manifest.json",
    ):
        shutil.copy2(manifest_path, destination)
    health_output = stage / ".vercel" / "output" / "functions" / "api" / "health.func"
    readiness_output = stage / ".vercel" / "output" / "functions" / "api" / "readiness.func"
    # @vercel/python emits a generated wrapper plus .vc-config.json; source
    # and packaged-public bytes are bound through filePathMap rather than
    # copied into each .func directory.
    (stage / "function_public" / "probe.txt").write_text("probe\n", encoding="utf-8")
    function_map = {
        ".python-version": ".python-version",
        "api/health.py": "api/health.py",
        "api/public_state.py": "api/public_state.py",
        "api/readiness.py": "api/readiness.py",
        "function_public/probe.txt": "function_public/probe.txt",
        "function_public/vercel-source-manifest.json": "function_public/vercel-source-manifest.json",
        "pyproject.toml": "pyproject.toml",
        "uv.lock": "uv.lock",
        "vercel.json": "vercel.json",
    }
    vendor_target = (
        stage
        / ".vercel"
        / "python"
        / ".venv"
        / "Lib"
        / "site-packages"
        / "vercel_runtime"
        / "__init__.py"
    )
    vendor_target.parent.mkdir(parents=True, exist_ok=True)
    vendor_target.write_bytes(b"PINNED-FIXTURE-VENDOR\n")
    function_map["_vendor/vercel_runtime/__init__.py"] = (
        ".vercel/python/.venv/Lib/site-packages/vercel_runtime/__init__.py"
    )
    (stage / ".python-version").write_text("3.13\n", encoding="utf-8")
    (stage / "api" / "public_state.py").write_text(
        "PUBLIC_STATE = {'static_file_hashes_verified': True}\n", encoding="utf-8"
    )
    (stage / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (stage / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (stage / "vercel.json").write_text("{}\n", encoding="utf-8")
    vc_config = {
        "handler": "vc__handler__python.vc_handler",
        "runtime": "python3.13",
        "architecture": "x86_64",
        "maxDuration": 10,
        "environment": {"PYTHONPATH": "_vendor", "PYTHONDONTWRITEBYTECODE": "1"},
        "supportsResponseStreaming": True,
        "filePathMap": function_map,
    }
    (health_output / ".vc-config.json").write_text(
        json.dumps(vc_config), encoding="utf-8"
    )
    (readiness_output / ".vc-config.json").write_text(
        json.dumps(vc_config), encoding="utf-8"
    )
    (health_output / "vc__handler__python.py").write_text(
        '"__VC_HANDLER_ENTRYPOINT": "api/health.py"; '
        '"__VC_HANDLER_MODULE_NAME": "api.health"',
        encoding="utf-8",
    )
    (readiness_output / "vc__handler__python.py").write_text(
        '"__VC_HANDLER_ENTRYPOINT": "api/readiness.py"; '
        '"__VC_HANDLER_MODULE_NAME": "api.readiness"',
        encoding="utf-8",
    )
    (stage / ".vercel" / "output" / "config.json").write_text(
        json.dumps(
            {
                "version": 3,
                "routes": [
                    {
                        "src": r"^(?:/(.*))$",
                        "headers": {
                            "Content-Security-Policy": "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; manifest-src 'self'; upgrade-insecure-requests",
                            "X-Content-Type-Options": "nosniff",
                            "Referrer-Policy": "no-referrer",
                            "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
                            "X-Frame-Options": "DENY",
                            "Cross-Origin-Opener-Policy": "same-origin",
                            "Cross-Origin-Resource-Policy": "same-origin",
                        },
                        "continue": True,
                    },
                    {"handle": "filesystem"},
                    {"src": "^/api(/.*)?$", "status": 404},
                    {"handle": "error"},
                    {"status": 404, "src": "^(?!/api).*$", "dest": "/404.html"},
                    {"handle": "miss"},
                    {
                        "src": r"^/api/(.+)(?:\.(?:py))$",
                        "dest": "/api/$1",
                        "check": True,
                    },
                ],
                "crons": [],
            },
            indent=4,
        ),
        encoding="utf-8",
    )
    _install_fixture_runtime_authority(repo, stage)
    return stage, commit, tree


def test_postbuild_python_suffix_function_layout_is_not_authorized(tmp_path: Path) -> None:
    stage, commit, tree = _valid_built_package(tmp_path)
    functions = stage / ".vercel" / "output" / "functions" / "api"
    (functions / "health.func").rename(functions / "health.py.func")
    (functions / "readiness.func").rename(functions / "readiness.py.func")
    command = (
        f". '{stage.parent.parent / 'scripts' / HELPER.name}'; "
        f"Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )
    result = _powershell(command)
    assert result.returncode != 0
    assert "generated-runtime authority" in result.stderr.lower()


def test_postbuild_duplicate_health_function_route_is_rejected(tmp_path: Path) -> None:
    stage, commit, tree = _valid_built_package(tmp_path)
    functions = stage / ".vercel" / "output" / "functions" / "api"
    (functions / "health.func").rename(functions / "health.py.func")
    shutil.copytree(functions / "health.py.func", functions / "health.func")
    command = (
        f". '{stage.parent.parent / 'scripts' / HELPER.name}'; "
        f"Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )
    result = _powershell(command)
    assert result.returncode != 0
    assert "exactly two function routes" in result.stderr.lower()


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
    assert "unexpected route or route semantics" in result.stderr.lower()


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
    assert "exactly the expected transformed routes" in result.stderr.lower()


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
    assert "unexpected schema" in result.stderr.lower()


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


def test_postbuild_handler_traversal_is_rejected(tmp_path: Path) -> None:
    stage, commit, tree = _valid_built_package(tmp_path)
    handler_path = (
        stage / ".vercel" / "output" / "functions" / "api" / "health.func" / ".vc-config.json"
    )
    handler_path.write_text(
        '{"handler":"../../api/attacker.py"}', encoding="utf-8"
    )
    command = (
        f". '{stage.parent.parent / 'scripts' / HELPER.name}'; "
        f"Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )
    result = _powershell(command)
    assert result.returncode != 0
    assert "unexpected schema" in result.stderr.lower()


def test_postbuild_missing_python_wrapper_is_rejected(tmp_path: Path) -> None:
    stage, commit, tree = _valid_built_package(tmp_path)
    health_wrapper = (
        stage
        / ".vercel"
        / "output"
        / "functions"
        / "api"
        / "health.func"
        / "vc__handler__python.py"
    )
    health_wrapper.unlink()
    command = (
        f". '{stage.parent.parent / 'scripts' / HELPER.name}'; "
        f"Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )
    result = _powershell(command)
    assert result.returncode != 0
    assert "exactly one vercel python wrapper" in result.stderr.lower()


def test_postbuild_wrapper_other_function_entrypoint_is_rejected(tmp_path: Path) -> None:
    stage, commit, tree = _valid_built_package(tmp_path)
    health_wrapper = (
        stage
        / ".vercel"
        / "output"
        / "functions"
        / "api"
        / "health.func"
        / "vc__handler__python.py"
    )
    health_wrapper.write_text(
        '"__VC_HANDLER_ENTRYPOINT": "api/readiness.py"; '
        '"__VC_HANDLER_MODULE_NAME": "api.readiness"',
        encoding="utf-8",
    )
    command = (
        f". '{stage.parent.parent / 'scripts' / HELPER.name}'; "
        f"Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )
    result = _powershell(command)
    assert result.returncode != 0
    assert "expected source entrypoint" in result.stderr.lower()


def test_generated_wrapper_injection_is_rejected_before_first_manifest(
    tmp_path: Path,
) -> None:
    stage, commit, tree = _valid_built_package(tmp_path)
    wrapper = (
        stage
        / ".vercel"
        / "output"
        / "functions"
        / "api"
        / "health.func"
        / "vc__handler__python.py"
    )
    wrapper.write_text(
        wrapper.read_text(encoding="utf-8")
        + '\n__import__("os").system("evil.exe")\n',
        encoding="utf-8",
    )
    package_manifest = stage / "vercel-package-manifest.json"
    assert not package_manifest.exists()
    result = _powershell(
        f". '{stage.parent.parent / 'scripts' / HELPER.name}'; "
        f"Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )
    assert result.returncode != 0
    assert "generated-runtime authority bytes changed" in result.stderr.lower()
    assert not package_manifest.exists()


def test_generated_vendor_tamper_is_rejected_before_first_manifest(
    tmp_path: Path,
) -> None:
    stage, commit, tree = _valid_built_package(tmp_path)
    vendor = (
        stage
        / ".vercel"
        / "python"
        / ".venv"
        / "Lib"
        / "site-packages"
        / "vercel_runtime"
        / "__init__.py"
    )
    vendor.write_bytes(b'__import__("os").system("evil.exe")\n')
    package_manifest = stage / "vercel-package-manifest.json"
    assert not package_manifest.exists()
    result = _powershell(
        f". '{stage.parent.parent / 'scripts' / HELPER.name}'; "
        f"Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )
    assert result.returncode != 0
    assert "generated-runtime authority bytes changed" in result.stderr.lower()
    assert not package_manifest.exists()


def test_postbuild_inert_function_file_is_rejected(tmp_path: Path) -> None:
    stage, commit, tree = _valid_built_package(tmp_path)
    inert = (
        stage
        / ".vercel"
        / "output"
        / "functions"
        / "api"
        / "health.func"
        / "inert.txt"
    )
    inert.write_text("unexpected", encoding="utf-8")
    command = (
        f". '{stage.parent.parent / 'scripts' / HELPER.name}'; "
        f"Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )
    result = _powershell(command)
    assert result.returncode != 0
    assert "unexpected file" in result.stderr.lower()


def test_postbuild_unexpected_vendor_root_is_rejected(tmp_path: Path) -> None:
    stage, commit, tree = _valid_built_package(tmp_path)
    vendor_target = stage / ".vercel" / "python" / ".venv" / "Lib" / "site-packages" / "sitecustomize.py"
    vendor_target.parent.mkdir(parents=True, exist_ok=True)
    vendor_target.write_text("import os\n", encoding="utf-8")
    for route in ("health.func", "readiness.func"):
        config_path = stage / ".vercel" / "output" / "functions" / "api" / route / ".vc-config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["filePathMap"]["_vendor/sitecustomize.py"] = ".vercel/python/.venv/Lib/site-packages/sitecustomize.py"
        config_path.write_text(json.dumps(config), encoding="utf-8")
    command = (
        f". '{stage.parent.parent / 'scripts' / HELPER.name}'; "
        f"Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )
    result = _powershell(command)
    assert result.returncode != 0
    assert "unexpected generated vendor root" in result.stderr.lower()


def test_postbuild_prerender_config_injection_is_rejected(tmp_path: Path) -> None:
    stage, commit, tree = _valid_built_package(tmp_path)
    injected = stage / ".vercel" / "output" / ".prerender-config.json"
    injected.write_text('{"routes":[]}', encoding="utf-8")
    command = (
        f". '{stage.parent.parent / 'scripts' / HELPER.name}'; "
        f"Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )
    result = _powershell(command)
    assert result.returncode != 0
    assert "unexpected entries" in result.stderr.lower()


def test_postbuild_package_inventory_rejects_post_seal_change(tmp_path: Path) -> None:
    stage, commit, tree = _valid_built_package(tmp_path)
    command_prefix = (
        f". '{stage.parent.parent / 'scripts' / HELPER.name}'; "
        f"Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )
    first = _powershell(command_prefix)
    assert first.returncode == 0, first.stderr
    wrapper = (
        stage
        / ".vercel"
        / "output"
        / "functions"
        / "api"
        / "health.func"
        / "vc__handler__python.py"
    )
    wrapper.write_text(wrapper.read_text(encoding="utf-8") + "tampered", encoding="utf-8")
    second = _powershell(command_prefix)
    assert second.returncode != 0
    assert "generated-runtime authority" in second.stderr.lower()


def test_postbuild_package_manifest_hash_rejects_rewritten_inventory(tmp_path: Path) -> None:
    stage, commit, tree = _valid_built_package(tmp_path)
    wrapper = (
        stage
        / ".vercel"
        / "output"
        / "functions"
        / "api"
        / "health.func"
        / "vc__handler__python.py"
    )
    manifest = stage / "vercel-package-manifest.json"
    command = (
        f". '{stage.parent.parent / 'scripts' / HELPER.name}'; "
        f"$sealed = Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'; "
        f"[IO.File]::AppendAllText('{wrapper}', [Environment]::NewLine + '# semantically inert generated wrapper byte'); "
        f"$inventory = Get-VercelPackageInventory -StageRoot '{stage}'; "
        f"$package = [ordered]@{{schema_version='dawnstrike.vercel_package_manifest.v1'; "
        f"directories=@(Get-VercelPackageDirectories -StageRoot '{stage}'); files=$inventory}}; "
        f"$u = New-Object Text.UTF8Encoding($false); "
        f"[IO.File]::WriteAllText('{manifest}', ($package | ConvertTo-Json -Depth 20), $u); "
        f"Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}' "
        f"-ExpectedPackageManifestSha256 $sealed"
    )
    result = _powershell(command)
    assert result.returncode != 0
    assert "generated-runtime authority" in result.stderr.lower()


def _open_boundary_command(stage: Path, manifest_sha256: str, body: str) -> str:
    helper = stage.parent.parent / "scripts" / HELPER.name
    return (
        f". '{helper}'; "
        "function Open-DawnstrikeStateBoundaryPath { param([string]$Path,[string]$Label) "
        "[pscustomobject]@{handle=[IO.MemoryStream]::new()} }; "
        f"$boundary=Open-VercelDeploymentPackageBoundary -StageRoot '{stage}' "
        f"-ExpectedPackageManifestSha256 '{manifest_sha256}'; "
        f"try {{ {body} }} finally {{ Close-VercelDeploymentPackageBoundary -Boundary $boundary }}"
    )


def test_frozen_deployment_union_includes_filepathmap_targets_by_target_path(
    tmp_path: Path,
) -> None:
    stage, commit, tree = _valid_built_package(tmp_path)
    seal = _powershell(
        f". '{stage.parent.parent / 'scripts' / HELPER.name}'; "
        f"Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )
    assert seal.returncode == 0, seal.stderr
    manifest_sha256 = seal.stdout.strip()
    command = _open_boundary_command(
        stage,
        manifest_sha256,
        "$summary=@($boundary.files | ForEach-Object { [ordered]@{"
        "path=[string]$_.relative_path; target=[string]$_.target_relative_path; "
        "origin=[string]$_.origin; sha256=[string]$_.sha256; size=[long]$_.length} }); "
        "ConvertTo-Json -Compress -Depth 8 -InputObject $summary",
    )
    result = _powershell(command)
    assert result.returncode == 0, result.stderr
    records = json.loads(result.stdout)
    by_path = {record["path"]: record for record in records}

    expected_external_targets = {
        ".python-version",
        "api/health.py",
        "api/readiness.py",
        "function_public/probe.txt",
        "function_public/vercel-source-manifest.json",
        "pyproject.toml",
        "uv.lock",
        "vercel.json",
        ".vercel/python/.venv/Lib/site-packages/vercel_runtime/__init__.py",
    }
    assert expected_external_targets <= by_path.keys()
    assert "_vendor/vercel_runtime/__init__.py" not in by_path
    vendor_path = ".vercel/python/.venv/Lib/site-packages/vercel_runtime/__init__.py"
    assert by_path[vendor_path]["origin"] == "FILE_PATH_MAP"
    assert all(record["path"] == record["target"] for record in records)
    assert any(path.startswith(".vercel/output/") for path in by_path)


def test_frozen_deployment_union_rejects_filepathmap_target_changed_after_seal(
    tmp_path: Path,
) -> None:
    stage, commit, tree = _valid_built_package(tmp_path)
    seal = _powershell(
        f". '{stage.parent.parent / 'scripts' / HELPER.name}'; "
        f"Assert-VercelBuiltPackage -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )
    assert seal.returncode == 0, seal.stderr
    manifest_sha256 = seal.stdout.strip()
    (stage / "api" / "health.py").write_bytes(b"POST-SEAL-SUBSTITUTION\n")

    result = _powershell(
        _open_boundary_command(stage, manifest_sha256, "$null=$boundary.file_count")
    )
    assert result.returncode != 0
    assert "retained filepathmap target bytes differ" in result.stderr.lower()
