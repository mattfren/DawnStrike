from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_dawnstrike_host_boundary.ps1"
BOOTSTRAP = ROOT / "scripts" / "bootstrap_dawnstrike_host_boundary.ps1"
LAUNCHER = ROOT / "scripts" / "dawnstrike_release_launcher.ps1"
POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")


def _quote(value: str | Path) -> str:
    return str(value).replace("'", "''")


def test_installer_imports_canonical_main_into_a_protected_exact_sha_release() -> None:
    text = INSTALLER.read_text(encoding="utf-8")

    for marker in (
        "https://github.com/mattfren/DawnStrike.git",
        "Join-Path $InstallRoot 'releases'",
        "'-c', 'core.autocrlf=false'",
        "ls-remote --exit-code $canonicalOrigin refs/heads/main",
        "+refs/heads/main:refs/remotes/origin/main",
        "fsck --full --strict --no-reflogs --no-dangling $ExpectedSha",
        "rev-parse --is-shallow-repository",
        ".git\\objects\\info\\alternates",
        "refs/replace/",
        "extensions\\.partialClone",
        "Protected exact-SHA release contains a reparse point.",
        "$candidate = $protectedReleaseRoot",
        "protected_release_root = $protectedReleaseRoot",
    ):
        assert marker in text

    assert "$candidate = [IO.Path]::GetFullPath($CandidateRoot)" not in text
    assert "$source = Join-Path $candidate $RelativePath" not in text


def test_exact_file_promotion_streams_the_git_object_not_the_worktree_path() -> None:
    text = INSTALLER.read_text(encoding="utf-8")

    assert "cat-file blob ' + $expectedBlob" in text
    assert "$process.StandardOutput.BaseStream.CopyTo($destinationHandle)" in text
    assert "[IO.FileMode]::CreateNew" in text
    assert "hash-object ('--path=' + $gitRelative) $temporary" in text
    assert "[IO.File]::Replace($temporary, $destinationFull" in text
    assert "[IO.File]::Open($source" not in text


def test_active_host_roots_publish_with_their_seals_in_one_directory_move() -> None:
    text = INSTALLER.read_text(encoding="utf-8")

    for marker in (
        ".dawnstrike-git-boundary-v1.json",
        ".dawnstrike-python-boundary-v1.json",
        ".dawnstrike-dependency-boundary-v1.json",
        ".dawnstrike-vercel-cli-boundary-v1.json",
        ".dawnstrike-node-boundary-v1.json",
        "Write-DawnstrikeProtectedGitManifest -Root $gitStage",
        "Write-DawnstrikeProtectedPythonManifest -Root $pythonStage",
        "Write-DawnstrikeProtectedDependencyManifest -Root $dependencyStage",
        "Write-DawnstrikeProtectedVercelManifest -Root $vercelStage",
        "Write-DawnstrikeProtectedNodeManifest -Root $nodeStage",
        "Write-DawnstrikeProtectedReleaseAdmission",
        "Assert-DawnstrikeProtectedReleaseAdmission",
        "$releaseDisposition = 'REUSED_EXACT_ATOMIC_IMPORT'",
    ):
        assert marker in text

    assert text.index("Write-DawnstrikeProtectedGitManifest -Root $gitStage") < text.index(
        "[IO.Directory]::Move($gitStage, $gitRoot)"
    )
    assert text.index("Write-DawnstrikeProtectedPythonManifest -Root $pythonStage") < text.index(
        "[IO.Directory]::Move($pythonStage, $pythonDestination)"
    )
    assert text.index(
        "Write-DawnstrikeProtectedDependencyManifest -Root $dependencyStage"
    ) < text.index("[IO.Directory]::Move($dependencyStage, $dependencyDestination)")
    assert text.index("Write-DawnstrikeProtectedVercelManifest -Root $vercelStage") < text.index(
        "[IO.Directory]::Move($vercelStage, $vercelRoot)"
    )
    assert text.index("Write-DawnstrikeProtectedNodeManifest -Root $nodeStage") < text.index(
        "[IO.Directory]::Move($nodeStage, $nodeRoot)"
    )


