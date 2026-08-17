"""Run and durably capture bounded final commit static gates."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = Path(__file__).resolve().parent


def main() -> int:
    powershell_parse = (
        "$ErrorActionPreference='Stop'; $errors=@(); "
        "$paths=@(git ls-files '*.ps1'); "
        "foreach($relative in $paths){$tokens=$null;$parseErrors=$null;"
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        "(Join-Path (Get-Location) $relative),[ref]$tokens,[ref]$parseErrors);"
        "if($parseErrors){$errors += $parseErrors}}; "
        "if($errors.Count){$errors | Format-List | Out-String | Write-Error; exit 2}; "
        "Write-Output ('parsed_ps1=' + $paths.Count)"
    )
    commands = (
        ("ruff", [sys.executable, "-m", "ruff", "check", "."]),
        ("mypy", [sys.executable, "-m", "mypy", "intraday_scanner"]),
        (
            "compileall",
            [sys.executable, "-m", "compileall", "-q", "intraday_scanner", "scripts"],
        ),
        ("pip-check", [sys.executable, "-m", "pip", "check"]),
        (
            "detect-secrets-tracked",
            [
                sys.executable,
                "scripts/run_detect_secrets_tracked.py",
                "--baseline",
                ".secrets.baseline",
            ],
        ),
        ("node-check", ["node", "--check", "web/assets/dawnstrike.js"]),
        ("powershell-parse", ["powershell.exe", "-NoProfile", "-Command", powershell_parse]),
        ("git-diff-check", ["git", "diff", "--check"]),
    )
    results = []
    overall = 0
    for name, command in commands:
        started = datetime.now(timezone.utc)
        started_clock = time.monotonic()
        completed = subprocess.run(
            command, cwd=ROOT, check=False, capture_output=True, text=True
        )
        ended = datetime.now(timezone.utc)
        stdout_path = EVIDENCE / f"static-{name}.stdout.txt"
        stderr_path = EVIDENCE / f"static-{name}.stderr.txt"
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        result = {
            "name": name,
            "command": subprocess.list2cmdline(command),
            "started_at_utc": started.isoformat(),
            "ended_at_utc": ended.isoformat(),
            "elapsed_seconds": time.monotonic() - started_clock,
            "exit_code": completed.returncode,
            "stdout_sha256": hashlib.sha256(stdout_path.read_bytes()).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr_path.read_bytes()).hexdigest(),
        }
        results.append(result)
        overall = overall or completed.returncode
        print(json.dumps(result, sort_keys=True), flush=True)
        if completed.returncode != 0:
            break
    payload = {
        "schema_version": "dawnstrike.final_commit_static_gates.v1",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "results": results,
        "gate_count": len(results),
        "all_passed": overall == 0 and len(results) == len(commands),
        "exit_code": overall,
    }
    (EVIDENCE / "static-gates.json").write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return overall


if __name__ == "__main__":
    raise SystemExit(main())

