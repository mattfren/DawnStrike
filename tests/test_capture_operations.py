from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

import intraday_scanner.services.capture_operations as capture_operations
from intraday_scanner.services.capture_operations import CapturePlan, CapturePlanError, plan_as_dict


def _operations_module():
    path = Path("scripts/capture_intraday_operations.py").resolve()
    spec = importlib.util.spec_from_file_location("capture_intraday_operations", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _daily_module():
    path = Path("scripts/run_daily_intraday_capture.py").resolve()
    spec = importlib.util.spec_from_file_location("run_daily_intraday_capture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _evidence_module():
    path = Path("scripts/capture_intraday_evidence.py").resolve()
    spec = importlib.util.spec_from_file_location("capture_intraday_evidence", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _clean_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "candidate-repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repo)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Capture Test"],
        check=True,
    )
    (repo / "candidate.txt").write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "candidate.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "candidate"],
        check=True,
        capture_output=True,
    )
    return repo


def test_capture_git_environment_binds_only_exact_repository_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(tmp_path / "hostile"))

    environment = capture_operations._governed_git_environment(repository)
    git_environment = {
        key.upper(): value for key, value in environment.items() if key.upper().startswith("GIT_")
    }

    resolved = repository.resolve(strict=True)
    git_dir = str(resolved / ".git")
    assert git_environment == {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_DIR": git_dir,
        "GIT_COMMON_DIR": git_dir,
        "GIT_WORK_TREE": str(resolved),
    }


def test_capture_identity_consumes_only_the_admitted_git_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "runtime"
    contract = MappingProxyType(
        {
            "schema_version": "dawnstrike.exact_git_contract.v1",
            "root": os.path.normcase(os.path.abspath(repository)),
            "candidate_sha": "a" * 40,
            "candidate_tree": "b" * 40,
            "origin_url": None,
            "origin_main_sha": None,
            "git_executable_sha256": "c" * 64,
            "clean": True,
            "tracked_inventory": (("100644", "d" * 40, "requirements.lock"),),
            "release_authority_blobs": MappingProxyType({"requirements.lock": b"locked"}),
            "public_web_inventory": (),
            "public_web_blobs": MappingProxyType({}),
        }
    )
    monkeypatch.setattr(sys, "_dawnstrike_exact_git_contract_v1", contract, raising=False)
    monkeypatch.setenv("DAWNSTRIKE_EXACT_GIT_ADMISSION_REQUIRED", "1")
    monkeypatch.setattr(
        capture_operations.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("post-admission Git subprocess attempted"),
    )

    assert capture_operations._git_identity(repository) == (
        "a" * 40,
        "b" * 40,
        "c" * 64,
    )