def test_node_is_downloaded_into_an_atomic_admin_protected_exact_boundary() -> None:
    text = INSTALLER.read_text(encoding="utf-8")

    for marker in (
        "https://nodejs.org/dist/v24.20.0/node-v24.20.0-win-x64.zip",
        "6cac9ffbca8f6a47091e4b5c772e0606049c3871cb67d900c0cedde630e545ba",
        "5c976096e04e5c2c1f091938926234cc9fbebfe9787ddd149351b3b0ecc707b5",
        "D1DC7755FAB17F01224CCCEA1C2FAEDC6F963E12",
        "Join-Path $InstallRoot 'Node-24.20.0'",
        "Expand-DawnstrikePinnedNodeExecutable",
        "Assert-DawnstrikeProtectedNodeBoundary",
        "FRESH_EXACT_ATOMIC_NODE",
        "REUSED_EXACT_SEALED_NODE",
        "node_boundary_manifest_sha256",
    ):
        assert marker in text
    assert text.index("Save-DawnstrikePinnedDownload -Uri $nodeArchiveUri") < text.index(
        "[IO.Directory]::Move($nodeStage, $nodeRoot)"
    )


def test_vercel_cli_is_copied_into_an_atomic_content_addressed_admin_boundary() -> None:
    text = INSTALLER.read_text(encoding="utf-8")

    for marker in (
        "$vercelRoot = Join-Path $InstallRoot ('VercelCli-' + $vercelTreeSha256)",
        "Copy-DawnstrikePinnedVercelTree -Source $vercelSourceRoot -Destination $vercelStage",
        "Assert-DawnstrikePinnedVercelPayload -Root $vercelStage",
        "Assert-DawnstrikeProtectedVercelTree",
        "vercel_cli_boundary_manifest_sha256",
        "FRESH_EXACT_ATOMIC_VERCEL_CLI",
        "REUSED_EXACT_SEALED_VERCEL_CLI",
    ):
        assert marker in text
    assert "[IO.FileMode]::CreateNew" in text
    assert "[IO.FileShare]::Read" in text


