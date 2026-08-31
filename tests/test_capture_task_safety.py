from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "capture_task_safety.ps1"


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


def _run(xml: Path, values: dict[str, str]) -> subprocess.CompletedProcess[str]:
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
        '-RequirePasswordPrincipal -RequireRunner -AllowLegacyLauncher | ConvertTo-Json -Compress'
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