def _plan(tmp_path: Path, repo: Path) -> CapturePlan:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "membership_policy": "controls only",
                "point_in_time_membership": "research_control_only",
                "symbols": ["SPY", "IWM", "QQQ", "DIA", "TLT"],
            }
        ),
        encoding="utf-8",
    )
    session = tmp_path / "session.json"
    session.write_text(
        json.dumps(
            {
                "exchange": "XNYS",
                "market_date": "2026-08-28",
                "exchange_session_id": "XNYS:2026-08-28:regular",
                "calendar_id": "us-equities-xnys-xnas-2026-2028.v1",
                "start_utc": "2026-08-28T13:30:00Z",
                "end_utc": "2026-08-28T20:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    entitlement = tmp_path / "entitlement.json"
    entitlement.write_text(
        json.dumps(
            {
                "entitlement": "alpaca-historical-sip-older-than-15-minutes",
                "proof_id": "operator-receipt-1",
                "provider": "alpaca",
                "feed": "sip",
                "probe_status": "PASS",
                "proven_endpoints": ["bars", "trades", "quotes"],
                "retention_allowed": True,
                "approved_plan": True,
                "research_only": True,
                "broker_execution": "disabled",
                "secret": "never-copy",  # pragma: allowlist secret - fixture redaction probe
            }
        ),
        encoding="utf-8",
    )
    source = tmp_path / "web_sources.yaml"
    source.write_text("sources: []\n", encoding="utf-8")
    env = tmp_path / "runtime.env"
    env.write_text("ALPACA_DATA_FEED=sip\n", encoding="utf-8")
    root = tmp_path / "external"
    return CapturePlan(
        mode="forward_observed",
        provider="alpaca",
        feed="sip",
        candidate_sha=subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip(),
        repo_root=repo,
        db_path=root / "db" / "staging.sqlite",
        evidence_root=root / "evidence",
        run_root=root / "runs",
        output_root=root / "output",
        symbols_manifest=manifest,
        symbols_manifest_sha256=_sha(manifest),
        expected_session=session,
        expected_session_sha256=_sha(session),
        entitlement_receipt=entitlement,
        entitlement_receipt_sha256=_sha(entitlement),
        source_config=source,
        source_config_sha256=_sha(source),
        env_file=env,
        max_pages=4,
        retries=2,
    )


def test_plan_requires_delayed_sip_and_separate_external_roots(tmp_path: Path) -> None:
    repo = _clean_repo(tmp_path)
    plan = _plan(tmp_path, repo)
    result = plan_as_dict(plan, now=datetime(2026, 8, 30, 12, tzinfo=UTC))
    assert result["status"] == "READY"
    assert result["symbols"] == ["SPY", "IWM", "QQQ", "DIA", "TLT"]
    assert result["broker_execution"] == "disabled"
    assert len(result["git_executable_sha256"]) == 64
    assert result["required_endpoints"] == [
        "bars",
        "trades",
        "quotes",
        "corporate_actions",
    ]
    assert result["full_microstructure_requested"] is True
    assert result["mode_evidence_root"].endswith("forward_observed")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("feed", "iex", "exactly sip"),
        ("provider", "massive", "Alpaca"),
    ],
)
def test_plan_rejects_feed_substitution_and_non_alpaca_provider(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    repo = _clean_repo(tmp_path)
    plan = _plan(tmp_path, repo)
    object.__setattr__(plan, field, value)
    with pytest.raises(CapturePlanError, match=message):
        plan.validate(now=datetime(2026, 8, 30, 12, tzinfo=UTC))


def test_plan_rejects_active_state_output(tmp_path: Path) -> None:
    repo = _clean_repo(tmp_path)
    plan = _plan(tmp_path, repo)
    object.__setattr__(plan, "output_root", Path(r"C:\r\dawnstrike-state\capture"))
    with pytest.raises(CapturePlanError, match="active state"):
        plan.validate(now=datetime(2026, 8, 30, 12, tzinfo=UTC))


def test_plan_rejects_recent_window(tmp_path: Path) -> None:
    repo = _clean_repo(tmp_path)
    plan = _plan(tmp_path, repo)
    session = plan.expected_session
    session.write_text(
        json.dumps(
            {
                "exchange": "XNYS",
                "market_date": "2026-08-28",
                "exchange_session_id": "XNYS:2026-08-28:regular",
                "start_utc": "2026-08-28T19:00:00Z",
                "end_utc": "2026-08-28T20:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    object.__setattr__(plan, "expected_session_sha256", _sha(session))
    with pytest.raises(CapturePlanError, match="15 minutes old"):
        plan.validate(now=datetime(2026, 8, 28, 20, 5, tzinfo=UTC))


def test_plan_rejects_dirty_candidate_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    tracked = repo / "tracked.txt"
    tracked.write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "base"],
        check=True,
        capture_output=True,
    )
    plan_root = tmp_path / "plan"
    plan_root.mkdir()
    plan = _plan(plan_root, repo)
    tracked.write_text("dirty\n", encoding="utf-8")

    with pytest.raises(CapturePlanError, match="not clean"):
        plan.validate(now=datetime(2026, 8, 30, 12, tzinfo=UTC))


def test_plan_rejects_unproven_entitlement_receipt(tmp_path: Path) -> None:
    repo = _clean_repo(tmp_path)
    plan = _plan(tmp_path, repo)
    plan.entitlement_receipt.write_text(
        json.dumps({"entitlement": "claimed", "proof_id": "forged"}),
        encoding="utf-8",
    )
    object.__setattr__(plan, "entitlement_receipt_sha256", _sha(plan.entitlement_receipt))

    with pytest.raises(CapturePlanError, match="provider/feed identity"):
        plan.validate(now=datetime(2026, 8, 30, 12, tzinfo=UTC))


@pytest.mark.skipif(os.name != "nt", reason="Windows share-mode boundary")
def test_capture_admission_retains_authority_handles_through_child_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _clean_repo(tmp_path)
    plan_root = tmp_path / "plan"
    plan_root.mkdir()
    plan = _plan(plan_root, repo)
    module = _operations_module()
    blocked: dict[str, bool] = {}
    attacked_paths: list[Path] = []
    authority_paths = {
        "symbols": plan.symbols_manifest.resolve(),
        "session": plan.expected_session.resolve(),
        "entitlement": plan.entitlement_receipt.resolve(),
        "source_config": plan.source_config.resolve(),
    }

    class FakeStore:
        def __init__(self, _path: str) -> None:
            pass

        def persist_expected_market_session(self, payload: dict[str, object]) -> None:
            assert payload["exchange"] == "XNYS"
            assert payload["session_open_utc"] == "2026-08-28T13:30:00Z"

    real_read_text = Path.read_text

    def deny_authority_reopen(path: Path, *args: object, **kwargs: object) -> str:
        if path.resolve() in authority_paths.values():
            pytest.fail(f"authoritative input was reopened by pathname: {path}")
        return real_read_text(path, *args, **kwargs)

    def hostile_child(command: list[str], **_kwargs: object) -> SimpleNamespace:
        all_inputs = dict(authority_paths)
        all_inputs["derived_symbols"] = Path(command[command.index("--symbols-file") + 1])
        all_inputs["derived_entitlement"] = Path(
            command[command.index("--operator-entitlement-metadata") + 1]
        )
        for label, path in all_inputs.items():
            attacked_paths.append(path)
            replacement = path.with_name(f"hostile-{label}.json")
            replacement.write_bytes(b"{}")
            try:
                os.replace(replacement, path)
            except PermissionError:
                blocked[label] = True
            else:
                blocked[label] = False
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"status": "COMPLETE", "run_id": "test-run"}),
            stderr="",
        )

    monkeypatch.setattr(module, "IntradayEvidenceStore", FakeStore)
    monkeypatch.setattr(module, "_approved_child_python", lambda: Path(sys.executable))
    args = SimpleNamespace(execute=True)

    with plan.admit(now=datetime(2026, 8, 30, 12, tzinfo=UTC)) as admission:
        monkeypatch.setattr(module.subprocess, "run", hostile_child)
        monkeypatch.setattr(Path, "read_text", deny_authority_reopen)
        assert module._run_admitted_capture(args, plan, admission) == 0
        assert admission.expected_session["exchange"] == "XNYS"
        assert (
            admission.sanitized_entitlement_metadata(receipt_hash=plan.entitlement_receipt_sha256)[
                "entitlement"
            ]
            == "alpaca-historical-sip-older-than-15-minutes"
        )

    assert blocked == {
        "symbols": True,
        "session": True,
        "entitlement": True,
        "source_config": True,
        "derived_symbols": True,
        "derived_entitlement": True,
    }
    for path in attacked_paths:
        assert path.exists()