@pytest.mark.skipif(
    not POWERSHELL.is_file(),
    reason="native Start-Process argument binding proof requires Windows PowerShell 5.1",
)
def test_python_installer_target_directory_remains_one_native_argument(tmp_path: Path) -> None:
    expected = [
        "/quiet",
        "InstallAllUsers=1",
        r"TargetDir=C:\Program Files\Dawnstrike\.python-stage-fixture",
        "Include_launcher=0",
        "Include_test=0",
    ]
    probe = tmp_path / "native argument probe.py"
    probe.write_text(
        "import sys\nraise SystemExit(0 if sys.argv[1:] == " + repr(expected) + " else 9)\n",
        encoding="utf-8",
    )
    command = rf"""
    $stage = 'C:\Program Files\Dawnstrike\.python-stage-fixture'
    $arguments = @(
        '"{_quote(probe)}"',
        '/quiet',
        'InstallAllUsers=1',
        ('TargetDir="' + $stage + '"'),
        'Include_launcher=0',
        'Include_test=0'
    ) -join ' '
    $child = Start-Process -FilePath '{_quote(sys.executable)}' `
        -ArgumentList $arguments -Wait -PassThru -WindowStyle Hidden
    exit $child.ExitCode
    """

    completed = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(
    not POWERSHELL.is_file(),
    reason="raw Git-object promotion proof requires Windows PowerShell 5.1",
)
def test_exact_file_promotion_ignores_a_swapped_worktree_file(tmp_path: Path) -> None:
    repo = tmp_path / "protected release"
    destination_root = tmp_path / "protected install"
    destination = destination_root / "bin" / "payload.ps1"
    repo.mkdir()
    destination.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Dawnstrike Test"], cwd=repo, check=True)
    payload = repo / "scripts" / "payload.ps1"
    payload.parent.mkdir()
    payload.write_bytes(b"Write-Output 'admitted'\n")
    subprocess.run(["git", "add", "scripts/payload.ps1"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=repo, check=True)
    expected_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # This models the old candidate-path race: the checked-out pathname no
    # longer contains the admitted commit bytes when promotion begins.
    payload.write_bytes(b"Write-Output 'attacker replacement'\n")
    script = rf"""
    $ErrorActionPreference = 'Stop'
    $errors = $null
    $tokens = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile(
        '{_quote(INSTALLER)}', [ref]$tokens, [ref]$errors
    )
    if ($errors.Count -ne 0) {{ throw 'installer parse failed' }}
    $functionAst = $ast.Find({{
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -ceq 'Copy-DawnstrikeExactGitFile'
    }}, $true)
    Invoke-Expression $functionAst.Extent.Text
    function Assert-DawnstrikeInstallNoReparse {{ param($Path, $Label) }}
    function Set-DawnstrikeProtectedReadableFileAcl {{ param($Path) }}
    $git = (Get-Command git.exe -ErrorAction Stop).Source
    $candidate = '{_quote(repo)}'
    $InstallRoot = '{_quote(destination_root)}'
    $ExpectedSha = '{expected_sha}'
    $safeGit = @('-c', 'core.autocrlf=true', '-C', $candidate)
    Copy-DawnstrikeExactGitFile `
        -RelativePath 'scripts/payload.ps1' `
        -Destination '{_quote(destination)}'
    [Console]::Out.Write([Convert]::ToBase64String([IO.File]::ReadAllBytes('{_quote(destination)}')))
    """
    completed = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "V3JpdGUtT3V0cHV0ICdhZG1pdHRlZCcK"


def test_outer_uac_bootstrap_elevates_only_a_frozen_encoded_copy() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")

    for marker in (
        "Get-DawnstrikeFrozenInstallerContract",
        "cat-file blob",
        "ExpectedInstallerSha256",
        "ExpectedInstallerLength",
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "-EncodedCommand",
        "-Verb RunAs",
        "[IO.Directory]::Move(`$stage,`$final)",
        "Verify-Final",
        "[IO.FileMode]::CreateNew",
        "SetAccessRuleProtection(`$true,`$false)",
    ):
        assert marker in text
    assert text.rindex("$null = Get-DawnstrikeFrozenInstallerContract") < text.index(
        "$child = Start-Process"
    )
    assert "-File $frozenInstaller" not in text
    assert "& $CandidateRoot" not in text
    assert "& `$dest @args" in text


def test_launcher_and_installer_wire_governed_bootstrap_and_boundary_migration() -> None:
    launcher = LAUNCHER.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")

    for marker in (
        "'BootstrapBaseline'",
        "'MigrateBoundary'",
        "-BootstrapBaseline:($Mode -eq 'BootstrapBaseline')",
        "Migrate-DawnstrikeStateRootBoundaryCandidate",
        "expected_boundary_sha=",
        "expected_runtime_sha=",
        "candidate_admission_sha256=",
        "Complete-DawnstrikeStateBoundaryTaskMutationFailClosed",
        "dawnstrike.runtime_activation_fail_closed.v1",
        "terminal-recovery-",
    ):
        assert marker in launcher
    assert launcher.index("Migrate-DawnstrikeStateRootBoundaryCandidate") < launcher.index(
        "$stateBoundary = Assert-DawnstrikeStateRootBoundary", launcher.index("MigrateBoundary")
    )
    for marker in (
        "-Mode MigrateBoundary",
        "-BoundaryPredecessorSha $BoundaryPredecessorSha",
        "-RuntimePredecessorSha $RuntimePredecessorSha",
        "Protected StateRoot candidate migration terminal identity is invalid.",
    ):
        assert marker in installer


def test_two_install_bootstrap_lifecycle_is_explicit_and_ordered() -> None:
    runbook = (
        ROOT / "docs" / "operations" / "runtime_activation_and_rollback.md"
    ).read_text(encoding="utf-8")
    section = runbook.split("The first protected release uses an explicit two-install", 1)[1]
    preloader = section.index("function Invoke-FrozenDawnstrikeBootstrapBridge")
    install_a = section.index("ExpectedSha = $shaA")
    bootstrap_a = section.index("-Mode BootstrapBaseline")
    install_b = section.index("ExpectedSha = $shaB")
    migrate_b = section.index("BoundaryPredecessorSha = $shaA")
    activate_b = section.index("-Mode Activate")
    assert preloader < install_a < bootstrap_a < install_b < migrate_b < activate_b
    for marker in (
        "-ExpectedSha256 $bridgeShaA -ExpectedLength $bridgeLengthA",
        "-ExpectedSha256 $bridgeShaB -ExpectedLength $bridgeLengthB",
        "ExpectedInstallerSha256 = $installerShaA",
        "ExpectedInstallerLength = $installerLengthA",
        "ExpectedInstallerSha256 = $installerShaB",
        "ExpectedInstallerLength = $installerLengthB",
        "BoundaryPredecessorTree = $treeA",
        "RuntimePredecessorSha = $shaA",
        "RuntimePredecessorTree = $treeA",
        "[IO.File]::Open($Path, 'Open', 'Read', 'None')",
        "[ScriptBlock]::Create([Text.Encoding]::UTF8.GetString($bytes))",
        "BOOTSTRAP authorization for A",
        "current ACTIVATE authorization",
        "A missing, mismatched, or nonterminal receipt is a stop condition",
    ):
        assert marker in section
    direct_bridge = (
        "-File C:\\r\\dawnstrike-main\\scripts\\bootstrap_dawnstrike_host_boundary.ps1"
    )
    assert direct_bridge not in section


@pytest.mark.skipif(
    not POWERSHELL.is_file(),
    reason="frozen installer stream proof requires Windows PowerShell 5.1",
)
def test_frozen_installer_contract_rejects_same_length_byte_tamper(tmp_path: Path) -> None:
    payload = b"# exact governed installer fixture\r\nWrite-Output 'safe'\r\n"
    expected = hashlib.sha256(payload).hexdigest()
    frozen = tmp_path / "frozen installer.ps1"
    frozen.write_bytes(payload)
    script = rf"""
    $ErrorActionPreference='Stop'
    $tokens=$null;$errors=$null
    $ast=[Management.Automation.Language.Parser]::ParseFile(
      '{_quote(BOOTSTRAP)}',[ref]$tokens,[ref]$errors)
    $fn=$ast.Find({{param($n)
      $n-is [Management.Automation.Language.FunctionDefinitionAst]-and
      $n.Name-ceq 'Get-DawnstrikeFrozenInstallerContract'}},$true)
    Invoke-Expression $fn.Extent.Text
    $good=$false;$blocked=$false
    $stream=[IO.File]::Open('{_quote(frozen)}','Open','Read','Read')
    try{{$result=Get-DawnstrikeFrozenInstallerContract -Stream $stream `
      -ExpectedSha256 '{expected}' -ExpectedLength {len(payload)};
      $good=([string]$result.sha256-ceq '{expected}')}}finally{{$stream.Dispose()}}
    $bytes=[IO.File]::ReadAllBytes('{_quote(frozen)}');$bytes[0]=$bytes[0]-bxor 1
    [IO.File]::WriteAllBytes('{_quote(frozen)}',$bytes)
    $stream=[IO.File]::Open('{_quote(frozen)}','Open','Read','Read')
    try{{try{{$null=Get-DawnstrikeFrozenInstallerContract -Stream $stream `
      -ExpectedSha256 '{expected}' -ExpectedLength {len(payload)}}}catch{{$blocked=$true}}}}
    finally{{$stream.Dispose()}}
    [pscustomobject]@{{good=$good;tamper_blocked=$blocked}}|ConvertTo-Json -Compress
    """
    completed = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip()) == {"good": True, "tamper_blocked": True}


