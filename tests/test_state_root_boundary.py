# ruff: noqa: E501
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
HELPER = ROOT / "scripts" / "state_root_boundary.ps1"


def _quote(value: str | Path) -> str:
    return str(value).replace("'", "''")


def _run_ps(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )


def test_state_boundary_is_installed_and_admitted_before_production_dispatch() -> None:
    installer = (ROOT / "scripts" / "install_dawnstrike_host_boundary.ps1").read_text(
        encoding="utf-8"
    )
    launcher = (ROOT / "scripts" / "dawnstrike_release_launcher.ps1").read_text(
        encoding="utf-8"
    )
    runner = (ROOT / "scripts" / "dawnstrike_process_runner.ps1").read_text(
        encoding="utf-8"
    )
    for marker in (
        "state_root_boundary.ps1",
        "Install-DawnstrikeStateRootBoundary",
        "candidate_tree = $candidateTree",
        "state_boundary_receipt_sha256",
        "state_boundary_rollback_manifest_sha256",
        "DISABLED_PENDING_GOVERNED_ACTIVATE_RESEAL",
        "DISABLED_PENDING_GOVERNED_HARDEN_CAPTURE_REBIND",
    ):
        assert marker in installer
    assert installer.index("Copy-DawnstrikeExactGitFile -RelativePath $stateBoundaryRelative") < (
        installer.index("Install-DawnstrikeStateRootBoundary")
    )
    assert "Assert-DawnstrikeStateRootBoundary -StateRoot $StateRoot" in launcher
    assert launcher.index("Assert-DawnstrikeStateRootBoundary -StateRoot $StateRoot") < (
        launcher.index("& $entryLocks[0].path")
    )
    assert "RunAsCredential is not an exact ACL-admitted StateRoot writer SID" in launcher
    assert '"scripts/state_root_boundary.ps1"' in runner
    assert "Scheduled StateRoot helper hash mismatch" in runner
    assert runner.index("Assert-DawnstrikeStateRootBoundary -StateRoot") < runner.index(
        "Scheduled launch manifest hash mismatch"
    )


def test_task_definition_reseal_has_durable_intent_completion_and_narrow_resume() -> None:
    helper = HELPER.read_text(encoding="utf-8")
    launcher = (ROOT / "scripts" / "dawnstrike_release_launcher.ps1").read_text(
        encoding="utf-8"
    )
    for marker in (
        "dawnstrike.state_boundary_task_mutation.v1",
        "dawnstrike.state_boundary_task_mutation_completion.v1",
        "old_current_receipt_sha256",
        "old_task_binding_sha256",
        "A task outside the protected mutation scope changed definition",
        "Disable-DawnstrikeStateBoundaryAffectedTasks",
        "Complete-DawnstrikeStateBoundaryTaskMutationAdoption",
        "StateRoot task binding has an unresolved protected mutation intent",
    ):
        assert marker in helper
    completion = helper.split("function Complete-DawnstrikeStateBoundaryTaskMutation {", 1)[1]
    assert completion.index("-Payload $completionPayload -Path $completionPath") < completion.index(
        "-Payload $newReceipt -Path $currentPath"
    )
    assert completion.index("-Payload $newReceipt -Path $currentPath") < completion.index(
        "Remove-Item -LiteralPath ([string]$intent.path)"
    )
    assert launcher.index("Enter-DawnstrikeStateBoundaryTaskMutation") < launcher.index(
        "& $entryLocks[0].path"
    )
    assert launcher.index("& $entryLocks[0].path") < launcher.index(
        "Complete-DawnstrikeStateBoundaryTaskMutation"
    )
    main = launcher.split("$taskMutationAlreadyCompleted = $false", 1)[1]
    assert main.index("Complete-DawnstrikeStateBoundaryTaskMutation") < main.index(
        "\nfinally {"
    )