def test_capture_execution_uses_admitted_session_and_entitlement_without_path_reopen() -> None:
    source = Path("scripts/capture_intraday_operations.py").read_text(encoding="utf-8")
    assert "admission.expected_session" in source
    assert "admission.sanitized_entitlement_metadata" in source
    assert "plan.expected_session.read_text" not in source
    assert "plan.sanitized_entitlement_metadata" not in source


def test_evidence_child_parses_the_same_bytes_it_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _evidence_module()
    symbols_path = tmp_path / "symbols.txt"
    admitted = b"SPY\nIWM\n"
    symbols_path.write_bytes(admitted)
    expected_sha256 = hashlib.sha256(admitted).hexdigest()
    metadata_path = tmp_path / "metadata.json"
    admitted_metadata = b'{"entitlement":"sip","research_only":"true"}\n'
    metadata_path.write_bytes(admitted_metadata)
    metadata_sha256 = hashlib.sha256(admitted_metadata).hexdigest()
    real_read_bytes = Path.read_bytes

    def swap_after_read(path: Path) -> bytes:
        raw = real_read_bytes(path)
        if path == symbols_path:
            path.write_bytes(b"HOSTILE\n")
        elif path == metadata_path:
            path.write_bytes(b'{"entitlement":"hostile"}\n')
        return raw

    monkeypatch.setattr(Path, "read_bytes", swap_after_read)

    assert module._read_symbols(symbols_path, expected_sha256) == ["SPY", "IWM"]
    assert module._read_metadata(metadata_path, metadata_sha256)["entitlement"] == "sip"
    assert real_read_bytes(symbols_path) == b"HOSTILE\n"
    assert real_read_bytes(metadata_path) == b'{"entitlement":"hostile"}\n'