@pytest.mark.skipif(
    not POWERSHELL.is_file(),
    reason="generated elevated payload parse proof requires Windows PowerShell 5.1",
)
def test_generated_outer_bootstrap_encoded_payload_is_parseable_and_bounded() -> None:
    script = rf"""
    $ErrorActionPreference='Stop'
    $tokens=$null;$errors=$null
    $ast=[Management.Automation.Language.Parser]::ParseFile(
      '{_quote(BOOTSTRAP)}',[ref]$tokens,[ref]$errors)
    if($errors.Count){{throw 'outer parse failed'}}
    $assignment=$ast.Find({{param($n)
      $n-is [Management.Automation.Language.AssignmentStatementAst]-and
      $n.Left-is [Management.Automation.Language.VariableExpressionAst]-and
      $n.Left.VariablePath.UserPath-ceq 'elevatedCommand'}},$true)
    $sourceLiteral="'C:\temp\installer.ps1'";$shaLiteral="'$([string]'a'*40)'"
    $hashLiteral="'$([string]'b'*64)'";$ExpectedInstallerLength=12345
    $candidateLiteral="'C:\r\candidate'";$boundaryShaLiteral="''"
    $boundaryTreeLiteral="''";$runtimeShaLiteral="''";$runtimeTreeLiteral="''"
    Invoke-Expression $assignment.Extent.Text
    $innerTokens=$null;$innerErrors=$null
    [Management.Automation.Language.Parser]::ParseInput(
      $elevatedCommand,[ref]$innerTokens,[ref]$innerErrors)>$null
    $encoded=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($elevatedCommand))
    [pscustomobject]@{{parse_errors=$innerErrors.Count;encoded_length=$encoded.Length}}|
      ConvertTo-Json -Compress
    """
    completed = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip())
    assert result["parse_errors"] == 0
    assert result["encoded_length"] < 28000