def test_retry_admission_accepts_only_validated_completion_new_current_lineage() -> None:
    helper = HELPER.read_text(encoding="utf-8")
    admission = helper.split(
        "function Get-DawnstrikeStateBoundaryTaskMutationReadAdmission {", 1
    )[1].split("function Enter-DawnstrikeStateBoundaryTaskMutation {", 1)[0]
    assert "old_current_receipt_sha256" in admission
    assert "old_task_binding_sha256" in admission
    assert "Complete-DawnstrikeStateBoundaryTaskMutationAdoption `" in admission
    assert "-StateRoot $state -EvidenceRoot $evidence -ValidationOnly" in admission
    assert (
        admission.index("Complete-DawnstrikeStateBoundaryTaskMutationAdoption `")
        < admission.index("return $boundary")
    )

    adoption = helper.split(
        "function Complete-DawnstrikeStateBoundaryTaskMutationAdoption {", 1
    )[1].split(
        "function Get-DawnstrikeStateBoundaryTaskMutationReadAdmission {", 1
    )[0]
    validation_return = adoption.index("status = 'VALIDATED_COMPLETION_LINEAGE'")
    intent_remove = adoption.index("Remove-Item -LiteralPath ([string]$Intent.path)")
    assert validation_return < intent_remove
    assert (
        "Read admission completion lineage is not the exact new current receipt."
        in adoption
    )

    completion = helper.split("function Complete-DawnstrikeStateBoundaryTaskMutation {", 1)[1]
    terminal_proof = completion.index(
        "Get-DawnstrikeStateBoundaryTaskMutationTerminalEvidence `"
    )
    completion_write = completion.index(
        "-Payload $completionPayload -Path $completionPath"
    )
    current_write = completion.index(
        "-Payload $newReceipt -Path $currentPath"
    )
    pending_remove = completion.index("Remove-Item -LiteralPath ([string]$intent.path)")
    assert terminal_proof < completion_write < current_write < pending_remove


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="requires Windows PowerShell 5.1")
def test_completion_new_current_validation_preserves_pending_for_request_hash(
    tmp_path: Path,
) -> None:
    pending = tmp_path / "state-boundary-task-mutation-pending.json"
    pending.write_text("pending", encoding="utf-8")
    state = tmp_path / "state"
    script = f"""
$ErrorActionPreference='Stop'
. '{_quote(HELPER)}'
$sha='{'a' * 40}';$tree='{'b' * 40}';$request='{'c' * 64}';$predecessor='{'d' * 64}'
$operation='{'e' * 32}';$old='{'1' * 64}';$receiptHash=''
$state=[IO.Path]::GetFullPath('{_quote(state)}').TrimEnd('\')
$task=[pscustomobject]@{{task_name='fixture';task_path='\';principal_sid='S-1-5-21-1';logon_type='Password';run_level='Limited';definition_sha256=('2'*64);definition_contract_sha256=('3'*64);action_contract_sha256=('4'*64);action_section_sha256=('5'*64);canonical_task_contract_sha256='';canonical_task_definition_contract_sha256='';canonical_task_action_contract_sha256='';canonical=$false}} # pragma: allowlist secret
$script:terminalContract=[pscustomobject][ordered]@{{mode='fixture';proof=('6'*64)}}
$newReceipt=[ordered]@{{schema_version='dawnstrike.state_boundary_installation.v2';status='PASS';candidate_sha=$sha;candidate_tree=$tree;state_root=$state;writer_sids=@('S-1-5-21-1');task_definitions_and_principals=@($task);task_binding_operation_id=$operation;task_binding_mode='RebindCapture';task_binding_release_sha=$sha;task_binding_release_tree=$tree;task_binding_request_contract_sha256=$request;task_binding_predecessor_terminal_evidence_sha256=$predecessor;task_binding_terminal_receipt_sha256=('7'*64);task_binding_terminal_journal_sha256=('8'*64);research_only=$true;broker_execution_enabled=$false}}
foreach($name in @('operation_id','installed_at_utc','state_root_identity','state_root_sddl','state_root_sddl_sha256','locks_root','locks_root_identity','locks_root_sddl','locks_root_sddl_sha256','state_entry_count','state_identity_contract_sha256','rollback_manifest_path','rollback_manifest_sha256','installed_helper_path','installed_helper_sha256')){{$newReceipt[$name]='fixture'}}
$newReceipt['task_binding_sha256']=Get-DawnstrikeStateBoundaryTaskBindingHash -Tasks @($task)
$newReceipt['task_binding_terminal_task_contract_sha256']=Get-DawnstrikeStateBoundarySha256Text ($script:terminalContract|ConvertTo-Json -Compress)
$newHash=Get-DawnstrikeStateBoundarySha256Text (($newReceipt|ConvertTo-Json -Depth 20)+"`r`n")
$script:newReceipt=[pscustomobject]$newReceipt;$script:boundaryHash=$newHash
$terminal=[pscustomobject][ordered]@{{receipt_path='fixture-receipt';receipt_sha256=('7'*64);journal_path='fixture-journal';journal_sha256=('8'*64);task_contract=$script:terminalContract}}
$intent=[pscustomobject]@{{path='{_quote(pending)}';payload=[pscustomobject]@{{operation_id=$operation;mode='RebindCapture';expected_sha=$sha;expected_tree=$tree;request_contract_sha256=$request;predecessor_terminal_evidence_sha256=$predecessor;old_current_receipt_sha256=$old}}}}
$completion=[pscustomobject]@{{path='fixture-completion';payload=[pscustomobject]@{{schema_version='dawnstrike.state_boundary_task_mutation_completion.v1';operation_id=$operation;mode='RebindCapture';expected_sha=$sha;expected_tree=$tree;request_contract_sha256=$request;predecessor_terminal_evidence_sha256=$predecessor;old_current_receipt_sha256=$old;new_current_receipt_sha256=$newHash;new_current_receipt=$script:newReceipt;terminal_evidence=$terminal;research_only=$true;broker_execution_enabled=$false}}}}
function Get-DawnstrikeStateBoundaryTaskMutationTerminalEvidence {{param($StateRoot,$Mode,$ExpectedSha,$ExpectedTree,$ReceiptPath,$JournalPath,$ExpectedReceiptSha256,$ExpectedJournalSha256);[pscustomobject]@{{record=$terminal;locks=@()}}}}
function Assert-DawnstrikeStateRootBoundary {{param($StateRoot,$EvidenceRoot,$AllowedTaskMutationOperationId,[switch]$AllowTaskDefinitionDrift);[pscustomobject]@{{receipt=$script:newReceipt;receipt_sha256=$script:boundaryHash;writer_sids=@('S-1-5-21-1');locks=@()}}}}
function Get-DawnstrikeStateBoundaryTaskInventory {{return @($task)}}
function Assert-DawnstrikeStateBoundaryTaskInventoryMatches {{param($ExpectedTasks,$LiveTasks,$WriterSids);return $true}}
function Assert-DawnstrikeStateBoundaryTerminalTaskContract {{param($Mode,$TerminalRecord,$LiveTasks);return $script:terminalContract}}
$validated=Complete-DawnstrikeStateBoundaryTaskMutationAdoption -Intent $intent -Completion $completion -StateRoot $state -EvidenceRoot '{_quote(tmp_path)}' -ValidationOnly
$pendingPreserved=Test-Path -LiteralPath '{_quote(pending)}' -PathType Leaf
$script:boundaryHash='9'*64
$tamperBlocked=$false
try {{$null=Complete-DawnstrikeStateBoundaryTaskMutationAdoption -Intent $intent -Completion $completion -StateRoot $state -EvidenceRoot '{_quote(tmp_path)}' -ValidationOnly}} catch {{$tamperBlocked=$true}}
[pscustomobject]@{{status=$validated.status;pending=$pendingPreserved;tamper=$tamperBlocked}}|ConvertTo-Json -Compress
"""
    result = _run_ps(script)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "status": "VALIDATED_COMPLETION_LINEAGE",
        "pending": True,
        "tamper": True,
    }


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="requires Windows PowerShell 5.1")
def test_state_boundary_scripts_parse_under_windows_powershell_51() -> None:
    paths = [
        HELPER,
        ROOT / "scripts" / "install_dawnstrike_host_boundary.ps1",
        ROOT / "scripts" / "dawnstrike_process_runner.ps1",
        ROOT / "scripts" / "dawnstrike_release_launcher.ps1",
    ]
    joined = ",".join(f"'{_quote(path)}'" for path in paths)
    script = f"""
$ErrorActionPreference='Stop'
$failures=@()
foreach($path in @({joined})) {{
  $tokens=$null;$errors=$null
  [void][Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$errors)
  if($errors.Count -ne 0) {{$failures += ($path + ':' + ($errors.Message -join ';'))}}
}}
if($failures.Count -ne 0) {{throw ($failures -join "`n")}}
'PASS'
"""
    result = _run_ps(script)
    assert result.returncode == 0, result.stderr
    assert "PASS" in result.stdout


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="requires Windows PowerShell 5.1")
def test_exact_state_acl_rejects_hostile_writer_inheritance_and_overbroad_writer() -> None:
    script = f"""
$ErrorActionPreference='Stop'
. '{_quote(HELPER)}'
$writer='S-1-5-21-100-200-300-400'
$exact=New-DawnstrikeStateBoundaryAcl -Directory $true -WriterSids @($writer) -AnchorDirectory
$exactPass=$false
try {{$null=Assert-DawnstrikeStateBoundaryAclObject -Acl $exact -Directory $true -WriterSids @($writer) -AnchorDirectory;$exactPass=$true}} catch {{}}

$hostile=New-DawnstrikeStateBoundaryAcl -Directory $true -WriterSids @($writer) -AnchorDirectory
$inherit=[Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit
$hostile.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
  [Security.Principal.SecurityIdentifier]::new('S-1-5-11'),
  ([Security.AccessControl.FileSystemRights]::Modify -bor [Security.AccessControl.FileSystemRights]::Synchronize),
  $inherit,[Security.AccessControl.PropagationFlags]::None,
  [Security.AccessControl.AccessControlType]::Allow))
$extraBlocked=$false
try {{$null=Assert-DawnstrikeStateBoundaryAclObject -Acl $hostile -Directory $true -WriterSids @($writer) -AnchorDirectory}} catch {{$extraBlocked=$true}}

$inherited=New-DawnstrikeStateBoundaryAcl -Directory $true -WriterSids @($writer) -AnchorDirectory
$inherited.SetAccessRuleProtection($false,$true)
$inheritanceBlocked=$false
try {{$null=Assert-DawnstrikeStateBoundaryAclObject -Acl $inherited -Directory $true -WriterSids @($writer) -AnchorDirectory}} catch {{$inheritanceBlocked=$true}}

$overbroad=New-DawnstrikeStateBoundaryAcl -Directory $true -WriterSids @($writer) -AnchorDirectory
$writerSid=[Security.Principal.SecurityIdentifier]::new($writer)
$old=[Security.AccessControl.FileSystemAccessRule]::new(
  $writerSid,([Security.AccessControl.FileSystemRights]::Modify -bor [Security.AccessControl.FileSystemRights]::Synchronize),
  $inherit,[Security.AccessControl.PropagationFlags]::None,
  [Security.AccessControl.AccessControlType]::Allow)
$overbroad.RemoveAccessRuleAll($old)
$overbroad.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
  $writerSid,[Security.AccessControl.FileSystemRights]::FullControl,
  $inherit,[Security.AccessControl.PropagationFlags]::None,
  [Security.AccessControl.AccessControlType]::Allow))
$overbroadBlocked=$false
try {{$null=Assert-DawnstrikeStateBoundaryAclObject -Acl $overbroad -Directory $true -WriterSids @($writer) -AnchorDirectory}} catch {{$overbroadBlocked=$true}}

$writerRules=@($exact.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) | Where-Object {{$_.IdentityReference.Value -eq $writer}})
$direct=@($writerRules | Where-Object {{$_.InheritanceFlags -eq [Security.AccessControl.InheritanceFlags]::None}})
$inheritOnly=@($writerRules | Where-Object {{$_.PropagationFlags -eq [Security.AccessControl.PropagationFlags]::InheritOnly}})
$unsafe=[Security.AccessControl.FileSystemRights]::Delete -bor [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor [Security.AccessControl.FileSystemRights]::ChangePermissions -bor [Security.AccessControl.FileSystemRights]::TakeOwnership
$rootSafe=$direct.Count -eq 1 -and ($direct[0].FileSystemRights -band $unsafe) -eq 0
$childLifecycle=$inheritOnly.Count -eq 1 -and ($inheritOnly[0].FileSystemRights -band [Security.AccessControl.FileSystemRights]::Modify) -eq [Security.AccessControl.FileSystemRights]::Modify
$descendant=New-DawnstrikeStateBoundaryAcl -Directory $true -WriterSids @($writer)
$descendantPass=$false
try {{$null=Assert-DawnstrikeStateBoundaryAclObject -Acl $descendant -Directory $true -WriterSids @($writer);$descendantPass=$true}} catch {{}}
$descendantRules=@($descendant.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) | Where-Object {{$_.IdentityReference.Value -eq $writer}})
$descendantLifecycle=$descendantRules.Count -eq 1 -and ($descendantRules[0].FileSystemRights -band [Security.AccessControl.FileSystemRights]::Modify) -eq [Security.AccessControl.FileSystemRights]::Modify -and ($descendantRules[0].FileSystemRights -band ([Security.AccessControl.FileSystemRights]::ChangePermissions -bor [Security.AccessControl.FileSystemRights]::TakeOwnership)) -eq 0

[pscustomobject]@{{exact=$exactPass;extra=$extraBlocked;inheritance=$inheritanceBlocked;overbroad=$overbroadBlocked;root_safe=$rootSafe;child_lifecycle=$childLifecycle;descendant=$descendantPass;descendant_lifecycle=$descendantLifecycle}}|ConvertTo-Json -Compress
"""
    result = _run_ps(script)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload == {
        "exact": True,
        "extra": True,
        "inheritance": True,
        "overbroad": True,
        "root_safe": True,
        "child_lifecycle": True,
        "descendant": True,
        "descendant_lifecycle": True,
    }


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="requires Windows PowerShell 5.1")
def test_task_principal_drift_is_rejected_against_exact_writer_sid_set() -> None:
    script = f"""
$ErrorActionPreference='Stop'
. '{_quote(HELPER)}'
$writer='S-1-5-21-100-200-300-400'
$hostile='S-1-5-21-100-200-300-401'
$expected=@(
  [pscustomobject]@{{task_name='Dawnstrike AlphaOps Morning';task_path='\';principal_sid=$writer;logon_type='Password';run_level='Limited';canonical=$true}}, # pragma: allowlist secret
  [pscustomobject]@{{task_name='Dawnstrike 10of10 Daily Finalize';task_path='\';principal_sid=$writer;logon_type='Password';run_level='Limited';canonical=$true}} # pragma: allowlist secret
)
$live=@(
  [pscustomobject]@{{task_name='Dawnstrike AlphaOps Morning';task_path='\';principal_sid=$writer;logon_type='Password';run_level='Limited';canonical=$true}}, # pragma: allowlist secret
  [pscustomobject]@{{task_name='Dawnstrike 10of10 Daily Finalize';task_path='\';principal_sid=$writer;logon_type='Password';run_level='Limited';canonical=$true}} # pragma: allowlist secret
)
$pass=$false
try {{$null=Assert-DawnstrikeStateBoundaryTaskPrincipalsMatch -ExpectedTasks $expected -LiveTasks $live -WriterSids @($writer);$pass=$true}} catch {{}}
$live[1].principal_sid=$hostile
$blocked=$false
try {{$null=Assert-DawnstrikeStateBoundaryTaskPrincipalsMatch -ExpectedTasks $expected -LiveTasks $live -WriterSids @($writer)}} catch {{$blocked=$true}}
[pscustomobject]@{{pass=$pass;blocked=$blocked}}|ConvertTo-Json -Compress
"""
    result = _run_ps(script)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "pass": True,
        "blocked": True,
    }


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="requires Windows PowerShell 5.1")
def test_pending_crash_intent_blocks_dispatch_and_success_only_holds_capture_disabled(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    pending = evidence / "state-boundary-pending-interrupted.json"
    pending.write_text("{}", encoding="utf-8")
    script = f"""
$ErrorActionPreference='Stop'
. '{_quote(HELPER)}'
$pendingBlocked=$false
try {{$null=Assert-DawnstrikeStateBoundaryNoPendingRecovery -EvidenceRoot '{_quote(evidence)}'}} catch {{$pendingBlocked=$true}}
$events=@()
function Enable-ScheduledTask {{param($TaskName,$TaskPath,$ErrorAction);$script:events += ('ENABLE:'+$TaskName)}}
function Disable-ScheduledTask {{param($TaskName,$TaskPath,$ErrorAction);$script:events += ('DISABLE:'+$TaskName)}}
$records=@(
  [pscustomobject]@{{task_name='Dawnstrike AlphaOps Morning';task_path='\';state='Ready'}},
  [pscustomobject]@{{task_name='Dawnstrike Delayed SIP Capture';task_path='\';state='Ready'}}
)
Restore-DawnstrikeStateBoundaryTaskStates -TaskRecords $records -SuccessfulInstallation
$successEvents=@($events)
$events=@()
Restore-DawnstrikeStateBoundaryTaskStates -TaskRecords $records
$rollbackEvents=@($events)
[pscustomobject]@{{pending=$pendingBlocked;success=$successEvents;rollback=$rollbackEvents}}|ConvertTo-Json -Compress
"""
    result = _run_ps(script)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["pending"] is True
    assert payload["success"] == [
        "DISABLE:Dawnstrike AlphaOps Morning",
        "DISABLE:Dawnstrike Delayed SIP Capture",
    ]
    assert payload["rollback"] == [
        "ENABLE:Dawnstrike AlphaOps Morning",
        "ENABLE:Dawnstrike Delayed SIP Capture",
    ]


def test_acl_mutation_intent_is_durable_before_the_first_task_or_acl_mutation() -> None:
    helper = HELPER.read_text(encoding="utf-8")
    install = helper.split("function Install-DawnstrikeStateRootBoundary {", 1)[1]
    pending = install.index(
        "Write-DawnstrikeStateBoundaryProtectedJson -Payload $pendingPayload -Path $pendingPath"
    )
    disable = install.index("Set-DawnstrikeStateBoundaryTasksDisabled -TaskRecords $taskRecords")
    acl_mutation = install.index("Set-DawnstrikeStateBoundaryPathAcl")
    assert pending < disable < acl_mutation


def test_acl_recovery_reproves_quiescence_and_exact_manifest_before_reenable() -> None:
    helper = HELPER.read_text(encoding="utf-8")
    recovery = helper.split(
        "function Invoke-DawnstrikeStateBoundaryPendingRecovery {", 1
    )[1].split("function Install-DawnstrikeStateRootBoundary {", 1)[0]
    recovery_disable = recovery.index(
        "Set-DawnstrikeStateBoundaryTasksDisabled -TaskRecords $manifest.tasks"
    )
    recovery_quiescent = recovery.index(
        "Assert-DawnstrikeStateBoundaryQuiescent -StateRoot $StateRoot",
        recovery_disable,
    )
    recovery_restore = recovery.index(
        "Restore-DawnstrikeStateBoundaryAcls -StateRoot $StateRoot",
        recovery_quiescent,
    )
    recovery_manifest = recovery.index(
        "Assert-DawnstrikeStateBoundaryManifestRestored", recovery_restore
    )
    recovery_recheck = recovery.index(
        "Assert-DawnstrikeStateBoundaryQuiescent -StateRoot $StateRoot",
        recovery_manifest,
    )
    recovery_reenable = recovery.index(
        "Restore-DawnstrikeStateBoundaryTaskStates -TaskRecords $manifest.tasks",
        recovery_recheck,
    )
    recovery_remove = recovery.index("Remove-Item -LiteralPath $pendingFiles[0].FullName")
    assert (
        recovery_disable
        < recovery_quiescent
        < recovery_restore
        < recovery_manifest
        < recovery_recheck
        < recovery_reenable
        < recovery_remove
    )

    install = helper.split("function Install-DawnstrikeStateRootBoundary {", 1)[1]
    catch = install.split("$failure = $_.Exception.Message", 1)[1]
    catch_disable = catch.index(
        "Set-DawnstrikeStateBoundaryTasksDisabled -TaskRecords $taskRecords"
    )
    catch_quiescent = catch.index(
        "Assert-DawnstrikeStateBoundaryQuiescent -StateRoot $state", catch_disable
    )
    catch_restore = catch.index(
        "Restore-DawnstrikeStateBoundaryAcls -StateRoot $state", catch_quiescent
    )
    catch_manifest = catch.index(
        "Assert-DawnstrikeStateBoundaryManifestRestored", catch_restore
    )
    catch_recheck = catch.index(
        "Assert-DawnstrikeStateBoundaryQuiescent -StateRoot $state", catch_manifest
    )
    catch_reenable = catch.index(
        "Restore-DawnstrikeStateBoundaryTaskStates -TaskRecords $taskRecords",
        catch_recheck,
    )
    catch_remove = catch.index("Remove-Item -LiteralPath $pendingPath")
    assert (
        catch_disable
        < catch_quiescent
        < catch_restore
        < catch_manifest
        < catch_recheck
        < catch_reenable
        < catch_remove
    )


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="requires Windows PowerShell 5.1")
def test_restored_acl_manifest_rejects_hostile_descendant_drift(tmp_path: Path) -> None:
    state = tmp_path / "state"
    script = f"""
$ErrorActionPreference='Stop'
. '{_quote(HELPER)}'
$script:hostile=$false
function New-FixtureRecord([string]$relative,[bool]$directory,[string]$identity,[string]$sddl) {{
  [pscustomobject]@{{relative_path=$relative;path=$relative;is_directory=$directory;identity=$identity;sddl=$sddl;sddl_sha256=(Get-DawnstrikeStateBoundarySha256Text $sddl);handle=$null}}
}}
function Get-DawnstrikeStateBoundaryTreeSnapshot {{
  param([string]$StateRoot)
  $records=@(
    (New-FixtureRecord '.' $true '00000001:0000000000000001' 'root-sddl'),
    (New-FixtureRecord 'locks' $true '00000001:0000000000000002' 'locks-sddl'),
    (New-FixtureRecord 'original.json' $false '00000001:0000000000000003' 'file-sddl')
  )
  if($script:hostile) {{
    $records += New-FixtureRecord 'hostile-after-manifest.json' $false '00000001:0000000000000004' 'hostile-sddl'
  }}
  return @($records)
}}
$snapshot=@(Get-DawnstrikeStateBoundaryTreeSnapshot -StateRoot '{_quote(state)}')
try {{$manifest=@(Get-DawnstrikeStateBoundaryManifestEntries -Snapshot $snapshot)}}
finally {{foreach($record in $snapshot){{if($null -ne $record.handle){{$record.handle.Dispose()}}}}}}
$exact=$false
try {{$null=Assert-DawnstrikeStateBoundaryManifestRestored -StateRoot '{_quote(state)}' -Entries $manifest;$exact=$true}} catch {{}}
$script:hostile=$true
$blocked=$false
try {{$null=Assert-DawnstrikeStateBoundaryManifestRestored -StateRoot '{_quote(state)}' -Entries $manifest}} catch {{$blocked=$true}}
[pscustomobject]@{{exact=$exact;blocked=$blocked}}|ConvertTo-Json -Compress
"""
    result = _run_ps(script)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "exact": True,
        "blocked": True,
    }