def test_capture_script_is_plan_only_without_execute() -> None:
    script = Path("scripts/capture_intraday_operations.py").read_text(encoding="utf-8")
    registration = Path("scripts/register_intraday_capture_task.ps1").read_text(encoding="utf-8")
    assert "--execute" in script
    assert "--include-trades" in script
    assert "--include-quotes" in script
    assert "--include-corporate-actions" in script
    assert "-Create" in registration
    assert "Register-ScheduledTask" in registration
    assert "feed substitution" in script or "feed must be exactly" in Path(
        "intraday_scanner/services/capture_operations.py"
    ).read_text(encoding="utf-8")
    assert "Python313\\python.exe" in registration
    assert "ef8f51028ac5329641985112f8efb1c2d4c47c86b8011ddf7e6fae21e2b4e5a1" in registration
    assert (
        '$pythonPrefix = @("-I", "-B", "-S", "-X", ("pycache_prefix=" + $bytecodePrefix), "-u")'
        in registration
    )
    assert "$bootstrapArgs = @(" in registration
    assert '"-c", $bootstrapPreloader, $bootstrap, $bootstrapSha256' in registration
    assert '"--release-root", $RuntimeRoot, "--expected-sha", $CandidateSha,' in registration
    assert '"--script", $runner, "--"' in registration
    assert "Get-AuthenticodeSignature" in registration
    execute_index = registration.index("$pythonVersion = @(& $Python -I -c")
    assert registration.index("Get-FileHash -LiteralPath $Python") < execute_index
    assert registration.index("Get-AuthenticodeSignature -LiteralPath $Python") < execute_index
    assert registration.index("SignerCertificate.Thumbprint") < execute_index
    assert "$captureActionArguments = @(" in registration
    assert "$pythonPrefix + $bootstrapArgs + $captureArgs" in registration
    assert "$argumentTokens = @($captureActionArguments)" in registration
    assert '-u "{0}"' not in registration
    assert "[switch]$InteractiveCurrentUser" in registration
    assert "New-ScheduledTaskPrincipal" in registration
    assert "-LogonType Interactive -RunLevel Limited" in registration
    assert "Choose either InteractiveCurrentUser or RunAsCredential" in registration
    assert "-StartWhenAvailable" in registration
    assert "-WakeToRun" in registration
    assert "-AllowStartIfOnBatteries" in registration
    assert "-DontStopIfGoingOnBatteries" in registration
    assert "-MultipleInstances IgnoreNew" in registration
    assert "-ExecutionTimeLimit (New-TimeSpan -Hours 3)" in registration
    assert "-RestartCount 3" in registration
    assert "-RestartInterval (New-TimeSpan -Minutes 15)" in registration
    assert "-At $StartAt" in registration
    assert "$StartAt.TimeOfDay" not in registration


def test_nested_capture_python_is_isolated_and_scrubs_startup_environment(monkeypatch) -> None:
    daily = _daily_module()
    operations = _operations_module()
    monkeypatch.setenv("PYTHONPATH", r"C:\hostile")
    monkeypatch.setenv("PYTHONHOME", r"C:\hostile-home")
    monkeypatch.setenv("PYTHONSTARTUP", r"C:\hostile-startup.py")
    monkeypatch.setenv("DAWNSTRIKE_EXACT_GIT_ADMISSION_REQUIRED", "1")
    for module in (daily, operations):
        env = module._isolated_child_environment()
        assert "PYTHONPATH" not in env
        assert "PYTHONHOME" not in env
        assert "PYTHONSTARTUP" not in env
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert env["DAWNSTRIKE_EXACT_GIT_ADMISSION_REQUIRED"] == "1"
    for path in (
        Path("scripts/run_daily_intraday_capture.py"),
        Path("scripts/capture_intraday_operations.py"),
    ):
        source = path.read_text(encoding="utf-8")
        assert '"-I",' in source
        assert '"-B",' in source
        assert '"-S",' in source
        assert "_BOOTSTRAP_PRELOADER" in source
        assert '"--release-root",' in source
        assert '"--expected-sha",' in source
        assert "_approved_child_python()" in source


def test_inner_capture_exit_codes_only_accept_complete() -> None:
    module = _evidence_module()
    assert module._capture_exit_code("COMPLETE") == 0
    assert module._capture_exit_code("PARTIAL") != 0
    assert module._capture_exit_code("NO_DATA") != 0
    assert module._capture_exit_code("PARTIAL") != module._capture_exit_code("NO_DATA")
    assert module._capture_exit_code("HASH_MISMATCH") != 0


def test_outer_operation_retains_terminal_incomplete_receipt() -> None:
    module = _operations_module()
    plan = {
        "mode": "forward_observed",
        "provider": "alpaca",
        "feed": "sip",
        "candidate_sha": "a" * 40,
        "candidate_tree_sha": "b" * 40,
        "candidate_worktree_clean": True,
        "plan_identity_sha256": "c" * 64,
        "market_date": "2026-08-31",
        "source_config_sha256": "d" * 64,
        "entitlement_receipt_sha256": "e" * 64,
        "required_endpoints": ["bars"],
    }
    inner = {
        "status": "PARTIAL",
        "run_id": "run-1",
        "coverage": [{"symbol": "SPY", "status": "PARTIAL"}],
    }
    result = module._safe_capture_receipt(
        subprocess.CompletedProcess(
            args=["capture"], returncode=20, stdout=json.dumps(inner), stderr=""
        ),
        plan,
    )
    assert result["status"] == "CAPTURE_INCOMPLETE"
    assert result["capture_status"] == "PARTIAL"
    assert result["run_id"] == "run-1"
    assert result["coverage"] == inner["coverage"]
    assert result["research_only"] is True
    assert result["broker_execution"] == "disabled"
    assert result["broker_execution_enabled"] is False