@pytest.mark.skipif(
    not POWERSHELL.is_file(),
    reason="pre-elevation bridge admission proof requires Windows PowerShell 5.1",
)
def test_builtin_preloader_rejects_hostile_bridge_before_execution(tmp_path: Path) -> None:
    runbook = (
        ROOT / "docs" / "operations" / "runtime_activation_and_rollback.md"
    ).read_text(encoding="utf-8")
    preloader = runbook.split("function Invoke-FrozenDawnstrikeBootstrapBridge", 1)[1]
    preloader = "function Invoke-FrozenDawnstrikeBootstrapBridge" + preloader.split(
        "# These exact bridge bytes run unelevated", 1
    )[0]
    trusted = b"param($Marker) [IO.File]::WriteAllText($Marker, 'trusted')\r\n"
    hostile = b"param($Marker) [IO.File]::WriteAllText($Marker, 'hostile')\r\n"
    assert len(trusted) == len(hostile)
    bridge = tmp_path / "mutable bridge.ps1"
    marker = tmp_path / "elevated marker.txt"
    bridge.write_bytes(hostile)
    expected = hashlib.sha256(trusted).hexdigest()
    script = (
        preloader
        + rf"""
        $blocked=$false
        try {{
          Invoke-FrozenDawnstrikeBootstrapBridge `
            -Path '{_quote(bridge)}' -ExpectedSha256 '{expected}' `
            -ExpectedLength {len(trusted)} -Arguments @{{Marker='{_quote(marker)}'}}
        }} catch {{$blocked=$true}}
        [pscustomobject]@{{blocked=$blocked;executed=(Test-Path -LiteralPath '{_quote(marker)}')}}|
          ConvertTo-Json -Compress
        """
    )
    completed = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip()) == {"blocked": True, "executed": False}