def test_host_receipt_root_is_bound_before_acl_and_receipt_is_atomic_no_replace() -> None:
    installer = (ROOT / "scripts" / "install_dawnstrike_host_boundary.ps1").read_text(
        encoding="utf-8"
    )
    helper = HELPER.read_text(encoding="utf-8")
    receipt_tail = installer.split("$receiptParent = Split-Path -Parent $ReceiptRoot", 1)[1]
    preexisting_check = receipt_tail.index(
        "-Path $ReceiptRoot -Label 'Preexisting protected host receipt root'"
    )
    root_bind = receipt_tail.index(
        "-Path $ReceiptRoot -Label 'Protected host receipt root before ACL migration'"
    )
    root_acl = receipt_tail.index("Set-DawnstrikeProtectedDirectoryAcl -Path $ReceiptRoot")
    state_install = receipt_tail.index("Install-DawnstrikeStateRootBoundary")
    host_write = receipt_tail.index("-Payload $receipt -Path $receiptPath -NoReplace")
    assert preexisting_check < root_bind < root_acl < state_install < host_write
    assert "[IO.Directory]::CreateDirectory($ReceiptRoot, $receiptRootCreateAcl)" in receipt_tail
    assert "[IO.File]::WriteAllText($receiptPath" not in installer
    assert "MoveNoReplace" in helper
    assert "MOVEFILE_WRITE_THROUGH" in helper
    assert "[IO.FileMode]::CreateNew" in helper
    assert "$temporaryStream.Flush($true)" in helper
    assert "Global\\Dawnstrike.HostBoundary.Install.v1" in installer
    assert installer.index("$installMutex.WaitOne(0, $false)") < installer.index(
        "Copy-DawnstrikeExactGitFile -RelativePath $launcherRelative"
    )


