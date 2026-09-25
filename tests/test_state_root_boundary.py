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
    assert installer.index("$materializedStateBoundaryBlob =") < (
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
    for marker in (
        "[ValidatePattern('^[0-9a-f]{64}$')][string]$RequestContractSha256",
        "[ValidatePattern('^[0-9a-f]{64}$')][string]$TerminalReceiptSha256",
        "[ValidatePattern('^[0-9a-f]{64}$')][string]$TerminalJournalSha256",
        "-RequestContractSha256 $RequestContractSha256",
        "-ExpectedReceiptSha256 $TerminalReceiptSha256.ToLowerInvariant()",
        "-ExpectedJournalSha256 $TerminalJournalSha256.ToLowerInvariant()",
    ):
        assert marker in completion
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
        "$materializedLauncherBlob ="
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
    assert "Find-DawnstrikeStateBoundaryExactTerminalEvidence" not in enter
    assert "Complete-DawnstrikeStateBoundaryExistingTerminal" not in helper
    # Writer-controlled terminal paths may be unreadable or reparsed. Every
    # changed affected task must be isolated before those paths are opened.
    assert resume.index("Disable-DawnstrikeStateBoundaryAffectedTasks") < resume.index(
        "Get-DawnstrikeStateBoundaryTerminalEvidencePairs"
    )
    assert "terminal_reconciliation_required = $terminalReconciliationRequired" in resume
    assert "if ($drift.Count -ne 0) {" in resume
    assert "-and -not $terminalReconciliationRequired" not in resume
    cancel = helper.split(
        "function Cancel-DawnstrikeStateBoundaryTaskMutationIfUnchanged {", 1
    )[1]
    assert cancel.index("Assert-DawnstrikeStateBoundaryTaskInventoryMatches") < cancel.index(
        "Get-DawnstrikeStateBoundaryTerminalEvidencePairs"
    )
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
def test_terminal_pair_change_is_diagnostic_not_adoption_authority(
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
$stalePairs=@(Get-DawnstrikeStateBoundaryTerminalEvidencePairs -StateRoot '{_quote(state)}' -Mode RebindCapture -ExpectedSha $sha)
$stale=@($stalePairs|Where-Object {{$_ -notin $predecessor}}).Count -gt 0
Write-Terminal 'new'
$freshPairs=@(Get-DawnstrikeStateBoundaryTerminalEvidencePairs -StateRoot '{_quote(state)}' -Mode RebindCapture -ExpectedSha $sha)
$fresh=@($freshPairs|Where-Object {{$_ -notin $predecessor}}).Count -gt 0
[pscustomobject]@{{predecessor_count=$predecessor.Count;stale=$stale;fresh=$fresh}}|ConvertTo-Json -Compress
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


def test_candidate_migration_is_distinct_crash_safe_and_preserves_bootstrap_authority() -> None:
    helper = HELPER.read_text(encoding="utf-8")
    migration = helper.split(
        "function Migrate-DawnstrikeStateRootBoundaryCandidate {", 1
    )[1].split("function Set-DawnstrikeStateBoundaryTasksDisabled {", 1)[0]
    transition = helper.split(
        "function Complete-DawnstrikeStateBoundaryCandidateMigrationFileTransition {", 1
    )[1].split("function Migrate-DawnstrikeStateRootBoundaryCandidate {", 1)[0]
    installation = helper.split("function Install-DawnstrikeStateRootBoundary {", 1)[1]
    assertion = helper.split("function Assert-DawnstrikeStateRootBoundary {", 1)[1]

    for marker in (
        "dawnstrike.state_boundary_candidate_migration_intent.v1",
        "dawnstrike.state_boundary_candidate_migration_completion.v1",
        "Open-DawnstrikeStateBoundaryRuntimeAuthorizationEvidence",
        "BOOTSTRAP_DISABLED",
        "ACTIVE_READY",
        "current_runtime_authorization_sha256",
        "rollbackAuthorization.status -ceq 'AUTHORIZED'",
        "activationLineage.status -ceq 'ACTIVE'",
        "Candidate migration target admission identity is invalid.",
    ):
        assert marker in migration or marker in helper
    assert "return Get-DawnstrikeStateBoundaryTaskMutationIntentPath" in helper
    assert "-Payload $intentPayload -Path $intentPath -NoReplace" in migration
    assert transition.index("-Payload $completionPayload") < transition.index(
        "-Payload $payload.new_current_receipt"
    )
    assert transition.index("-Path ([string]$payload.historical_receipt_path)") < (
        transition.index("-Payload $payload.new_current_receipt -Path $CurrentReceiptPath")
    )
    assert transition.index("new_current_receipt_sha256") < transition.index(
        "Remove-Item -LiteralPath ([string]$Intent.path)"
    )
    assert "explicit candidate migration; reinstall is denied" in installation
    assert "Assert-DawnstrikeStateBoundaryNoCandidateMigration" in assertion


def test_fail_closed_activation_completion_is_explicit_deep_and_crash_convergent() -> None:
    helper = HELPER.read_text(encoding="utf-8")
    adoption = helper.split(
        "function Complete-DawnstrikeStateBoundaryTaskMutationFailClosedAdoption {", 1
    )[1].split(
        "function Complete-DawnstrikeStateBoundaryTaskMutationFailClosed {", 1
    )[0]
    completion = helper.split(
        "function Complete-DawnstrikeStateBoundaryTaskMutationFailClosed {", 1
    )[1].split(
        "function Get-DawnstrikeStateBoundaryTaskMutationReadAdmission {", 1
    )[0]
    enter = helper.split("function Enter-DawnstrikeStateBoundaryTaskMutation {", 1)[1]

    for marker in (
        "dawnstrike.state_boundary_task_mutation_fail_closed.v1",
        "COMPENSATED_DISABLED",
        "Get-DawnstrikeStateBoundaryTaskMutationTerminalEvidence",
        "Open-DawnstrikeStateBoundaryRuntimeAuthorizationEvidence",
        "Assert-DawnstrikeStateBoundaryFailClosedTaskInventory",
        "source_activation_id",
        "source_terminal_receipt_sha256",
        "source_terminal_journal_sha256",
        "fail_closed_compensation_receipt_sha256",
        "fail_closed_compensation_journal_sha256",
        "DISABLED_BY_GOVERNED_ACTIVATION_CANCELLATION",
        "TEST_CRASH_AFTER_FAIL_CLOSED_COMPLETION",
        "TEST_CRASH_AFTER_FAIL_CLOSED_CURRENT",
    ):
        assert marker in adoption or marker in completion
    deep_proof = adoption.index(
        "Open-DawnstrikeStateBoundaryRuntimeAuthorizationEvidence"
    )
    live_proof = adoption.index("Assert-DawnstrikeStateBoundaryFailClosedTaskInventory")
    protected_completion = adoption.index(
        "-Payload $payload -Path $completionPath -NoReplace"
    )
    current_write = adoption.index("-Payload $newReceipt -Path $currentPath")
    intent_removal = adoption.index(
        "Remove-Item -LiteralPath ([string]$Intent.path)"
    )
    assert deep_proof < protected_completion
    assert live_proof < protected_completion < current_write < intent_removal
    assert "Complete-DawnstrikeStateBoundaryTaskMutationFailClosedAdoption" in enter


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="requires Windows PowerShell 5.1")
def test_fail_closed_task_inventory_accepts_only_disabled_exact_actions() -> None:
    script = f"""
$ErrorActionPreference='Stop'
. '{_quote(HELPER)}'
$sid='S-1-5-21-1-2-3-1001';$def='a'*64;$action='b'*64;$section='c'*64
$expected=@();$live=@()
foreach($name in @(
  'Dawnstrike AlphaOps Morning','Dawnstrike AlphaOps Monitor 5m',
  'Dawnstrike AlphaOps EOD Full Report','Dawnstrike AlphaOps V6 Weekly Training',
  'Dawnstrike 10of10 Daily Finalize'
)){{
  $expected += [pscustomobject]@{{task_name=$name;task_path='\';state='Ready';principal_sid=$sid;logon_type='Password';run_level='Limited';definition_sha256=('d'*64);definition_contract_sha256=$def;action_contract_sha256=$action;action_section_sha256=$section;canonical=$true;canonical_task_definition_contract_sha256=$def;canonical_task_action_contract_sha256=$action}}
  $live += [pscustomobject]@{{task_name=$name;task_path='\';state='Disabled';principal_sid=$sid;logon_type='Password';run_level='Limited';definition_sha256=('e'*64);definition_contract_sha256=$def;action_contract_sha256=$action;action_section_sha256=$section;canonical=$true;canonical_task_definition_contract_sha256=$def;canonical_task_action_contract_sha256=$action}}
}}
$accepted=$false
try {{$null=Assert-DawnstrikeStateBoundaryFailClosedTaskInventory -ExpectedTasks $expected -LiveTasks $live -WriterSids @($sid);$accepted=$true}} catch {{}}
$live[0].state='Ready';$readyRejected=$false
try {{$null=Assert-DawnstrikeStateBoundaryFailClosedTaskInventory -ExpectedTasks $expected -LiveTasks $live -WriterSids @($sid)}} catch {{$readyRejected=$true}}
$live[0].state='Disabled';$live[0].action_contract_sha256='f'*64;$actionRejected=$false
try {{$null=Assert-DawnstrikeStateBoundaryFailClosedTaskInventory -ExpectedTasks $expected -LiveTasks $live -WriterSids @($sid)}} catch {{$actionRejected=$true}}
[pscustomobject]@{{accepted=$accepted;ready_rejected=$readyRejected;action_rejected=$actionRejected}}|ConvertTo-Json -Compress
"""
    result = _run_ps(script)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "accepted": True,
        "ready_rejected": True,
        "action_rejected": True,
    }


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="requires Windows PowerShell 5.1")
def test_fail_closed_completion_recovers_both_hard_kill_boundaries(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    evidence = tmp_path / "evidence"
    state.mkdir()
    evidence.mkdir()
    pending = evidence / "state-boundary-task-mutation-pending.json"
    pending.write_text("pending", encoding="utf-8")
    completion = evidence / ("state-boundary-task-mutation-completion-" + "1" * 32 + ".json")
    current = evidence / "state-boundary-current.json"
    compensation = state / "receipts" / "runtime-activation" / "compensated.json"
    compensation.parent.mkdir(parents=True)
    recovery_journal = (
        state
        / "receipts"
        / "runtime-operation"
        / ("terminal-recovery-" + "1" * 32 + ".json")
    )
    recovery_journal.parent.mkdir(parents=True)
    script = rf"""
$ErrorActionPreference='Stop'
. '{_quote(HELPER)}'
$candidate='a'*40;$candidateTree='b'*40;$previous='c'*40;$previousTree='d'*40
$operation='1'*32;$request='2'*64;$sourceId='3'*24;$sid='S-1-5-21-1-2-3-1001'
$taskContract='4'*64;$definition='5'*64;$action='6'*64;$section='7'*64
$expected=@();$live=@()
foreach($name in @(
  'Dawnstrike AlphaOps Morning','Dawnstrike AlphaOps Monitor 5m',
  'Dawnstrike AlphaOps EOD Full Report','Dawnstrike AlphaOps V6 Weekly Training',
  'Dawnstrike 10of10 Daily Finalize'
)){{
  $expected += [pscustomobject]@{{task_name=$name;task_path='\';state='Ready';principal_sid=$sid;logon_type='Password';run_level='Limited';definition_sha256=('8'*64);definition_contract_sha256=$definition;action_contract_sha256=$action;action_section_sha256=$section;canonical=$true;canonical_task_contract_sha256=$taskContract;canonical_task_definition_contract_sha256=$definition;canonical_task_action_contract_sha256=$action}}
  $live += [pscustomobject]@{{task_name=$name;task_path='\';state='Disabled';principal_sid=$sid;logon_type='Password';run_level='Limited';definition_sha256=('9'*64);definition_contract_sha256=$definition;action_contract_sha256=$action;action_section_sha256=$section;canonical=$true;canonical_task_contract_sha256=$taskContract;canonical_task_definition_contract_sha256=$definition;canonical_task_action_contract_sha256=$action}}
}}
$old=[pscustomobject][ordered]@{{
 schema_version='dawnstrike.state_boundary_installation.v2';status='PASS';operation_id=('0'*32);installed_at_utc='2026-09-03T00:00:00Z';candidate_sha=$candidate;candidate_tree=$candidateTree;state_root='{_quote(state)}';state_root_identity='state';state_root_sddl='sddl';state_root_sddl_sha256=('a'*64);locks_root='locks';locks_root_identity='locks-id';locks_root_sddl='locks-sddl';locks_root_sddl_sha256=('b'*64);state_entry_count=1;state_identity_contract_sha256=('c'*64);rollback_manifest_path='rollback';rollback_manifest_sha256=('d'*64);installed_helper_path='helper';installed_helper_sha256=('e'*64);writer_sids=@($sid);task_definitions_and_principals=@($expected);task_binding_sha256='';research_only=$true;broker_execution_enabled=$false
}}
$old.task_binding_sha256=Get-DawnstrikeStateBoundaryTaskBindingHash $expected
$script:currentReceipt=$old;$script:currentHash='f'*64
$script:authorization=[pscustomobject]@{{status='AUTHORIZED';sha256=('a'*64);operation_type='BOOTSTRAP';runtime_sha=$previous;runtime_tree=$previousTree;contract=[pscustomobject]@{{}};material=[pscustomobject]@{{canonical_task_definition_contract_sha256=$definition;canonical_task_action_contract_sha256=$action}}}}
$script:noneAuthorization=[pscustomobject]@{{status='NONE';sha256='NONE';operation_type='';runtime_sha='';runtime_tree='';contract=$null;material=$null}}
$script:intent=[pscustomobject]@{{path='{_quote(pending)}';sha256=('b'*64);payload=[pscustomobject][ordered]@{{schema_version='dawnstrike.state_boundary_task_mutation.v1';operation_id=$operation;mode='Activate';expected_sha=$candidate;expected_tree=$candidateTree;candidate_sha=$candidate;candidate_tree=$candidateTree;state_root='{_quote(state)}';request_contract_sha256=$request;old_current_receipt_sha256=$script:currentHash;old_task_binding_sha256=$old.task_binding_sha256;predecessor_terminal_evidence_sha256=('c'*64);task_definitions_and_principals=@($expected);writer_sids=@($sid);completion_path='{_quote(completion)}'}}}}
$comp=[ordered]@{{schema_version='dawnstrike.runtime_compensation_receipt.v2';status='COMPENSATED';operation='runtime_activation';candidate_sha=$candidate;candidate_tree=$candidateTree;prior_journal_file_sha256=('d'*64);task_contract_sha256=$taskContract;task_state='Disabled';task_action_contract_sha256=$action;task_definition_contract_sha256=$definition}}
$journal=[ordered]@{{schema_version='dawnstrike.runtime_operation_journal.v2';operation='runtime_activation';phase='COMPENSATED';candidate_sha=$candidate;candidate_tree=$candidateTree;current_sha=$previous;current_tree=$previousTree;previous_sha=$previous;previous_tree=$previousTree;task_contract_sha256=$taskContract;prior_journal_file_sha256=('d'*64);compensation_receipt_relative_path='receipts/runtime-activation/compensated.json';compensation_receipt_sha256=('e'*64)}}
function New-Disposable {{[IO.MemoryStream]::new()}}
function Get-DawnstrikeStateBoundaryTaskMutationIntent {{param($EvidenceRoot);if(Test-Path -LiteralPath '{_quote(pending)}'){{$script:intent}}else{{$null}}}}
function Assert-DawnstrikeStateBoundaryTaskMutationIntent {{param($Intent,$StateRoot,$Mode,$ExpectedSha,$ExpectedTree,$RequestContractSha256);$true}}
function Get-DawnstrikeStateBoundaryTaskInventory {{@($script:live)}}
function Get-DawnstrikeStateBoundaryRuntimeAuthorization {{param($Receipt,$Kind,$StateRoot);if($Kind -eq 'current'){{$script:authorization}}else{{$script:noneAuthorization}}}}
function Get-DawnstrikeStateBoundaryActivationLineage {{[pscustomobject]@{{status='NONE';activation_id='NONE';receipt_relative_path='NONE';receipt_sha256='NONE';journal_relative_path='NONE';journal_sha256='NONE'}}}}
function Open-DawnstrikeStateBoundaryRuntimeAuthorizationEvidence {{[pscustomobject]@{{locks=@(New-Disposable)}}}}
function Get-DawnstrikeStateBoundaryTaskMutationTerminalEvidence {{[pscustomobject]@{{record=[pscustomobject]@{{activation_id=$sourceId;previous_sha=$previous;previous_tree=$previousTree}};locks=@(New-Disposable)}}}}
function Open-DawnstrikeStateBoundaryExactFile {{
  param($Path,$ExpectedSha256,$Label)
  $value=if($Label -like '*journal*'){{$journal}}else{{$comp}}
  [pscustomobject]@{{bytes=[Text.UTF8Encoding]::new($false).GetBytes(($value|ConvertTo-Json -Compress));stream=(New-Disposable);lease=(New-Disposable)}}
}}
function Assert-DawnstrikeStateRootBoundary {{
  param($StateRoot,$EvidenceRoot,$AllowedTaskMutationOperationId,[switch]$AllowTaskDefinitionDrift)
  [pscustomobject]@{{receipt=$script:currentReceipt;receipt_sha256=$script:currentHash;writer_sids=@($sid);locks=@((New-Disposable),(New-Disposable),(New-Disposable))}}
}}
function Write-DawnstrikeStateBoundaryProtectedJson {{
  param($Payload,[string]$Path,[switch]$NoReplace)
  $bytes=[Text.UTF8Encoding]::new($false).GetBytes(($Payload|ConvertTo-Json -Depth 20)+"`r`n")
  $hash=Get-DawnstrikeStateBoundarySha256Bytes $bytes
  if($NoReplace -and (Test-Path -LiteralPath $Path)){{
    if((Get-DawnstrikeStateBoundarySha256Bytes ([IO.File]::ReadAllBytes($Path))) -cne $hash){{throw 'fixture no-replace mismatch'}}
  }}else{{[IO.File]::WriteAllBytes($Path,$bytes)}}
  if([IO.Path]::GetFullPath($Path) -eq [IO.Path]::GetFullPath('{_quote(current)}')){{$script:currentReceipt=[pscustomobject]$Payload;$script:currentHash=$hash}}
  [pscustomobject]@{{path=$Path;sha256=$hash}}
}}
$args=@{{StateRoot='{_quote(state)}';EvidenceRoot='{_quote(evidence)}';ExpectedSha=$candidate;ExpectedTree=$candidateTree;OperationId=$operation;RequestContractSha256=$request;CompensationReceiptPath='{_quote(compensation)}';CompensationReceiptSha256=('e'*64);CompensationJournalPath='{_quote(recovery_journal)}';CompensationJournalSha256=('f'*64);SourceActivationId=$sourceId;SourceTerminalReceiptSha256=('1'*64);SourceTerminalJournalSha256=('2'*64)}}
$firstCrash=$false
try {{$null=Complete-DawnstrikeStateBoundaryTaskMutationFailClosed @args -TestCrashPoint after_completion}} catch {{$firstCrash=$_.Exception.Message -like '*AFTER_FAIL_CLOSED_COMPLETION*'}}
$firstOld=($script:currentHash -ceq ('f'*64));$completionHeld=(Test-Path -LiteralPath '{_quote(completion)}');$pendingHeld=(Test-Path -LiteralPath '{_quote(pending)}')
$secondCrash=$false
try {{$null=Complete-DawnstrikeStateBoundaryTaskMutationFailClosed @args -TestCrashPoint after_current}} catch {{$secondCrash=$_.Exception.Message -like '*AFTER_FAIL_CLOSED_CURRENT*'}}
$secondNew=($script:currentHash -cne ('f'*64));$pendingAfterCurrent=(Test-Path -LiteralPath '{_quote(pending)}')
$bytes=[IO.File]::ReadAllBytes('{_quote(completion)}')
$completionObject=[pscustomobject]@{{path='{_quote(completion)}';payload=([Text.Encoding]::UTF8.GetString($bytes)|ConvertFrom-Json);sha256=(Get-DawnstrikeStateBoundarySha256Bytes $bytes)}}
$final=Complete-DawnstrikeStateBoundaryTaskMutationFailClosedAdoption -Intent $script:intent -Completion $completionObject -StateRoot '{_quote(state)}' -EvidenceRoot '{_quote(evidence)}'
foreach($lock in @($final.locks)){{if($null-ne$lock){{$lock.Dispose()}}}}
[pscustomobject]@{{first_crash=$firstCrash;first_old=$firstOld;completion_held=$completionHeld;pending_held=$pendingHeld;second_crash=$secondCrash;second_new=$secondNew;pending_after_current=$pendingAfterCurrent;final_status=([string]$final.status);pending_removed=(-not(Test-Path -LiteralPath '{_quote(pending)}'))}}|ConvertTo-Json -Compress
"""
    result = _run_ps(script)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "first_crash": True,
        "first_old": True,
        "completion_held": True,
        "pending_held": True,
        "second_crash": True,
        "second_new": True,
        "pending_after_current": True,
        "final_status": "COMPENSATED_DISABLED",
        "pending_removed": True,
    }


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="requires Windows PowerShell 5.1")
def test_candidate_migration_file_transition_recovers_both_hard_kill_boundaries(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    current = evidence / "state-boundary-current.json"
    historical = evidence / ("state-boundary-" + "b" * 40 + ".json")
    pending = evidence / "state-boundary-task-mutation-pending.json"
    completion = evidence / ("state-boundary-candidate-migration-" + "c" * 32 + ".json")
    script = rf"""
$ErrorActionPreference='Stop'
. '{_quote(HELPER)}'
function Write-DawnstrikeStateBoundaryProtectedJson {{
  param($Payload,[string]$Path,[switch]$NoReplace)
  $bytes=[Text.UTF8Encoding]::new($false).GetBytes(($Payload|ConvertTo-Json -Depth 20)+"`r`n")
  $hash=Get-DawnstrikeStateBoundarySha256Bytes $bytes
  if($NoReplace -and (Test-Path -LiteralPath $Path -PathType Leaf)) {{
    $old=[IO.File]::ReadAllBytes($Path)
    if((Get-DawnstrikeStateBoundarySha256Bytes $old) -cne $hash) {{throw 'fixture no-replace mismatch'}}
  }} else {{[IO.File]::WriteAllBytes($Path,$bytes)}}
  [pscustomobject]@{{path=$Path;sha256=$hash}}
}}
function Read-DawnstrikeStateBoundaryProtectedJson {{
  param([string]$Path)
  $bytes=[IO.File]::ReadAllBytes($Path)
  [pscustomobject]@{{payload=([Text.Encoding]::UTF8.GetString($bytes)|ConvertFrom-Json);sha256=(Get-DawnstrikeStateBoundarySha256Bytes $bytes);stream=[IO.MemoryStream]::new($bytes)}}
}}
$old=[ordered]@{{candidate_sha=('a'*40);current_runtime_authorization_sha256=('1'*64)}}
$new=[ordered]@{{candidate_sha=('b'*40);current_runtime_authorization_sha256=('1'*64);rollback_runtime_authorization_sha256='NONE';last_activation_id='NONE';candidate_migration_runtime_sha=('a'*40);candidate_migration_runtime_tree=('2'*40);candidate_migration_runtime_helper_path='C:\Program Files\Dawnstrike\releases\'+('a'*40)+'\scripts\state_root_boundary.ps1';candidate_migration_runtime_helper_sha256=('7'*64)}}
$oldWrite=Write-DawnstrikeStateBoundaryProtectedJson -Payload $old -Path '{_quote(current)}'
$newHash=Get-DawnstrikeStateBoundarySha256Text (($new|ConvertTo-Json -Depth 20)+"`r`n")
$intentPayload=[ordered]@{{
 schema_version='dawnstrike.state_boundary_candidate_migration_intent.v1';operation_id=('c'*32);created_at_utc='2026-09-03T00:00:00Z';state_root='C:\r\dawnstrike-state';from_candidate_sha=('a'*40);from_candidate_tree=('2'*40);runtime_sha=('a'*40);runtime_tree=('2'*40);candidate_sha=('b'*40);candidate_tree=('3'*40);request_contract_sha256=('4'*64);old_current_receipt_sha256=$oldWrite.sha256;authorization_state='BOOTSTRAP_DISABLED';current_runtime_authorization_sha256=('1'*64);rollback_runtime_authorization_sha256='NONE';activation_lineage_id='NONE';predecessor_helper_path='C:\Program Files\Dawnstrike\releases\'+('a'*40)+'\scripts\state_root_boundary.ps1';predecessor_helper_sha256=('7'*64);runtime_helper_path='C:\Program Files\Dawnstrike\releases\'+('a'*40)+'\scripts\state_root_boundary.ps1';runtime_helper_sha256=('7'*64);installed_helper_path='C:\Program Files\Dawnstrike\releases\'+('b'*40)+'\scripts\state_root_boundary.ps1';installed_helper_sha256=('5'*64);candidate_admission_path='C:\Program Files\Dawnstrike\releases\'+('b'*40)+'\.git\dawnstrike-host-admission-v1.json';candidate_admission_sha256=('6'*64);new_current_receipt_sha256=$newHash;new_current_receipt=$new;completion_path='{_quote(completion)}';historical_receipt_path='{_quote(historical)}';research_only=$true;broker_execution_enabled=$false
}}
$intentWrite=Write-DawnstrikeStateBoundaryProtectedJson -Payload $intentPayload -Path '{_quote(pending)}'
$intent=[pscustomobject]@{{path='{_quote(pending)}';payload=[pscustomobject]$intentPayload;sha256=$intentWrite.sha256}}
$firstCrash=$false
try {{$null=Complete-DawnstrikeStateBoundaryCandidateMigrationFileTransition -Intent $intent -EvidenceRoot '{_quote(evidence)}' -CurrentReceiptPath '{_quote(current)}' -TestCrashPoint after_completion}} catch {{$firstCrash=$_.Exception.Message -like '*after_completion*'}}
$afterFirst=(Read-DawnstrikeStateBoundaryProtectedJson '{_quote(current)}').sha256
$secondCrash=$false
try {{$null=Complete-DawnstrikeStateBoundaryCandidateMigrationFileTransition -Intent $intent -EvidenceRoot '{_quote(evidence)}' -CurrentReceiptPath '{_quote(current)}' -TestCrashPoint after_current}} catch {{$secondCrash=$_.Exception.Message -like '*after_current*'}}
$afterSecond=(Read-DawnstrikeStateBoundaryProtectedJson '{_quote(current)}').sha256
$final=Complete-DawnstrikeStateBoundaryCandidateMigrationFileTransition -Intent $intent -EvidenceRoot '{_quote(evidence)}' -CurrentReceiptPath '{_quote(current)}'
$currentRead=Read-DawnstrikeStateBoundaryProtectedJson '{_quote(current)}'
$historicalRead=Read-DawnstrikeStateBoundaryProtectedJson '{_quote(historical)}'
[pscustomobject]@{{first_crash=$firstCrash;first_old=($afterFirst -ceq $oldWrite.sha256);second_crash=$secondCrash;second_new=($afterSecond -ceq $newHash);current_new=($currentRead.sha256 -ceq $newHash);historical_new=($historicalRead.sha256 -ceq $newHash);authorization_preserved=([string]$currentRead.payload.current_runtime_authorization_sha256 -ceq ('1'*64));pending_removed=(-not (Test-Path -LiteralPath '{_quote(pending)}'));completion_preserved=(Test-Path -LiteralPath '{_quote(completion)}')}}|ConvertTo-Json -Compress
"""
    result = _run_ps(script)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "first_crash": True,
        "first_old": True,
        "second_crash": True,
        "second_new": True,
        "current_new": True,
        "historical_new": True,
        "authorization_preserved": True,
        "pending_removed": True,
        "completion_preserved": True,
    }


