from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("powershell.exe") is None,
    reason="capture-task safety executes the governed Windows PowerShell contract",
)

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "capture_task_safety.ps1"
APPROVED_PYTHON = Path(
    r"C:\Users\MattFields\AppData\Local\Programs\Python\Python313\python.exe"
)
APPROVED_PYTHON_SHA256 = (
    "ef8f51028ac5329641985112f8efb1c2d4c47c86b8011ddf7e6fae21e2b4e5a1"  # pragma: allowlist secret
)
APPROVED_PYTHON_SIGNER_THUMBPRINT = (
    "9BA3C2E210C7E8296C5056515BFC0B0BBA78AC48"  # pragma: allowlist secret
)
BOOTSTRAP_PRELOADER = (
    "import hashlib,sys; p=sys.argv[1]; e=sys.argv[2]; b=open(p,'rb').read(); "
    "a=hashlib.sha256(b).hexdigest(); a==e or (_ for _ in ()).throw("
    "RuntimeError('bootstrap hash mismatch')); r=sys.argv[3:]; sys.argv=[p,*r]; "
    "exec(compile(b,p,'exec'),{'__name__':'__main__','__file__':p})"
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    external = tmp_path / "external"
    (runtime / "scripts").mkdir(parents=True)
    (runtime / "scripts" / "run_daily_intraday_capture.py").write_text("# safe\n")
    (state / "secrets").mkdir(parents=True)
    (state / "secrets" / "runtime.env").write_text("TEST_ONLY=1\n")
    files = {}
    for name in ("symbols.json", "entitlement.json", "sources.yaml"):
        path = external / "config" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
        files[name] = path
    options = (
        ("--candidate-sha", "a" * 40),
        ("--repo-root", str(runtime)),
        ("--db-path", str(external / "db" / "capture.sqlite")),
        ("--evidence-root", str(external / "evidence")),
        ("--run-root", str(external / "runs")),
        ("--output-root", str(external / "output")),
        ("--session-root", str(external / "sessions")),
        ("--symbols-manifest", str(files["symbols.json"])),
        ("--symbols-manifest-sha256", _sha(files["symbols.json"])),
        ("--entitlement-receipt", str(files["entitlement.json"])),
        ("--entitlement-receipt-sha256", _sha(files["entitlement.json"])),
        ("--source-config", str(files["sources.yaml"])),
        ("--source-config-sha256", _sha(files["sources.yaml"])),
        ("--env-file", str(state / "secrets" / "runtime.env")),
        ("--max-pages", "100"),
        ("--retries", "3"),
    )
    tokens = ["-3.13", "-u", str(runtime / "scripts" / "run_daily_intraday_capture.py")]
    for key, value in options:
        tokens.extend((key, value))
    tokens.append("--execute")
    arguments = " ".join(f'&quot;{token}&quot;' for token in tokens)
    xml = tmp_path / "task.xml"
    xml.write_text(
        f'''<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task" version="1.3">
  <RegistrationInfo>
    <Description>Dawnstrike delayed SIP research capture; no broker execution.</Description>
    <URI>\\Dawnstrike Delayed SIP Capture</URI>
  </RegistrationInfo>
  <Principals><Principal id="Author">
    <UserId>S-1-5-18</UserId><LogonType>Password</LogonType>
    <RunLevel>LeastPrivilege</RunLevel>
  </Principal></Principals>
  <Settings><Enabled>false</Enabled></Settings>
  <Triggers><CalendarTrigger><StartBoundary>2026-08-31T15:20:00-05:00</StartBoundary>
    <ScheduleByWeek><WeeksInterval>1</WeeksInterval><DaysOfWeek>
      <Monday/><Tuesday/><Wednesday/><Thursday/><Friday/>
    </DaysOfWeek></ScheduleByWeek>
  </CalendarTrigger></Triggers>
  <Actions Context="Author"><Exec><Command>py.exe</Command>
    <Arguments>{arguments}</Arguments><WorkingDirectory>{runtime}</WorkingDirectory>
  </Exec></Actions>
</Task>''',
        encoding="utf-8",
    )
    return xml, {
        "runtime": str(runtime), "state": str(state), "symbols": str(files["symbols.json"]),
        "symbols_hash": _sha(files["symbols.json"]), "entitlement": str(files["entitlement.json"]),
        "entitlement_hash": _sha(files["entitlement.json"]), "source": str(files["sources.yaml"]),
        "source_hash": _sha(files["sources.yaml"]),
        "external": str(external),
    }


def _canonical_fixture(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    xml, values = _fixture(tmp_path)
    runtime = Path(values["runtime"])
    state = Path(values["state"])
    bootstrap = runtime / "scripts" / "dawnstrike_python_bootstrap.py"
    bootstrap.write_text("# safe bootstrap\n", encoding="utf-8")
    (state / "capture-bytecode" / ("a" * 40)).mkdir(parents=True)
    options = (
        ("--candidate-sha", "a" * 40),
        ("--repo-root", str(runtime)),
        ("--db-path", str(Path(values["external"]) / "db" / "capture.sqlite")),
        ("--evidence-root", str(Path(values["external"]) / "evidence")),
        ("--run-root", str(Path(values["external"]) / "runs")),
        ("--output-root", str(Path(values["external"]) / "output")),
        ("--session-root", str(Path(values["external"]) / "sessions")),
        ("--symbols-manifest", values["symbols"]),
        ("--symbols-manifest-sha256", values["symbols_hash"]),
        ("--entitlement-receipt", values["entitlement"]),
        ("--entitlement-receipt-sha256", values["entitlement_hash"]),
        ("--source-config", values["source"]),
        ("--source-config-sha256", values["source_hash"]),
        ("--env-file", str(state / "secrets" / "runtime.env")),
        ("--max-pages", "100"),
        ("--retries", "3"),
    )
    tokens = [
        "-I",
        "-B",
        "-S",
        "-X",
        f"pycache_prefix={state / 'capture-bytecode' / ('a' * 40)}",
        "-u",
        "-c",
        BOOTSTRAP_PRELOADER,
        str(bootstrap),
        _sha(bootstrap),
        "--release-root",
        str(runtime),
        "--expected-sha",
        "a" * 40,
        "--script",
        str(runtime / "scripts" / "run_daily_intraday_capture.py"),
        "--",
    ]
    for key, value in options:
        tokens.extend((key, value))
    tokens.append("--execute")
    old_arguments = " ".join(f"&quot;{token}&quot;" for token in _legacy_tokens(values))
    new_arguments = " ".join(f"&quot;{token}&quot;" for token in tokens)
    xml_text = xml.read_text(encoding="utf-8")
    assert old_arguments in xml_text
    xml.write_text(
        xml_text.replace("<Command>py.exe</Command>", f"<Command>{APPROVED_PYTHON}</Command>")
        .replace(
            "<Settings><Enabled>false</Enabled></Settings>",
            "<Settings><Enabled>false</Enabled><StartWhenAvailable>true</StartWhenAvailable>"
            "<WakeToRun>true</WakeToRun><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
            "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT3H</ExecutionTimeLimit>"
            "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><RestartOnFailure><Interval>PT15M</Interval>"
            "<Count>3</Count></RestartOnFailure><UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine></Settings>",
        )
        .replace(old_arguments, new_arguments),
        encoding="utf-8",
    )
    values["canonical"] = "1"
    values["bootstrap"] = str(bootstrap)
    return xml, values


def _legacy_tokens(values: dict[str, str]) -> list[str]:
    state = Path(values["state"])
    runtime = Path(values["runtime"])
    external = Path(values["external"])
    options = (
        ("--candidate-sha", "a" * 40),
        ("--repo-root", str(runtime)),
        ("--db-path", str(external / "db" / "capture.sqlite")),
        ("--evidence-root", str(external / "evidence")),
        ("--run-root", str(external / "runs")),
        ("--output-root", str(external / "output")),
        ("--session-root", str(external / "sessions")),
        ("--symbols-manifest", values["symbols"]),
        ("--symbols-manifest-sha256", values["symbols_hash"]),
        ("--entitlement-receipt", values["entitlement"]),
        ("--entitlement-receipt-sha256", values["entitlement_hash"]),
        ("--source-config", values["source"]),
        ("--source-config-sha256", values["source_hash"]),
        ("--env-file", str(state / "secrets" / "runtime.env")),
        ("--max-pages", "100"),
        ("--retries", "3"),
    )
    tokens = ["-3.13", "-u", str(runtime / "scripts" / "run_daily_intraday_capture.py")]
    for key, value in options:
        tokens.extend((key, value))
    tokens.append("--execute")
    return tokens


def _run(
    xml: Path,
    values: dict[str, str],
    *,
    canonical: bool = False,
    extra_args: str = "",
) -> subprocess.CompletedProcess[str]:
    interpreter_args = (
        f'-ExpectedInterpreterPath "{APPROVED_PYTHON}" '
        f'-ExpectedInterpreterSha256 "{APPROVED_PYTHON_SHA256}" '
        f'-ExpectedInterpreterSignerThumbprint "{APPROVED_PYTHON_SIGNER_THUMBPRINT}"'
        if canonical
        else "-AllowLegacyLauncher"
    )
    command = (
        f'. "{HELPER}"; $x=[IO.File]::ReadAllText("{xml}"); '
        f'Assert-DawnstrikeCaptureTaskSafety -Xml $x -RuntimeRoot "{values["runtime"]}" '
        f'-StateRoot "{values["state"]}" -ExpectedPrincipal "S-1-5-18" '
        f'-ExpectedCandidateSha "{"a" * 40}" -ExpectedSymbolsManifest "{values["symbols"]}" '
        f'-ExpectedSymbolsManifestSha256 "{values["symbols_hash"]}" '
        f'-ExpectedEntitlementReceipt "{values["entitlement"]}" '
        f'-ExpectedEntitlementReceiptSha256 "{values["entitlement_hash"]}" '
        f'-ExpectedSourceConfig "{values["source"]}" '
        f'-ExpectedSourceConfigSha256 "{values["source_hash"]}" '
        f'-ExpectedDbPath "{values["external"]}\\db\\capture.sqlite" '
        f'-ExpectedEvidenceRoot "{values["external"]}\\evidence" '
        f'-ExpectedRunRoot "{values["external"]}\\runs" '
        f'-ExpectedOutputRoot "{values["external"]}\\output" '
        f'-ExpectedSessionRoot "{values["external"]}\\sessions" '
        f'-ExpectedConfigRoot "{values["external"]}\\config" '
        f'-RequirePasswordPrincipal -RequireRunner {interpreter_args} {extra_args} '
        '| ConvertTo-Json -Compress'
    )
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        text=True, capture_output=True, check=False,
    )


def test_exact_capture_action_and_principal_pass(tmp_path: Path) -> None:
    xml, values = _fixture(tmp_path)
    result = _run(xml, values)
    assert result.returncode == 0, result.stderr
    assert '"forward_observed":true' in result.stdout


def test_exact_bootstrap_capture_action_and_isolation_pass(tmp_path: Path) -> None:
    xml, values = _canonical_fixture(tmp_path)
    result = _run(xml, values, canonical=True)
    assert result.returncode == 0, result.stderr
    assert '"bootstrap_path"' in result.stdout
    assert '"python_prefix":"-I -B -S -X pycache_prefix=' in result.stdout


def test_pre_swap_bootstrap_override_requires_disabled_task(tmp_path: Path) -> None:
    xml, values = _canonical_fixture(tmp_path)
    Path(values["bootstrap"]).unlink()
    result = _run(
        xml,
        values,
        canonical=True,
        extra_args='-AllowMissingBootstrap -ExpectedEnabled "true"',
    )
    assert result.returncode != 0
    assert "only for a Disabled task pre-swap" in result.stderr


def test_legacy_direct_override_requires_disabled_task(tmp_path: Path) -> None:
    xml, values = _fixture(tmp_path)
    result = _run(
        xml,
        values,
        extra_args='-AllowLegacyDirectAction -ExpectedEnabled "true"',
    )
    assert result.returncode != 0
    assert "only for a Disabled task migration" in result.stderr


@pytest.mark.parametrize("mutation", [
    "missing_s",
    "wrong_preloader",
    "missing_preloader",
    "wrong_bootstrap",
    "wrong_bootstrap_hash",
    "missing_bootstrap_hash",
    "tampered_bootstrap",
    "wrong_expected_sha",
    "missing_expected_sha",
    "wrong_release_root",
    "wrong_runner",
    "missing_separator",
])
def test_bootstrap_action_hostile_layout_fails_closed(tmp_path: Path, mutation: str) -> None:
    xml, values = _canonical_fixture(tmp_path)
    text = xml.read_text(encoding="utf-8")
    runtime = Path(values["runtime"])
    if mutation == "missing_s":
        text = text.replace('&quot;-S&quot; ', "", 1)
    elif mutation == "wrong_preloader":
        text = text.replace(
            f'&quot;{BOOTSTRAP_PRELOADER}&quot;',
            '&quot;import sys&quot;',
            1,
        )
    elif mutation == "missing_preloader":
        text = text.replace(
            f'&quot;-c&quot; &quot;{BOOTSTRAP_PRELOADER}&quot; ',
            "",
            1,
        )
    elif mutation == "wrong_bootstrap":
        text = text.replace(
            f'&quot;{values["bootstrap"]}&quot;',
            f'&quot;{runtime / "scripts" / "wrong_bootstrap.py"}&quot;',
            1,
        )
    elif mutation == "wrong_bootstrap_hash":
        text = text.replace(
            f'&quot;{_sha(Path(values["bootstrap"]))}&quot;',
            f'&quot;{"b" * 64}&quot;',
            1,
        )
    elif mutation == "missing_bootstrap_hash":
        text = text.replace(
            f'&quot;{_sha(Path(values["bootstrap"]))}&quot; ',
            "",
            1,
        )
    elif mutation == "tampered_bootstrap":
        Path(values["bootstrap"]).write_text("# changed bootstrap\n", encoding="utf-8")
    elif mutation == "wrong_expected_sha":
        text = text.replace(
            f'&quot;--expected-sha&quot; &quot;{"a" * 40}&quot;',
            f'&quot;--expected-sha&quot; &quot;{"b" * 40}&quot;',
            1,
        )
    elif mutation == "missing_expected_sha":
        text = text.replace(
            f'&quot;--expected-sha&quot; &quot;{"a" * 40}&quot; ',
            "",
            1,
        )
    elif mutation == "wrong_release_root":
        text = text.replace(
            f'&quot;{runtime}&quot;', f'&quot;{runtime.parent}&quot;', 1
        )
    elif mutation == "wrong_runner":
        text = text.replace(
            f'&quot;{runtime / "scripts" / "run_daily_intraday_capture.py"}&quot;',
            f'&quot;{runtime / "scripts" / "wrong_runner.py"}&quot;',
            1,
        )
    else:
        text = text.replace('&quot;--&quot; ', "", 1)
    xml.write_text(text, encoding="utf-8")
    result = _run(xml, values, canonical=True)
    assert result.returncode != 0


@pytest.mark.skipif(
    not APPROVED_PYTHON.is_file(),
    reason="The governed Python 3.13 interpreter is not installed on this host.",
)
def test_power_shell_preloader_pins_bootstrap_bytes(tmp_path: Path) -> None:
    preloader_result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f'. "{HELPER}"; Get-DawnstrikeCaptureBootstrapPreloader',
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert preloader_result.returncode == 0, preloader_result.stderr
    preloader = preloader_result.stdout.strip()
    assert preloader == BOOTSTRAP_PRELOADER
    bootstrap = tmp_path / "bootstrap.py"
    marker = tmp_path / "executed.txt"
    bootstrap.write_text(
        "from pathlib import Path; import sys; "
        "Path(sys.argv[1]).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    expected_hash = _sha(bootstrap)
    command = [
        str(APPROVED_PYTHON),
        "-I",
        "-B",
        "-S",
        "-c",
        preloader,
        str(bootstrap),
        expected_hash,
        str(marker),
    ]
    accepted = subprocess.run(command, text=True, capture_output=True, check=False)
    assert accepted.returncode == 0, accepted.stderr
    assert marker.read_text(encoding="utf-8") == "executed"

    bootstrap.write_text(bootstrap.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
    rejected = subprocess.run(command, text=True, capture_output=True, check=False)
    assert rejected.returncode != 0
    assert "bootstrap hash mismatch" in rejected.stderr


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("<Command>py.exe</Command>", "<Command>powershell.exe</Command>"),
        ("<RunLevel>LeastPrivilege</RunLevel>", "<RunLevel>HighestAvailable</RunLevel>"),
        ('<Actions Context="Author">', '<Actions Context="Other">'),
        ('<Principal id="Author">', '<Principal id="Other">'),
        ("</Principal>", "<GroupId>S-1-5-32-544</GroupId></Principal>"),
        ("</Exec>", "<ComHandler><ClassId>x</ClassId></ComHandler></Exec>"),
        ("&quot;--execute&quot;", "&quot;--execute&quot; &quot;--live&quot;"),
        ("<CalendarTrigger>", "<BootTrigger>"),
        ("</CalendarTrigger>", "</BootTrigger>"),
        ("T15:20:00-05:00", "T15:21:00-05:00"),
        ("<Friday/>", "<Friday/><Saturday/>"),
        (
            "</Triggers>",
            "<CalendarTrigger><StartBoundary>2026-08-31T15:20:00-05:00</StartBoundary>"
            "</CalendarTrigger></Triggers>",
        ),
    ],
)
def test_unsafe_action_or_principal_fails_closed(tmp_path: Path, old: str, new: str) -> None:
    xml, values = _fixture(tmp_path)
    xml.write_text(xml.read_text().replace(old, new), encoding="utf-8")
    result = _run(xml, values)
    assert result.returncode != 0


@pytest.mark.parametrize(
    "origin",
    [
        "ssh://alice:secret@github.com/mattfren/DawnStrike.git",
        "ftp://github.com/mattfren/DawnStrike.git",
        "https://alice:secret@github.com/mattfren/DawnStrike.git",
        "https://github.com/mattfren/DawnStrike.git?token=secret",
    ],
)
def test_origin_credentials_and_unknown_schemes_fail_closed(origin: str) -> None:
    escaped = origin.replace("'", "''")
    result = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            f'. "{HELPER}"; Get-DawnstrikeCanonicalOrigin \'{escaped}\'',
        ],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