def test_task_mutation_intent_and_request_admission_are_single_writer_and_held() -> None:
    helper = HELPER.read_text(encoding="utf-8")
    launcher = (ROOT / "scripts" / "dawnstrike_release_launcher.ps1").read_text(
        encoding="utf-8"
    )
    enter = helper.split("function Enter-DawnstrikeStateBoundaryTaskMutation {", 1)[1]
    fresh = enter.split("$null = Assert-DawnstrikeStateBoundaryTaskMutationIntent", 1)[0]
    assert "Find-DawnstrikeStateBoundaryExactTerminalEvidence" not in fresh
    assert "predecessor_terminal_evidence_pairs" in fresh
    assert "request_contract_sha256" in fresh
    assert "-Payload $payload -Path $intentPath -NoReplace" in fresh
    resume = enter.split("$null = Assert-DawnstrikeStateBoundaryTaskMutationIntent", 1)[1]
    assert resume.index("Find-DawnstrikeStateBoundaryExactTerminalEvidence") < resume.index(
        "Disable-DawnstrikeStateBoundaryAffectedTasks"
    )
    assert "-ExcludedEvidencePairs @($intent.payload.predecessor_terminal_evidence_pairs)" in resume
    admission = launcher.index("Get-DawnstrikeStateBoundaryTaskMutationReadAdmission")
    first_request_read = launcher.index("Get-DawnstrikeLauncherRequestFileContract `", admission)
    release_receipt = launcher.index("$requestAdmission.locks[0].Dispose()", first_request_read)
    enter_call = launcher.index("Enter-DawnstrikeStateBoundaryTaskMutation `", admission)
    dispatch = launcher.index("& $entryLocks[0].path", enter_call)
    release_remaining = launcher.index(
        "foreach ($requestAdmissionLock in @($requestAdmission.locks))", enter_call
    )
    assert (
        admission
        < first_request_read
        < release_receipt
        < enter_call
        < release_remaining
        < dispatch
    )
    assert (
        "$requestAdmission.locks = @($requestAdmission.locks | Select-Object -Skip 1)"
        in launcher
    )
    assert "$requestInputLocks += @($activationRequest.stream, $activationRequest.lease)" in launcher
    assert launcher.index("foreach ($requestInputLock in @($requestInputLocks))") > dispatch


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="requires Windows PowerShell 5.1")
def test_current_receipt_stream_is_the_only_lease_released_for_atomic_adoption(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    current = evidence / "state-boundary-current.json"
    replacement = evidence / "replacement.json"
    current.write_text("old", encoding="utf-8")
    replacement.write_text("new", encoding="utf-8")
    script = f"""
$ErrorActionPreference='Stop'
. '{_quote(HELPER)}'
$namespaceLease=Open-DawnstrikeStateBoundaryPath -Path '{_quote(evidence)}' -Label 'adoption namespace fixture'
$receiptStream=[IO.File]::Open('{_quote(current)}',[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
$blocked=$false
try {{[Dawnstrike.StateBoundary.AtomicFile]::Replace('{_quote(replacement)}','{_quote(current)}')}} catch {{$blocked=$true}}
finally {{$receiptStream.Dispose()}}
if(-not (Test-Path -LiteralPath '{_quote(replacement)}' -PathType Leaf)) {{
  [IO.File]::WriteAllText('{_quote(replacement)}','new',[Text.UTF8Encoding]::new($false))
}}
$namespaceHeld=$false
try {{
  [Dawnstrike.StateBoundary.AtomicFile]::Replace('{_quote(replacement)}','{_quote(current)}')
  $namespaceHeld=$null -ne $namespaceLease.Handle
}}
finally {{$namespaceLease.handle.Dispose()}}
[pscustomobject]@{{blocked=$blocked;current=[IO.File]::ReadAllText('{_quote(current)}');namespace_held=$namespaceHeld}}|ConvertTo-Json -Compress
"""
    result = _run_ps(script)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "blocked": True,
        "current": "new",
        "namespace_held": True,
    }


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="requires Windows PowerShell 5.1")
def test_state_boundary_rejects_a_reparse_component(tmp_path: Path) -> None:
    target = tmp_path / "target"
    link = tmp_path / "state-link"
    target.mkdir()
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlink creation is unavailable")
    script = f"""
$ErrorActionPreference='Stop'
. '{_quote(HELPER)}'
$blocked=$false
try {{$null=Assert-DawnstrikeStateBoundaryNoReparse -Path '{_quote(link)}' -Label hostile}} catch {{$blocked=$true}}
if(-not $blocked) {{throw 'reparse boundary was accepted'}}
'PASS'
"""
    result = _run_ps(script)
    assert result.returncode == 0, result.stderr
    assert "PASS" in result.stdout


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="requires Windows PowerShell 5.1")
def test_retained_boundary_chain_denies_ancestor_namespace_rename(tmp_path: Path) -> None:
    anchor = tmp_path / "rename-anchor"
    target = anchor / "dawnstrike-state" / "locks"
    target.mkdir(parents=True)
    moved = tmp_path / "renamed-anchor"
    script = f"""
$ErrorActionPreference='Stop'
. '{_quote(HELPER)}'
$lease=Open-DawnstrikeStateBoundaryPath -Path '{_quote(target)}' -Label 'ancestor rename fixture'
$blocked=$false
try {{Move-Item -LiteralPath '{_quote(anchor)}' -Destination '{_quote(moved)}' -ErrorAction Stop}} catch {{$blocked=$true}}
finally {{$lease.handle.Dispose()}}
if(-not $blocked) {{throw 'retained component chain allowed an ancestor rename'}}
'PASS'
"""
    result = _run_ps(script)
    assert result.returncode == 0, result.stderr
    assert "PASS" in result.stdout


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="requires Windows PowerShell 5.1")
def test_task_reseal_requires_exact_complete_mode_receipt_and_journal(tmp_path: Path) -> None:
    state = tmp_path / "state"
    receipt = state / "receipts" / "capture-task" / (
        "capture-task-hardening-" + "a" * 40 + ".json"
    )
    journal = state / "receipts" / "runtime-operation" / (
        "capture-task-hardening-" + "a" * 40 + ".json"
    )
    receipt.parent.mkdir(parents=True)
    journal.parent.mkdir(parents=True)
    script = f"""
$ErrorActionPreference='Stop'
. '{_quote(HELPER)}'
$sha='{'a' * 40}';$tree='{'b' * 40}'
$encoding=[Text.UTF8Encoding]::new($false)
$receiptPayload=[ordered]@{{schema_version='dawnstrike.capture_task_hardening_receipt.v2';status='COMPLETE';candidate_sha=$sha;candidate_tree=$tree;task_name='Dawnstrike Delayed SIP Capture';final_state='Disabled';xml_after_sha256=('c'*64);action_after_sha256=('d'*64);research_only=$true;broker_execution_enabled=$false}}
$receiptPayload['receipt_sha256']=Get-DawnstrikeStateBoundaryJsonSelfHash -Payload $receiptPayload -Field receipt_sha256 -TrailingNewline
[IO.File]::WriteAllText('{_quote(receipt)}',($receiptPayload|ConvertTo-Json -Compress),$encoding)
$receiptHash=Get-DawnstrikeStateBoundarySha256File '{_quote(receipt)}'
$journalPayload=[ordered]@{{schema_version='dawnstrike.runtime_operation_journal.v1';operation='capture_task_hardening';phase='COMPLETE';candidate_sha=$sha;candidate_tree=$tree;complete_receipt_relative_path=('receipts/capture-task/'+[IO.Path]::GetFileName('{_quote(receipt)}'));complete_receipt_sha256=$receiptHash;research_only=$true;broker_execution_enabled=$false}}
$journalPayload['journal_self_sha256']=Get-DawnstrikeStateBoundaryJsonSelfHash -Payload $journalPayload -Field journal_self_sha256
[IO.File]::WriteAllText('{_quote(journal)}',($journalPayload|ConvertTo-Json -Compress),$encoding)
$evidence=Get-DawnstrikeStateBoundaryTaskMutationTerminalEvidence -StateRoot '{_quote(state)}' -Mode HardenCapture -ExpectedSha $sha -ExpectedTree $tree -ReceiptPath '{_quote(receipt)}' -JournalPath '{_quote(journal)}'
$expectedReceiptHash=[string]$evidence.record.receipt_sha256
$expectedJournalHash=[string]$evidence.record.journal_sha256
foreach($lock in @($evidence.locks)){{$lock.Dispose()}}
[IO.File]::AppendAllText('{_quote(receipt)}',' ', $encoding)
$blocked=$false
try {{$null=Get-DawnstrikeStateBoundaryTaskMutationTerminalEvidence -StateRoot '{_quote(state)}' -Mode HardenCapture -ExpectedSha $sha -ExpectedTree $tree -ReceiptPath '{_quote(receipt)}' -JournalPath '{_quote(journal)}' -ExpectedReceiptSha256 $expectedReceiptHash -ExpectedJournalSha256 $expectedJournalHash}} catch {{$blocked=$true}}
if(-not $blocked){{throw 'changed terminal evidence was adopted'}}
'PASS'
"""
    result = _run_ps(script)
    assert result.returncode == 0, result.stderr
    assert "PASS" in result.stdout


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="requires Windows PowerShell 5.1")
def test_terminal_task_contract_rejects_post_mode_task_rewrite() -> None:
    script = f"""
$ErrorActionPreference='Stop'
. '{_quote(HELPER)}'
$contract='a'*64;$definition='b'*64;$action='c'*64
$tasks=@()
foreach($name in @(
  'Dawnstrike AlphaOps Morning','Dawnstrike AlphaOps Monitor 5m',
  'Dawnstrike AlphaOps EOD Full Report','Dawnstrike AlphaOps V6 Weekly Training',
  'Dawnstrike 10of10 Daily Finalize'
)){{
  $tasks += [pscustomobject]@{{task_name=$name;canonical=$true;state='Ready';canonical_task_count=5;canonical_task_contract_sha256=$contract;canonical_task_definition_contract_sha256=$definition;canonical_task_action_contract_sha256=$action}}
}}
$terminal=[pscustomobject]@{{task_count=5;task_contract_sha256=$contract;task_definition_contract_sha256=$definition;task_action_contract_sha256=$action}}
$pass=$false
try {{$null=Assert-DawnstrikeStateBoundaryTerminalTaskContract -Mode Activate -TerminalRecord $terminal -LiveTasks $tasks;$pass=$true}} catch {{}}
$tasks[2].canonical_task_action_contract_sha256='d'*64
$blocked=$false
try {{$null=Assert-DawnstrikeStateBoundaryTerminalTaskContract -Mode Activate -TerminalRecord $terminal -LiveTasks $tasks}} catch {{$blocked=$true}}
$capture=[pscustomobject]@{{task_name='Dawnstrike Delayed SIP Capture';canonical=$false;state='Disabled';definition_sha256=('e'*64);definition_contract_sha256=('f'*64);action_contract_sha256=('1'*64);action_section_sha256=('2'*64)}}
$hardening=[pscustomobject]@{{task_name='Dawnstrike Delayed SIP Capture';final_state='Disabled';xml_after_sha256=('e'*64);action_after_sha256=('2'*64)}}
$capturePass=$false
try {{$null=Assert-DawnstrikeStateBoundaryTerminalTaskContract -Mode HardenCapture -TerminalRecord $hardening -LiveTasks @($capture);$capturePass=$true}} catch {{}}
$capture.action_section_sha256='3'*64
$captureBlocked=$false
try {{$null=Assert-DawnstrikeStateBoundaryTerminalTaskContract -Mode HardenCapture -TerminalRecord $hardening -LiveTasks @($capture)}} catch {{$captureBlocked=$true}}
[pscustomobject]@{{pass=$pass;blocked=$blocked;capture_pass=$capturePass;capture_blocked=$captureBlocked}}|ConvertTo-Json -Compress
"""
    result = _run_ps(script)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "pass": True,
        "blocked": True,
        "capture_pass": True,
        "capture_blocked": True,
    }


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="requires Windows PowerShell 5.1")
def test_rollback_scope_and_terminal_contract_bind_optional_auxiliary() -> None:
    script = f"""
$ErrorActionPreference='Stop'
. '{_quote(HELPER)}'
$contract='a'*64;$definition='b'*64;$action='c'*64
$tasks=@()
foreach($name in @(
  'Dawnstrike AlphaOps Morning','Dawnstrike AlphaOps Monitor 5m',
  'Dawnstrike AlphaOps EOD Full Report','Dawnstrike AlphaOps V6 Weekly Training',
  'Dawnstrike 10of10 Daily Finalize'
)){{
  $tasks += [pscustomobject]@{{task_name=$name;canonical=$true;state='Ready';canonical_task_count=5;canonical_task_contract_sha256=$contract;canonical_task_definition_contract_sha256=$definition;canonical_task_action_contract_sha256=$action}}
}}
$capture=[pscustomobject]@{{task_name='Dawnstrike Delayed SIP Capture';canonical=$false;state='Ready';definition_sha256=('d'*64);definition_contract_sha256=('e'*64);action_contract_sha256=('f'*64)}}
$terminal=[pscustomobject]@{{task_count=5;task_contract_sha256=$contract;task_definition_contract_sha256=$definition;task_action_contract_sha256=$action;auxiliary_capture_present=$true;auxiliary_capture_disposition='RESTORED_EXACT_PRESENT';auxiliary_capture_action='RESTORED_EXACT';auxiliary_capture_state_after='Ready';auxiliary_capture_xml_sha256=('d'*64);auxiliary_capture_definition_contract_sha256=('e'*64);auxiliary_capture_action_contract_sha256=('f'*64)}}
$pass=$false
try {{$null=Assert-DawnstrikeStateBoundaryTerminalTaskContract -Mode Rollback -TerminalRecord $terminal -LiveTasks @($tasks+$capture);$pass=$true}} catch {{}}
$capture.definition_sha256='1'*64
$blocked=$false
try {{$null=Assert-DawnstrikeStateBoundaryTerminalTaskContract -Mode Rollback -TerminalRecord $terminal -LiveTasks @($tasks+$capture)}} catch {{$blocked=$true}}
$absent=[pscustomobject]@{{task_count=5;task_contract_sha256=$contract;task_definition_contract_sha256=$definition;task_action_contract_sha256=$action;auxiliary_capture_present=$false;auxiliary_capture_disposition='SCHEMA_V1_ABSENT_REQUIRES_NO_LIVE_AUXILIARY'}}
$absentPass=$false
try {{$null=Assert-DawnstrikeStateBoundaryTerminalTaskContract -Mode Rollback -TerminalRecord $absent -LiveTasks $tasks;$absentPass=$true}} catch {{}}
$affected=@(Get-DawnstrikeStateBoundaryTaskMutationAffectedNames -Mode Rollback)
[pscustomobject]@{{pass=$pass;blocked=$blocked;absent=$absentPass;count=$affected.Count;aux=($affected -contains 'Dawnstrike Delayed SIP Capture')}}|ConvertTo-Json -Compress
"""
    result = _run_ps(script)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "pass": True,
        "blocked": True,
        "absent": True,
        "count": 6,
        "aux": True,
    }


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="requires Windows PowerShell 5.1")
def test_resume_adopts_only_terminal_evidence_newer_than_protected_predecessor_set(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    receipt = state / "receipts" / "capture-task" / (
        "capture-task-rebind-" + "a" * 40 + ".json"
    )
    journal = state / "receipts" / "runtime-operation" / (
        "capture-task-rebind-" + "a" * 40 + ".json"
    )
    receipt.parent.mkdir(parents=True)
    journal.parent.mkdir(parents=True)
    script = f"""
$ErrorActionPreference='Stop'
. '{_quote(HELPER)}'
$sha='{'a' * 40}';$tree='{'b' * 40}';$encoding=[Text.UTF8Encoding]::new($false)
$live=@([pscustomobject]@{{task_name='Dawnstrike Delayed SIP Capture';canonical=$false;state='Ready';definition_sha256=('c'*64);definition_contract_sha256=('d'*64);action_contract_sha256=('e'*64);action_section_sha256=('f'*64)}})
function Write-Terminal([string]$stamp){{
  $r=[ordered]@{{schema_version='dawnstrike.capture_task_rebind_receipt.v2';status='COMPLETE';candidate_sha=$sha;candidate_tree=$tree;task_name='Dawnstrike Delayed SIP Capture';xml_after_sha256=('c'*64);action_after_sha256=('e'*64);definition_after_sha256=('d'*64);enablement_after='Ready';completed_at_utc=$stamp;research_only=$true;broker_execution_enabled=$false}}
  $r['receipt_sha256']=Get-DawnstrikeStateBoundaryJsonSelfHash -Payload $r -Field receipt_sha256 -TrailingNewline
  [IO.File]::WriteAllText('{_quote(receipt)}',($r|ConvertTo-Json -Compress),$encoding)
  $rh=Get-DawnstrikeStateBoundarySha256File '{_quote(receipt)}'
  $j=[ordered]@{{schema_version='dawnstrike.runtime_operation_journal.v1';operation='capture_task_rebind';phase='COMPLETE';candidate_sha=$sha;candidate_tree=$tree;complete_receipt_relative_path=('receipts/capture-task/'+[IO.Path]::GetFileName('{_quote(receipt)}'));complete_receipt_sha256=$rh;stamp=$stamp;research_only=$true;broker_execution_enabled=$false}}
  $j['journal_self_sha256']=Get-DawnstrikeStateBoundaryJsonSelfHash -Payload $j -Field journal_self_sha256
  [IO.File]::WriteAllText('{_quote(journal)}',($j|ConvertTo-Json -Compress),$encoding)
}}
Write-Terminal 'old'
$predecessor=@(Get-DawnstrikeStateBoundaryTerminalEvidencePairs -StateRoot '{_quote(state)}' -Mode RebindCapture -ExpectedSha $sha)
$stale=Find-DawnstrikeStateBoundaryExactTerminalEvidence -StateRoot '{_quote(state)}' -Mode RebindCapture -ExpectedSha $sha -ExpectedTree $tree -LiveTasks $live -ExcludedEvidencePairs $predecessor
Write-Terminal 'new'
$fresh=Find-DawnstrikeStateBoundaryExactTerminalEvidence -StateRoot '{_quote(state)}' -Mode RebindCapture -ExpectedSha $sha -ExpectedTree $tree -LiveTasks $live -ExcludedEvidencePairs $predecessor
[pscustomobject]@{{predecessor_count=$predecessor.Count;stale=($null -ne $stale);fresh=($null -ne $fresh)}}|ConvertTo-Json -Compress
"""
    result = _run_ps(script)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "predecessor_count": 1,
        "stale": False,
        "fresh": True,
    }


def test_windows_operations_timeout_covers_expanded_hostile_suites() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    windows = workflow.split("  windows-operations:", 1)[1]
    assert "timeout-minutes: 90" in windows.split("    steps:", 1)[0]