@pytest.mark.skipif(not POWERSHELL.is_file(), reason="requires Windows PowerShell 5.1")
def test_candidate_migration_intent_rejects_remapped_retry(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    state = Path(r"C:\r\dawnstrike-state")
    sha_a, tree_a, sha_b, tree_b = "a" * 40, "1" * 40, "b" * 40, "2" * 40
    runtime_sha, runtime_tree = "d" * 40, "e" * 40
    request = "3" * 64
    helper = Path(rf"C:\Program Files\Dawnstrike\releases\{sha_b}\scripts\state_root_boundary.ps1")
    admission = Path(
        rf"C:\Program Files\Dawnstrike\releases\{sha_b}\.git\dawnstrike-host-admission-v1.json"
    )
    script = rf"""
$ErrorActionPreference='Stop'
. '{_quote(HELPER)}'
$new=[ordered]@{{candidate_sha='{sha_b}';candidate_tree='{tree_b}';current_runtime_authorization_sha256=('4'*64);rollback_runtime_authorization_contract='NONE';rollback_runtime_authorization_sha256='NONE';last_activation_id='NONE';candidate_migration_operation_id=('5'*32);candidate_migration_request_contract_sha256='{request}';candidate_migration_predecessor_receipt_sha256=('6'*64);candidate_migration_predecessor_helper_path=('C:\Program Files\Dawnstrike\releases\{sha_a}\scripts\state_root_boundary.ps1');candidate_migration_predecessor_helper_sha256=('a'*64);candidate_migration_authorization_state='BOOTSTRAP_DISABLED';candidate_migration_runtime_sha='{runtime_sha}';candidate_migration_runtime_tree='{runtime_tree}';candidate_migration_runtime_helper_path=('C:\Program Files\Dawnstrike\releases\{runtime_sha}\scripts\state_root_boundary.ps1');candidate_migration_runtime_helper_sha256=('d'*64)}}
$newHash=Get-DawnstrikeStateBoundarySha256Text (($new|ConvertTo-Json -Depth 20)+"`r`n")
$payload=[ordered]@{{schema_version='dawnstrike.state_boundary_candidate_migration_intent.v1';operation_id=('5'*32);created_at_utc='2026-09-03T00:00:00Z';state_root='{_quote(state)}';from_candidate_sha='{sha_a}';from_candidate_tree='{tree_a}';runtime_sha='{runtime_sha}';runtime_tree='{runtime_tree}';candidate_sha='{sha_b}';candidate_tree='{tree_b}';request_contract_sha256='{request}';old_current_receipt_sha256=('6'*64);authorization_state='BOOTSTRAP_DISABLED';current_runtime_authorization_sha256=('4'*64);rollback_runtime_authorization_sha256='NONE';activation_lineage_id='NONE';predecessor_helper_path=('C:\Program Files\Dawnstrike\releases\{sha_a}\scripts\state_root_boundary.ps1');predecessor_helper_sha256=('a'*64);runtime_helper_path=('C:\Program Files\Dawnstrike\releases\{runtime_sha}\scripts\state_root_boundary.ps1');runtime_helper_sha256=('d'*64);installed_helper_path='{_quote(helper)}';installed_helper_sha256=('7'*64);candidate_admission_path='{_quote(admission)}';candidate_admission_sha256=('8'*64);new_current_receipt_sha256=$newHash;new_current_receipt=$new;completion_path=(Join-Path '{_quote(evidence)}' ('state-boundary-candidate-migration-'+('5'*32)+'.json'));historical_receipt_path=(Join-Path '{_quote(evidence)}' ('state-boundary-{sha_b}.json'));research_only=$true;broker_execution_enabled=$false}}
$intent=[pscustomobject]@{{payload=[pscustomobject]$payload}}
$valid=$false
try {{$null=Assert-DawnstrikeStateBoundaryCandidateMigrationIntent -Intent $intent -StateRoot '{_quote(state)}' -EvidenceRoot '{_quote(evidence)}' -ExpectedBoundarySha '{sha_a}' -ExpectedBoundaryTree '{tree_a}' -ExpectedRuntimeSha '{runtime_sha}' -ExpectedRuntimeTree '{runtime_tree}' -CandidateSha '{sha_b}' -CandidateTree '{tree_b}' -RequestContractSha256 '{request}' -InstalledHelperPath '{_quote(helper)}' -InstalledHelperSha256 ('7'*64) -CandidateAdmissionPath '{_quote(admission)}' -CandidateAdmissionSha256 ('8'*64);$valid=$true}} catch {{}}
$intent.payload.request_contract_sha256='9'*64
$remapBlocked=$false
try {{$null=Assert-DawnstrikeStateBoundaryCandidateMigrationIntent -Intent $intent -StateRoot '{_quote(state)}' -EvidenceRoot '{_quote(evidence)}' -ExpectedBoundarySha '{sha_a}' -ExpectedBoundaryTree '{tree_a}' -ExpectedRuntimeSha '{runtime_sha}' -ExpectedRuntimeTree '{runtime_tree}' -CandidateSha '{sha_b}' -CandidateTree '{tree_b}' -RequestContractSha256 '{request}' -InstalledHelperPath '{_quote(helper)}' -InstalledHelperSha256 ('7'*64) -CandidateAdmissionPath '{_quote(admission)}' -CandidateAdmissionSha256 ('8'*64)}} catch {{$remapBlocked=$true}}
$intent.payload.request_contract_sha256='{request}'
$intent.payload.authorization_state='ACTIVE_READY'
$intent.payload.rollback_runtime_authorization_sha256='b'*64
$intent.payload.activation_lineage_id='c'*24
$intent.payload.new_current_receipt.candidate_migration_authorization_state='ACTIVE_READY'
$intent.payload.new_current_receipt.rollback_runtime_authorization_sha256='b'*64
$intent.payload.new_current_receipt.last_activation_id='c'*24
$intent.payload.new_current_receipt_sha256=Get-DawnstrikeStateBoundarySha256Text (($intent.payload.new_current_receipt|ConvertTo-Json -Depth 20)+"`r`n")
$activeValid=$false
try {{$null=Assert-DawnstrikeStateBoundaryCandidateMigrationIntent -Intent $intent -StateRoot '{_quote(state)}' -EvidenceRoot '{_quote(evidence)}' -ExpectedBoundarySha '{sha_a}' -ExpectedBoundaryTree '{tree_a}' -ExpectedRuntimeSha '{runtime_sha}' -ExpectedRuntimeTree '{runtime_tree}' -CandidateSha '{sha_b}' -CandidateTree '{tree_b}' -RequestContractSha256 '{request}' -InstalledHelperPath '{_quote(helper)}' -InstalledHelperSha256 ('7'*64) -CandidateAdmissionPath '{_quote(admission)}' -CandidateAdmissionSha256 ('8'*64);$activeValid=$true}} catch {{}}
[pscustomobject]@{{valid=$valid;remap_blocked=$remapBlocked;active_valid=$activeValid}}|ConvertTo-Json -Compress
"""
    result = _run_ps(script)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "valid": True,
        "remap_blocked": True,
        "active_valid": True,
    }
