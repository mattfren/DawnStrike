[CmdletBinding()]
param(
    [string]$RuntimeRoot = "C:\r\dawnstrike-runtime",
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateSha = "",
    [Parameter(Mandatory = $true)][string]$SymbolsManifest,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$SymbolsManifestSha256,
    [Parameter(Mandatory = $true)][string]$EntitlementReceipt,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$EntitlementReceiptSha256,
    [Parameter(Mandatory = $true)][string]$SourceConfig,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$SourceConfigSha256,
    [pscredential]$RunAsCredential,
    [string]$ReceiptPath = "",
    [switch]$Enable,
    [switch]$InjectFailureAfterMutation,
    [Alias("InjectHardCrashAfterEnable")][switch]$InjectCrashAfterEnable,
    [ValidateRange(30, 1800)][int]$ProcessTimeoutSeconds = 300
)

$ErrorActionPreference = "Stop"
if (
    [int]$PSVersionTable.PSVersion.Major -lt 5 -or
    [string]$PSVersionTable.PSEdition -ne "Desktop"
) {
    throw "Dawnstrike capture rebind requires Windows PowerShell 5.1 or later (Desktop edition)."
}
$captureRebindRuntimeRoot = $RuntimeRoot
$captureRebindStateRoot = $StateRoot
$captureRebindTimeout = $ProcessTimeoutSeconds
$captureRebindRunAsCredential = $RunAsCredential
. (Join-Path $PSScriptRoot "resolve_dawnstrike_task_principal.ps1")
. (Join-Path $PSScriptRoot "activate_dawnstrike_runtime.ps1")
. (Join-Path $PSScriptRoot "dawnstrike_job_process.ps1")
. (Join-Path $PSScriptRoot "invoke_dawnstrike_stage.ps1")
$RuntimeRoot = $captureRebindRuntimeRoot
$StateRoot = $captureRebindStateRoot
$ProcessTimeoutSeconds = $captureRebindTimeout
$RunAsCredential = $captureRebindRunAsCredential
if ($null -eq $RunAsCredential -or [string]::IsNullOrWhiteSpace($RunAsCredential.UserName)) {
    throw "Rebind requires the locally prompted RunAsCredential for the Password auxiliary task."
}
$rebindPassword = $RunAsCredential.GetNetworkCredential().Password
if ([string]::IsNullOrWhiteSpace($rebindPassword)) { throw "Rebind credential is incomplete." }
function Get-DawnstrikePrincipalSid([string]$Value) {
    if ($Value -match '^S-\d-\d+') { return $Value.ToUpperInvariant() }
    try {
        return ([System.Security.Principal.NTAccount]::new($Value)).Translate(
            [System.Security.Principal.SecurityIdentifier]
        ).Value.ToUpperInvariant()
    }
    catch { throw "Unable to canonicalize the hardened auxiliary principal SID." }
}

function Get-DawnstrikeAuxiliarySectionHash {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Xml, [Parameter(Mandatory = $true)][string]$Name)
    try {
        $document = [System.Xml.XmlDocument]::new()
        $document.PreserveWhitespace = $true
        $document.LoadXml($Xml)
        $nodes = @($document.SelectNodes("//*[local-name()='$Name']"))
        if ($nodes.Count -ne 1) { throw "expected one $Name section" }
        return Get-DawnstrikeSha256Text ([string]$nodes[0].OuterXml)
    }
    catch { throw "Auxiliary task XML has an invalid $Name section." }
}

function Get-DawnstrikeHardeningSectionHash {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Xml,
        [Parameter(Mandatory = $true)][ValidateSet("Principal", "Triggers", "Settings", "Actions")][string]$Name,
        [string]$NormalizeEnabledTo = ""
    )
    try {
        $document = [System.Xml.XmlDocument]::new()
        $document.PreserveWhitespace = $true
        $document.LoadXml($Xml)
        $nodes = @($document.SelectNodes("//*[local-name()='$Name']"))
        if ($nodes.Count -ne 1) { throw "expected one $Name section" }
        if ($Name -eq "Settings" -and $NormalizeEnabledTo) {
            $enabled = @($nodes[0].ChildNodes | Where-Object { $_.LocalName -eq "Enabled" })
            if ($enabled.Count -gt 1) { throw "duplicate Enabled setting" }
            if ($enabled.Count -eq 1) { $enabled[0].InnerText = $NormalizeEnabledTo }
        }
        return Get-DawnstrikeSha256Text ([string]$nodes[0].OuterXml)
    }
    catch { throw "Auxiliary task hardening section is invalid." }
}

function Get-DawnstrikeHardeningReceipt {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$ContractPath,
        [Parameter(Mandatory = $true)][string]$CandidateSha,
        [Parameter(Mandatory = $true)][string]$CandidateTree,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )
    $root = Join-Path $StateRoot "receipts\capture-task"
    Assert-DawnstrikeNoReparseComponents $root "Capture-task hardening receipt root"
    $path = Join-Path $root ("capture-task-hardening-" + $CandidateSha + ".json")
    Assert-DawnstrikeNoReparseComponents $path "Capture-task hardening receipt"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "The exact candidate-named delayed-SIP hardening receipt is missing."
    }
    $result = Invoke-DawnstrikeActivationProcess $PythonPath @(
        $ContractPath, "verify-hardening", "--receipt", $path,
        "--candidate-sha", $CandidateSha, "--candidate-tree", $CandidateTree
    ) $PSScriptRoot "Capture-task hardening receipt verification" $TimeoutSeconds
    try { $payload = [string]$result.Stdout | ConvertFrom-Json }
    catch { throw "Capture-task hardening receipt verification did not return valid JSON." }
    if ([string]$payload.schema_version -ne "dawnstrike.capture_task_hardening_receipt.v2") {
        throw "Only the attested v2 hardening receipt may authorize rebind."
    }
    return [pscustomobject]@{ path = $path; payload = $payload }
}

function Assert-DawnstrikeCaptureHardeningBoundary {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Current,
        [Parameter(Mandatory = $true)][object]$ActivationReceipt,
        [Parameter(Mandatory = $true)][string]$OriginalXml,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$ContractPath,
        [Parameter(Mandatory = $true)][string]$CandidateSha,
        [Parameter(Mandatory = $true)][string]$CandidateTree,
        [Parameter(Mandatory = $true)][string]$OriginUrl,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )
    $receiptRecord = Get-DawnstrikeHardeningReceipt `
        -StateRoot $StateRoot -PythonPath $PythonPath -ContractPath $ContractPath `
        -CandidateSha $CandidateSha -CandidateTree $CandidateTree -TimeoutSeconds $TimeoutSeconds
    $receipt = $receiptRecord.payload
    if (
        [string]$ActivationReceipt.capture_hardening_receipt_relative_path -ne
            [string]$receipt.receipt_relative_path -or
        [string]$ActivationReceipt.capture_hardening_receipt_raw_sha256 -ne
            (Get-DawnstrikeSha256File $receiptRecord.path) -or
        [string]$ActivationReceipt.capture_hardening_receipt_sha256 -ne
            [string]$receipt.receipt_sha256 -or
        [string]$ActivationReceipt.capture_hardening_xml_sha256 -ne
            [string]$receipt.xml_after_sha256 -or
        [string]$ActivationReceipt.capture_hardening_action_sha256 -ne
            [string]$receipt.action_after_sha256 -or
        [string]$ActivationReceipt.capture_hardening_principal_sha256 -ne
            [string]$receipt.principal_after_sha256 -or
        [string]$ActivationReceipt.capture_hardening_trigger_sha256 -ne
            [string]$receipt.trigger_sha256 -or
        [string]$ActivationReceipt.capture_hardening_settings_sha256 -ne
            [string]$receipt.settings_after_sha256
    ) { throw "Activation receipt is not bound to the exact hardening receipt identity." }
    if ([string]$receipt.task_name -ne $script:DawnstrikeAuxiliaryCaptureTaskName -or [string]$receipt.task_path -ne "\") {
        throw "Hardening receipt task identity is invalid."
    }
    $hardeningBackupText = $null
    foreach ($field in @("backup_relative_path", "prepared_relative_path")) {
        $relative = [string]$receipt.$field
        if (
            [string]::IsNullOrWhiteSpace($relative) -or
            [System.IO.Path]::IsPathRooted($relative) -or
            $relative -match '(^|[\\/])\.\.?([\\/]|$)'
        ) { throw "Hardening receipt contains an unsafe durable path." }
        $statePrefix = ([System.IO.Path]::GetFullPath($StateRoot)).TrimEnd('\') + '\'
        $resolved = [System.IO.Path]::GetFullPath((Join-Path $StateRoot ($relative -replace '/', '\')))
        if (-not $resolved.StartsWith($statePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Hardening receipt durable path escaped StateRoot."
        }
        Assert-DawnstrikeNoReparseComponents $resolved "Hardening durable record"
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            throw "Hardening receipt durable record is missing."
        }
        if ($field -eq "backup_relative_path" -and (Get-DawnstrikeSha256File $resolved) -ne [string]$receipt.backup_xml_file_sha256) {
            throw "Hardening XML backup hash does not match the receipt."
        }
        if ($field -eq "backup_relative_path") {
            $hardeningBackupText = [System.IO.File]::ReadAllText($resolved, [System.Text.UTF8Encoding]::new($false))
            if (
                (Get-DawnstrikeSha256Text $hardeningBackupText) -ne [string]$receipt.backup_xml_sha256 -or
                (Get-DawnstrikeSha256Text $hardeningBackupText) -ne [string]$receipt.xml_before_sha256
            ) { throw "Hardening XML backup content does not match the receipt." }
        }
        if ($field -eq "prepared_relative_path" -and (Get-DawnstrikeSha256File $resolved) -ne [string]$receipt.prepared_record_sha256) {
            throw "Hardening PREPARED record hash does not match the receipt."
        }
    }
$receiptRelative = ([System.IO.Path]::GetFullPath($receiptRecord.path).Substring(([System.IO.Path]::GetFullPath($StateRoot)).TrimEnd('\').Length + 1) -replace '\\','/')
    if (
        [string]::IsNullOrWhiteSpace($hardeningBackupText) -or
        [string]$receipt.receipt_relative_path -ne $receiptRelative -or
        [string]$receipt.xml_after_sha256 -ne (Get-DawnstrikeSha256Text $OriginalXml) -or
        [string]$receipt.xml_after_sha256 -ne [string]$Current.xml_sha256
    ) {
        throw "Hardening receipt is not bound to the exact migration and disabled replacement task XML."
    }
    if (
        [string]$receipt.origin_url_sha256 -ne (Get-DawnstrikeSha256Text $OriginUrl) -or
        [string]$receipt.origin_url -ne $OriginUrl
    ) { throw "Hardening receipt origin binding does not match the activation runtime origin." }
    if (
        [string]$receipt.action_sha256 -ne (Get-DawnstrikeHardeningSectionHash $hardeningBackupText "Actions") -or
        [string]$receipt.principal_before_sha256 -ne (Get-DawnstrikeHardeningSectionHash $hardeningBackupText "Principal") -or
        [string]$receipt.settings_before_sha256 -ne (Get-DawnstrikeHardeningSectionHash $hardeningBackupText "Settings") -or
        [string]$receipt.trigger_sha256 -ne (Get-DawnstrikeHardeningSectionHash $hardeningBackupText "Triggers")
    ) {
        throw "Hardening receipt before-state bindings do not match the durable hardening backup."
    }
    if ([string]$receipt.schema_version -ne "dawnstrike.capture_task_hardening_receipt.v2") {
        throw "Only the attested v2 hardening receipt may authorize rebind."
    }
    if (
        [string]$receipt.action_after_sha256 -ne (Get-DawnstrikeHardeningSectionHash $OriginalXml "Actions") -or
        [string]$receipt.action_after_sha256 -ne (Get-DawnstrikeHardeningSectionHash $Current.xml "Actions")
    ) {
        throw "Hardening receipt final action binding does not match the current task."
    }
    if (
        [string]$receipt.trigger_sha256 -ne (Get-DawnstrikeHardeningSectionHash $OriginalXml "Triggers") -or
        [string]$receipt.trigger_sha256 -ne (Get-DawnstrikeHardeningSectionHash $Current.xml "Triggers")
    ) {
        throw "Hardening receipt trigger binding does not match the current task."
    }
    if (
        [string]$receipt.principal_after_sha256 -ne (Get-DawnstrikeHardeningSectionHash $OriginalXml "Principal") -or
        [string]$receipt.principal_after_sha256 -ne (Get-DawnstrikeHardeningSectionHash $Current.xml "Principal")
    ) {
        throw "Hardening receipt principal binding does not match the current task."
    }
    if (
        [string]$receipt.settings_after_sha256 -ne (Get-DawnstrikeHardeningSectionHash $OriginalXml "Settings") -or
        [string]$receipt.settings_after_sha256 -ne (Get-DawnstrikeHardeningSectionHash $Current.xml "Settings")
    ) {
        throw "Hardening receipt settings binding does not match the current task."
    }
    $currentDocument = [System.Xml.XmlDocument]::new()
    $currentDocument.LoadXml([string]$Current.xml)
    $currentPrincipalUser = @($currentDocument.SelectNodes("//*[local-name()='Principal']/*[local-name()='UserId']"))
    if ($currentPrincipalUser.Count -ne 1) { throw "Current auxiliary task principal is ambiguous." }
    $null = Assert-DawnstrikeCaptureTaskSafety -Xml ([string]$Current.xml) -RuntimeRoot $RuntimeRoot -StateRoot $StateRoot -ExpectedPrincipal ([string]$currentPrincipalUser[0].InnerText) -ExpectedCandidateSha $CandidateSha -ExpectedInterpreterPath ([string]$receipt.interpreter_path) -ExpectedInterpreterSha256 ([string]$receipt.interpreter_sha256) -ExpectedInterpreterSignerThumbprint ([string]$receipt.interpreter_signer_thumbprint) -ExpectedEnabled "false" -RequirePasswordPrincipal -RequireRunner
    $records = @(Get-DawnstrikeCaptureActionRecords ([string]$Current.xml))
    if ($records.Count -ne 1) { throw "Current auxiliary task action is ambiguous." }
    $expectedBytecodePrefix = [System.IO.Path]::GetFullPath((Join-Path $StateRoot ("capture-bytecode\" + $CandidateSha)))
    if ([string]$receipt.action_bindings.bytecode_prefix -ine $expectedBytecodePrefix) {
        throw "Hardening receipt bytecode prefix is not candidate-bound."
    }
    $bindingPairs = @(
        @("candidate-sha", "candidate_sha"),
        @("symbols-manifest", "symbols_manifest_path"),
        @("symbols-manifest-sha256", "symbols_manifest_sha256"),
        @("entitlement-receipt", "entitlement_receipt_path"),
        @("entitlement-receipt-sha256", "entitlement_receipt_sha256"),
        @("source-config", "source_config_path"),
        @("source-config-sha256", "source_config_sha256")
    )
    foreach ($pair in $bindingPairs) {
        if ([string](Get-DawnstrikeCaptureBindingValue ([string]$records[0].arguments) $pair[0]) -ne [string]$receipt.action_bindings.($pair[1])) {
            throw "Hardening receipt action input binding does not match the current task: --$($pair[0])."
        }
    }
    if ((Get-DawnstrikeSha256File ([string]$receipt.runner_path)) -ne [string]$receipt.runner_sha256) {
        throw "Hardening receipt runner hash does not match the runner bytes."
    }
    if ([string]$receipt.logon_type -ne "Password" -or $receipt.network_capable -ne $true) {
        throw "Hardening receipt does not attest a network-capable Password principal."
    }
    if ($receipt.start_when_available -ne $true -or $receipt.wake_to_run -ne $true -or $receipt.battery_safe -ne $true) {
        throw "Hardening receipt availability or battery contract is invalid."
    }
    if ($receipt.restart_count -ne 3 -or [string]$receipt.restart_interval -ne "PT15M" -or [string]$receipt.execution_time_limit -ne "PT3H" -or [string]$receipt.multiple_instances -ne "IgnoreNew") {
        throw "Hardening receipt restart or execution contract is invalid."
    }
    if ($receipt.research_only -ne $true -or $receipt.broker_execution_enabled -ne $false) {
        throw "Hardening receipt safety boundary is invalid."
    }
    $receipt | Add-Member -NotePropertyName __path -NotePropertyValue $receiptRecord.path -Force
    return $receipt
}

function Get-DawnstrikeNormalizedAuxiliaryXml {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Xml,
        [Parameter(Mandatory = $true)][string]$OldSha,
        [Parameter(Mandatory = $true)][string]$NewSha
    )
    $value = $Xml.Replace($OldSha, "DAWNSTRIKE_CANDIDATE_SHA").Replace($NewSha, "DAWNSTRIKE_CANDIDATE_SHA")
    return $value
}

$script:DawnstrikeCaptureBindingNames = @(
    "candidate-sha",
    "symbols-manifest",
    "symbols-manifest-sha256",
    "entitlement-receipt",
    "entitlement-receipt-sha256",
    "source-config",
    "source-config-sha256"
)

function Get-DawnstrikeCaptureActionRecords {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Xml)

    try {
        $document = [System.Xml.XmlDocument]::new()
        $document.PreserveWhitespace = $true
        $document.LoadXml($Xml)
        $nodes = @($document.SelectNodes("//*[local-name()='Exec']"))
        if ($nodes.Count -lt 1) { throw "no Exec action" }
        $records = @()
        foreach ($node in $nodes) {
            $command = @($node.SelectNodes("./*[local-name()='Command']"))
            $arguments = @($node.SelectNodes("./*[local-name()='Arguments']"))
            $working = @($node.SelectNodes("./*[local-name()='WorkingDirectory']"))
            if ($command.Count -ne 1 -or $arguments.Count -ne 1 -or $working.Count -ne 1) {
                throw "an Exec action has an incomplete command contract"
            }
            $records += [pscustomobject]@{
                execute = [string]$command[0].InnerText
                arguments = [string]$arguments[0].InnerText
                working_directory = [string]$working[0].InnerText
            }
        }
        return $records
    }
    catch { throw "Auxiliary capture XML has an invalid action contract." }
}

function Get-DawnstrikeCaptureBindingPattern {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Name)

    $escaped = [regex]::Escape($Name)
    $option = "(?:`"--$escaped`"|'--$escaped'|--$escaped)"
    return '(?i)(?<![A-Za-z0-9_-])' + $option +
        '(?:=|\s+)(?:"[^"]*"|''[^'']*''|[^\s]+)'
}

function Get-DawnstrikeCaptureBindingMatches {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Arguments,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $pattern = Get-DawnstrikeCaptureBindingPattern $Name
    return @([regex]::Matches($Arguments, $pattern))
}

function Get-DawnstrikeCaptureBindingValue {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Arguments,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $escaped = [regex]::Escape($Name)
    $option = "(?:`"--$escaped`"|'--$escaped'|--$escaped)"
    $pattern = '(?i)(?<![A-Za-z0-9_-])' + $option +
        '(?:=|\s+)(?:(?<double>"[^"]*")|(?<single>''[^'']*'')|(?<bare>[^\s]+))'
    $matches = @([regex]::Matches($Arguments, $pattern))
    if ($matches.Count -gt 1) {
        throw "Scheduled capture action contains duplicate --$Name bindings."
    }
    if ($matches.Count -eq 0) { return $null }
    $match = $matches[0]
    if ($match.Groups["double"].Success) {
        return [string]$match.Groups["double"].Value.Trim('"')
    }
    if ($match.Groups["single"].Success) {
        return [string]$match.Groups["single"].Value.Trim("'")
    }
    return [string]$match.Groups["bare"].Value
}

function ConvertTo-DawnstrikeCaptureActionValue {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)

    return '"' + $Value.Replace('"', '\"') + '"'
}

function Set-DawnstrikeCaptureBindingValue {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Arguments,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value
    )

    $escaped = [regex]::Escape($Name)
    $option = "(?:`"--$escaped`"|'--$escaped'|--$escaped)"
    $pattern = '(?i)(?<prefix>(?<![A-Za-z0-9_-])' + $option +
        '(?:=|\s+))(?:(?<double>"[^"]*")|(?<single>''[^'']*'')|(?<bare>[^\s]+))'
    $matches = @([regex]::Matches($Arguments, $pattern))
    if ($matches.Count -gt 1) {
        throw "Scheduled capture action contains duplicate --$Name bindings."
    }
    if ($matches.Count -eq 0) {
        return ($Arguments.TrimEnd() + " --$Name " + (ConvertTo-DawnstrikeCaptureActionValue $Value)).Trim()
    }
    $replacement = {
        param($match)
        $prefix = [string]$match.Groups["prefix"].Value
        if ($match.Groups["double"].Success) {
            return $prefix + (ConvertTo-DawnstrikeCaptureActionValue $Value)
        }
        if ($match.Groups["single"].Success) {
            return $prefix + "'" + $Value.Replace("'", "''") + "'"
        }
        return $prefix + $Value
    }
    return [regex]::Replace($Arguments, $pattern, $replacement, 1)
}

function Get-DawnstrikeCaptureArgumentSkeleton {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Arguments)

    $remaining = $Arguments
    foreach ($name in $script:DawnstrikeCaptureBindingNames) {
        $matches = Get-DawnstrikeCaptureBindingMatches $remaining $name
        if ($matches.Count -gt 1) {
            throw "Scheduled capture action contains duplicate --$name bindings."
        }
        if ($matches.Count -eq 1) {
            $remaining = $remaining.Remove($matches[0].Index, $matches[0].Length)
        }
    }
    $remaining = [regex]::Replace($remaining, '\s+', ' ').Trim()
    $canonical = @(
        "--candidate-sha DAWNSTRIKE_CAPTURE_CANDIDATE",
        "--symbols-manifest DAWNSTRIKE_CAPTURE_SYMBOLS_PATH",
        "--symbols-manifest-sha256 DAWNSTRIKE_CAPTURE_SYMBOLS_HASH",
        "--entitlement-receipt DAWNSTRIKE_CAPTURE_ENTITLEMENT_PATH",
        "--entitlement-receipt-sha256 DAWNSTRIKE_CAPTURE_ENTITLEMENT_HASH",
        "--source-config DAWNSTRIKE_CAPTURE_SOURCE_PATH",
        "--source-config-sha256 DAWNSTRIKE_CAPTURE_SOURCE_HASH"
    ) -join " "
    if ([string]::IsNullOrWhiteSpace($remaining)) { return $canonical }
    return "$remaining $canonical"
}

function Get-DawnstrikeCaptureNormalizedDefinitionHash {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Xml)

    try {
        $document = [System.Xml.XmlDocument]::new()
        $document.PreserveWhitespace = $true
        $document.LoadXml($Xml)
        foreach ($node in @($document.SelectNodes("//*[local-name()='Arguments']"))) {
            $node.InnerText = Get-DawnstrikeCaptureArgumentSkeleton ([string]$node.InnerText)
        }
        $settingsEnabled = @($document.SelectNodes("/*[local-name()='Task']/*[local-name()='Settings']/*[local-name()='Enabled']"))
        if ($settingsEnabled.Count -gt 1) { throw "duplicate Settings/Enabled node" }
        if ($settingsEnabled.Count -eq 1) { $settingsEnabled[0].InnerText = "DAWNSTRIKE_CAPTURE_ENABLEMENT" }
        return Get-DawnstrikeSha256Text (Get-DawnstrikeTaskDefinitionText ([string]$document.OuterXml))
    }
    catch { throw "Auxiliary capture XML cannot produce a normalized definition contract." }
}

function Assert-DawnstrikeCaptureActionTransformation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$OriginalXml,
        [Parameter(Mandatory = $true)][string]$CurrentXml,
        [Parameter(Mandatory = $true)][string]$PreviousSha,
        [Parameter(Mandatory = $true)][string]$CandidateSha,
        [Parameter(Mandatory = $true)][string]$SymbolsManifest,
        [Parameter(Mandatory = $true)][string]$SymbolsManifestSha256,
        [Parameter(Mandatory = $true)][string]$EntitlementReceipt,
        [Parameter(Mandatory = $true)][string]$EntitlementReceiptSha256,
        [Parameter(Mandatory = $true)][string]$SourceConfig,
        [Parameter(Mandatory = $true)][string]$SourceConfigSha256
    )

    $originalRecords = @(Get-DawnstrikeCaptureActionRecords $OriginalXml)
    $currentRecords = @(Get-DawnstrikeCaptureActionRecords $CurrentXml)
    if ($originalRecords.Count -ne 1 -or $currentRecords.Count -ne 1) {
        throw "Auxiliary capture task must contain exactly one governed action."
    }
    $originalCandidate = Get-DawnstrikeCaptureBindingValue ([string]$originalRecords[0].arguments) "candidate-sha"
    if ($null -eq $originalCandidate -or $originalCandidate.ToLowerInvariant() -ne $PreviousSha.ToLowerInvariant()) {
        throw "Auxiliary capture XML does not contain the exact original candidate SHA binding."
    }
    $currentCandidate = Get-DawnstrikeCaptureBindingValue ([string]$currentRecords[0].arguments) "candidate-sha"
    if ($null -eq $currentCandidate -or $currentCandidate.ToLowerInvariant() -ne $CandidateSha.ToLowerInvariant()) {
        throw "Auxiliary capture XML does not contain the exact replacement candidate SHA binding."
    }
    foreach ($name in @("Principal", "Triggers")) {
        if (
            (Get-DawnstrikeAuxiliarySectionHash $OriginalXml $name) -ne
                (Get-DawnstrikeAuxiliarySectionHash $CurrentXml $name)
        ) { throw "Capture-task principal, triggers, or settings changed during rebind." }
    }
    if (
        (Get-DawnstrikeHardeningSectionHash $OriginalXml "Settings" "false") -ne
            (Get-DawnstrikeHardeningSectionHash $CurrentXml "Settings" "false")
    ) { throw "Capture-task settings changed outside the governed enablement transition." }
    if ((Get-DawnstrikeCaptureNormalizedDefinitionHash $OriginalXml) -ne
        (Get-DawnstrikeCaptureNormalizedDefinitionHash $CurrentXml)) {
        throw "Capture-task XML changed outside the permitted action bindings."
    }
    $candidateCount = 0
    foreach ($index in 0..($currentRecords.Count - 1)) {
        $before = $originalRecords[$index]
        $after = $currentRecords[$index]
        if (
            [string]$before.execute -ne [string]$after.execute -or
            [string]$before.working_directory -ne [string]$after.working_directory -or
            (Get-DawnstrikeCaptureArgumentSkeleton ([string]$before.arguments)) -ne
                (Get-DawnstrikeCaptureArgumentSkeleton ([string]$after.arguments))
        ) { throw "Capture-task action changed outside the permitted bindings." }
        $candidateValue = Get-DawnstrikeCaptureBindingValue ([string]$after.arguments) "candidate-sha"
        if ($null -ne $candidateValue) {
            $candidateCount += 1
            if ($candidateValue.ToLowerInvariant() -ne $CandidateSha.ToLowerInvariant()) {
                throw "Capture-task action candidate SHA is not the requested exact SHA."
            }
        }
        foreach ($binding in @(
            @("symbols-manifest", $SymbolsManifest),
            @("entitlement-receipt", $EntitlementReceipt),
            @("source-config", $SourceConfig)
        )) {
            $actualPath = Get-DawnstrikeCaptureBindingValue ([string]$after.arguments) $binding[0]
            if ($null -eq $actualPath -or
                [System.IO.Path]::GetFullPath($actualPath) -ne [System.IO.Path]::GetFullPath([string]$binding[1])) {
                throw "Capture-task action --$($binding[0]) is not bound to the supplied input path."
            }
        }
        foreach ($binding in @(
            @("symbols-manifest-sha256", $SymbolsManifestSha256),
            @("entitlement-receipt-sha256", $EntitlementReceiptSha256),
            @("source-config-sha256", $SourceConfigSha256)
        )) {
            $actualHash = Get-DawnstrikeCaptureBindingValue ([string]$after.arguments) $binding[0]
            if ($null -eq $actualHash -or $actualHash.ToLowerInvariant() -ne [string]$binding[1].ToLowerInvariant()) {
                throw "Capture-task action --$($binding[0]) is not bound to the supplied input hash."
            }
        }
    }
    if ($candidateCount -ne 1) { throw "Capture-task action candidate SHA binding is missing or ambiguous." }
    return $currentRecords
}

function Assert-DawnstrikeCaptureInput {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$SymbolsManifest,
        [Parameter(Mandatory = $true)][string]$SymbolsManifestSha256,
        [Parameter(Mandatory = $true)][string]$EntitlementReceipt,
        [Parameter(Mandatory = $true)][string]$EntitlementReceiptSha256,
        [Parameter(Mandatory = $true)][string]$SourceConfig,
        [Parameter(Mandatory = $true)][string]$SourceConfigSha256
    )
    foreach ($entry in @(
        @($SymbolsManifest, $SymbolsManifestSha256, "Symbols manifest"),
        @($EntitlementReceipt, $EntitlementReceiptSha256, "Entitlement receipt"),
        @($SourceConfig, $SourceConfigSha256, "Source config")
    )) {
        Assert-DawnstrikeNoReparseComponents $entry[0] $entry[2]
        $item = Get-Item -LiteralPath $entry[0] -Force -ErrorAction Stop
        if ($item.PSIsContainer -or ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$($entry[2]) must be a regular file."
        }
        if ((Get-DawnstrikeSha256File $entry[0]) -ne [string]$entry[1]) {
            throw "$($entry[2]) hash does not match the supplied identity."
        }
    }
    try { $manifest = Get-Content -LiteralPath $SymbolsManifest -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw "Symbols manifest is invalid JSON." }
    if (
        $null -eq $manifest -or
        [string]::IsNullOrWhiteSpace([string]$manifest.membership_policy) -or
        @($manifest.symbols).Count -lt 1 -or
        $manifest.point_in_time_membership -notin @($false, "research_control_only", "not_claimed")
    ) { throw "Symbols manifest violates the bounded capture contract." }
    try { $entitlement = Get-Content -LiteralPath $EntitlementReceipt -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw "Entitlement receipt is invalid JSON." }
    $provenEndpoints = @($entitlement.proven_endpoints | Where-Object { $_ -in @("bars", "trades", "quotes") })
    if (
        [string]$entitlement.provider -ne "alpaca" -or
        [string]$entitlement.feed -ne "sip" -or
        [string]$entitlement.probe_status -ne "PASS" -or
        $provenEndpoints.Count -lt 3 -or
        $entitlement.retention_allowed -ne $true -or
        $entitlement.approved_plan -ne $true -or
        $entitlement.research_only -ne $true -or
        [string]$entitlement.broker_execution -ne "disabled" -or
        [string]::IsNullOrWhiteSpace([string]$entitlement.entitlement) -or
        [string]::IsNullOrWhiteSpace($(if ($null -ne $entitlement.receipt) { [string]$entitlement.receipt } else { [string]$entitlement.proof_id }))
    ) { throw "Entitlement receipt does not prove the approved SIP research inputs." }
    return [pscustomobject]@{ symbols = $manifest; entitlement = $entitlement }
}

function Get-DawnstrikeCaptureOriginalFromActivationBackup {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][object]$ActivationReceipt
    )

    if ([string]$ActivationReceipt.auxiliary_capture_backup_name -notmatch '^runtime-(activation|rollback)-[0-9a-f]{24}$') {
        throw "Activation receipt auxiliary capture backup name is invalid."
    }
    $backupName = [string]$ActivationReceipt.auxiliary_capture_backup_name
    $backupRoot = Join-Path $StateRoot ("scheduler-backups\" + $backupName)
    $manifestPath = Join-Path $backupRoot "manifest.json"
    Assert-DawnstrikeNoReparseComponents $backupRoot "Capture-task activation backup root"
    Assert-DawnstrikeNoReparseComponents $manifestPath "Capture-task activation backup manifest"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Capture-task activation backup manifest is missing."
    }
    if ((Get-DawnstrikeSha256File $manifestPath) -ne [string]$ActivationReceipt.auxiliary_capture_backup_manifest_sha256) {
        throw "Capture-task activation backup manifest hash does not match the activation receipt."
    }
    try { $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw "Capture-task activation backup manifest is invalid JSON." }
    if (
        [string]$manifest.schema_version -ne "dawnstrike.scheduler_xml_backup.v1" -or
        [string]$manifest.activation_id -ne [string]$ActivationReceipt.activation_id -or
        $manifest.research_only -ne $true -or
        $manifest.broker_execution_enabled -ne $false
    ) { throw "Capture-task activation backup manifest violates the safety contract." }
    $entry = $manifest.auxiliary_capture
    if ($null -eq $entry -or $entry.present -ne $true) {
        throw "Activation backup does not contain the required original capture task."
    }
    if (
        [string]$entry.task_name -ne $script:DawnstrikeAuxiliaryCaptureTaskName -or
        [string]$entry.state_before -notin @("Ready", "Disabled") -or
        [string]$entry.action -ne "DISABLED_UNTIL_EXACT_SHA_REBIND" -or
        [string]$entry.file_name -notmatch '^[A-Za-z0-9_.-]+\.xml$'
    ) { throw "Activation backup auxiliary capture entry is invalid." }
    $xmlPath = Join-Path $backupRoot ([string]$entry.file_name)
    Assert-DawnstrikeNoReparseComponents $xmlPath "Capture-task original XML backup"
    $xmlItem = Get-Item -LiteralPath $xmlPath -Force -ErrorAction Stop
    if ($xmlItem.PSIsContainer -or ($xmlItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Capture-task original XML backup is not a regular file."
    }
    if ((Get-DawnstrikeSha256File $xmlPath) -ne [string]$entry.xml_file_sha256) {
        throw "Capture-task original XML backup file hash does not match its manifest."
    }
    $xml = [System.IO.File]::ReadAllText($xmlPath)
    Assert-DawnstrikeNoReparseComponents $xmlPath "Capture-task original XML backup"
    if ((Get-DawnstrikeSha256File $xmlPath) -ne [string]$entry.xml_file_sha256) {
        throw "Capture-task original XML backup changed during read."
    }
    if (
        (Get-DawnstrikeSha256Text $xml) -ne [string]$entry.xml_sha256 -or
        [string]$entry.xml_sha256 -ne [string]$ActivationReceipt.auxiliary_capture_xml_sha256 -or
        [string]$entry.xml_file_sha256 -ne [string]$ActivationReceipt.auxiliary_capture_xml_file_sha256 -or
        [string]$entry.definition_contract_sha256 -ne [string]$ActivationReceipt.auxiliary_capture_definition_contract_sha256 -or
        [string]$entry.action_contract_sha256 -ne [string]$ActivationReceipt.auxiliary_capture_action_contract_sha256
    ) { throw "Capture-task original XML backup does not match the activation receipt." }
    return [pscustomobject]@{
        present = $true
        task_name = $script:DawnstrikeAuxiliaryCaptureTaskName
        task_path = [string]$entry.task_path
        state = "Disabled"
        enabled = $false
        xml = $xml
        xml_sha256 = [string]$entry.xml_sha256
        xml_file_sha256 = [string]$entry.xml_file_sha256
        definition_contract_sha256 = [string]$entry.definition_contract_sha256
        action_contract_sha256 = [string]$entry.action_contract_sha256
    }
}

function Get-DawnstrikeCapturePreparedRecord {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$ContractPath,
        [Parameter(Mandatory = $true)][string]$PreparedPath,
        [Parameter(Mandatory = $true)][string]$CandidateSha,
        [Parameter(Mandatory = $true)][string]$CandidateTree,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )
    $result = Invoke-DawnstrikeActivationProcess $PythonPath @(
        $ContractPath, "verify-prepared", "--prepared", $PreparedPath,
        "--candidate-sha", $CandidateSha, "--candidate-tree", $CandidateTree
    ) $PSScriptRoot "Capture-task PREPARED record verification" $TimeoutSeconds
    try { return ([string]$result.Stdout | ConvertFrom-Json) }
    catch { throw "Capture-task PREPARED record verification did not return valid JSON." }
}

function Assert-DawnstrikeCapturePreparedChain {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Prepared,
        [Parameter(Mandatory = $true)][object]$ActivationReceipt,
        [Parameter(Mandatory = $true)][string]$ActivationReceiptName,
        [Parameter(Mandatory = $true)][string]$ActivationReceiptSha256,
        [Parameter(Mandatory = $true)][object]$Original,
        [Parameter(Mandatory = $true)][string]$CandidateSha,
        [Parameter(Mandatory = $true)][string]$CandidateTree,
        [Parameter(Mandatory = $true)][string]$SymbolsManifest,
        [Parameter(Mandatory = $true)][string]$SymbolsManifestSha256,
        [Parameter(Mandatory = $true)][string]$EntitlementReceipt,
        [Parameter(Mandatory = $true)][string]$EntitlementReceiptSha256,
        [Parameter(Mandatory = $true)][string]$SourceConfig,
        [Parameter(Mandatory = $true)][string]$SourceConfigSha256
    )
    if (
        [string]$Prepared.activation_id -ne [string]$ActivationReceipt.activation_id -or
        [string]$Prepared.activation_receipt_name -ne $ActivationReceiptName -or
        [string]$Prepared.activation_receipt_sha256 -ne $ActivationReceiptSha256 -or
        [string]$Prepared.candidate_sha -ne $CandidateSha -or
        [string]$Prepared.candidate_tree -ne $CandidateTree -or
        [string]$Prepared.xml_before_sha256 -ne [string]$Original.xml_sha256 -or
        [string]$Prepared.action_before_sha256 -ne [string]$Original.action_contract_sha256 -or
        [string]$Prepared.definition_before_sha256 -ne [string]$Original.definition_contract_sha256 -or
        [string]$Prepared.normalized_definition_before_sha256 -ne (Get-DawnstrikeCaptureNormalizedDefinitionHash ([string]$Original.xml)) -or
        [string]$Prepared.principal_sha256 -ne (Get-DawnstrikeAuxiliarySectionHash ([string]$Original.xml) "Principal") -or
        [string]$Prepared.trigger_sha256 -ne (Get-DawnstrikeAuxiliarySectionHash ([string]$Original.xml) "Triggers") -or
        [string]$Prepared.settings_sha256 -ne (Get-DawnstrikeAuxiliarySectionHash ([string]$Original.xml) "Settings") -or
        [string]$Prepared.previous_candidate_sha -notmatch '^[0-9a-f]{40}$'
    ) { throw "Capture-task PREPARED record is not bound to the exact activation and original XML." }
    $preparedOriginalRecords = @(Get-DawnstrikeCaptureActionRecords ([string]$Original.xml))
    if (
        $preparedOriginalRecords.Count -ne 1 -or
        [string](Get-DawnstrikeCaptureBindingValue ([string]$preparedOriginalRecords[0].arguments) "candidate-sha") -ne
            [string]$Prepared.previous_candidate_sha
    ) {
        throw "Capture-task original XML does not contain the exact PREPARED candidate SHA binding."
    }
    foreach ($entry in @(
        @("symbols_manifest_path", $SymbolsManifest),
        @("entitlement_receipt_path", $EntitlementReceipt),
        @("source_config_path", $SourceConfig),
        @("symbols_manifest_sha256", $SymbolsManifestSha256),
        @("entitlement_receipt_sha256", $EntitlementReceiptSha256),
        @("source_config_sha256", $SourceConfigSha256)
    )) {
        $actual = [string]$Prepared.($entry[0])
        if ($entry[0].EndsWith("_path")) {
            if ([System.IO.Path]::GetFullPath($actual) -ne [System.IO.Path]::GetFullPath([string]$entry[1])) {
                throw "Capture-task PREPARED input path does not match the supplied input."
            }
        }
        elseif ($actual.ToLowerInvariant() -ne [string]$entry[1].ToLowerInvariant()) {
            throw "Capture-task PREPARED input hash does not match the supplied input."
        }
    }
}

function New-DawnstrikeCaptureReboundActions {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object[]]$Actions,
        [Parameter(Mandatory = $true)][string]$CandidateSha,
        [Parameter(Mandatory = $true)][string]$SymbolsManifest,
        [Parameter(Mandatory = $true)][string]$SymbolsManifestSha256,
        [Parameter(Mandatory = $true)][string]$EntitlementReceipt,
        [Parameter(Mandatory = $true)][string]$EntitlementReceiptSha256,
        [Parameter(Mandatory = $true)][string]$SourceConfig,
        [Parameter(Mandatory = $true)][string]$SourceConfigSha256
    )
    $bindings = @(
        @("candidate-sha", $CandidateSha),
        @("symbols-manifest", $SymbolsManifest),
        @("symbols-manifest-sha256", $SymbolsManifestSha256),
        @("entitlement-receipt", $EntitlementReceipt),
        @("entitlement-receipt-sha256", $EntitlementReceiptSha256),
        @("source-config", $SourceConfig),
        @("source-config-sha256", $SourceConfigSha256)
    )
    $newActions = @()
    foreach ($action in $Actions) {
        $arguments = [string]$action.Arguments
        foreach ($binding in $bindings) {
            $arguments = Set-DawnstrikeCaptureBindingValue $arguments $binding[0] ([string]$binding[1])
        }
        $newActions += New-ScheduledTaskAction `
            -Execute ([string]$action.Execute) -Argument $arguments `
            -WorkingDirectory ([string]$action.WorkingDirectory)
    }
    return $newActions
}

function Remove-DawnstrikeCapturePrepared {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)
    Assert-DawnstrikeNoReparseComponents $Path "Capture-task PREPARED record"
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Remove-Item -LiteralPath $Path -Force -ErrorAction Stop
        if (Test-Path -LiteralPath $Path) { throw "Capture-task PREPARED record could not be removed." }
    }
}

function New-DawnstrikeCaptureReceiptPayload {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Prepared,
        [Parameter(Mandatory = $true)][object]$Final,
        [Parameter(Mandatory = $true)][string]$CandidateSha,
        [Parameter(Mandatory = $true)][string]$CandidateTree,
        [Parameter(Mandatory = $true)][string]$ActivationId,
        [Parameter(Mandatory = $true)][string]$ActivationReceiptName,
        [Parameter(Mandatory = $true)][string]$ActivationReceiptSha256,
        [Parameter(Mandatory = $true)][string]$RuntimeOriginSha256,
        [Parameter(Mandatory = $true)][object]$HardeningReceipt
    )
    return [ordered]@{
        schema_version = "dawnstrike.capture_task_rebind_receipt.v2"
        status = "COMPLETE"
        task_name = $script:DawnstrikeAuxiliaryCaptureTaskName
        candidate_sha = $CandidateSha
        candidate_tree = $CandidateTree
        activation_id = [string]$Prepared.activation_id
        activation_receipt_name = $ActivationReceiptName
        activation_receipt_sha256 = $ActivationReceiptSha256
        runtime_origin_sha256 = $RuntimeOriginSha256
        previous_candidate_sha = [string]$Prepared.previous_candidate_sha
        xml_before_sha256 = [string]$Prepared.xml_before_sha256
        xml_after_sha256 = Get-DawnstrikeSha256Text ([string]$Final.xml)
        action_before_sha256 = [string]$Prepared.action_before_sha256
        action_after_sha256 = [string]$Final.action_contract_sha256
        definition_before_sha256 = [string]$Prepared.definition_before_sha256
        definition_after_sha256 = [string]$Final.definition_contract_sha256
        principal_sha256 = [string]$Prepared.principal_sha256
        trigger_sha256 = [string]$Prepared.trigger_sha256
        settings_sha256 = [string]$Prepared.settings_sha256
        symbols_manifest_sha256 = [string]$Prepared.symbols_manifest_sha256
        entitlement_receipt_sha256 = [string]$Prepared.entitlement_receipt_sha256
        source_config_sha256 = [string]$Prepared.source_config_sha256
        enablement_before = "Disabled"
        enablement_after = "Ready"
        changed_field = "candidate_sha_and_input_bindings"
        preserved_contract = $true
        completed_at_utc = [DateTime]::UtcNow.ToString("o")
        research_only = $true
        broker_execution_enabled = $false
        hardening_receipt_relative_path = [string]$HardeningReceipt.receipt_relative_path
        hardening_receipt_raw_sha256 = Get-DawnstrikeSha256File ([string]$HardeningReceipt.__path)
        hardening_receipt_sha256 = [string]$HardeningReceipt.receipt_sha256
        hardening_xml_sha256 = [string]$HardeningReceipt.xml_after_sha256
        hardening_action_sha256 = [string]$HardeningReceipt.action_after_sha256
        hardening_principal_sha256 = [string]$HardeningReceipt.principal_after_sha256
        hardening_trigger_sha256 = [string]$HardeningReceipt.trigger_sha256
        hardening_settings_sha256 = [string]$HardeningReceipt.settings_after_sha256
    }
}

if (-not $Enable) { throw "Exact-SHA capture rebind requires explicit -Enable." }
$runtime = Resolve-DawnstrikeActivationRoot $RuntimeRoot "RuntimeRoot"
$state = Resolve-DawnstrikeActivationRoot $StateRoot "StateRoot"
Assert-DawnstrikeRootIsolation $state @($runtime) "StateRoot"
$git = @(Get-Command git.exe -CommandType Application -ErrorAction Stop)[0].Source
$python = @(Get-Command py.exe -CommandType Application -ErrorAction Stop)[0].Source
$runtimeContract = Get-DawnstrikeGitContract $git $runtime $ProcessTimeoutSeconds
if ([string]::IsNullOrWhiteSpace($CandidateSha)) { $CandidateSha = [string]$runtimeContract.head }
if ($runtimeContract.head -ne $CandidateSha) { throw "Runtime HEAD is not the requested exact candidate SHA." }
$origin = Get-DawnstrikeGitValue $git $runtime @("remote", "get-url", "origin") "Capture rebind origin verification" $ProcessTimeoutSeconds
Assert-DawnstrikeSafeOrigin $origin
$remoteMain = Get-DawnstrikeGitValue $git $runtime @("rev-parse", "refs/remotes/origin/main") "Capture rebind origin/main verification" $ProcessTimeoutSeconds
if ($remoteMain.ToLowerInvariant() -ne $CandidateSha) { throw "Runtime HEAD is not the exact origin/main SHA." }
$inputs = Assert-DawnstrikeCaptureInput `
    -SymbolsManifest $SymbolsManifest -SymbolsManifestSha256 $SymbolsManifestSha256 `
    -EntitlementReceipt $EntitlementReceipt -EntitlementReceiptSha256 $EntitlementReceiptSha256 `
    -SourceConfig $SourceConfig -SourceConfigSha256 $SourceConfigSha256

$activationReceiptRoot = Join-Path $state "receipts\runtime-activation"
Assert-DawnstrikeNoReparseComponents $activationReceiptRoot "Activation receipt root"
$activationReceipts = @()
foreach ($item in @(Get-ChildItem -LiteralPath $activationReceiptRoot -Filter "runtime-activation-*.json" -File -ErrorAction SilentlyContinue)) {
    try {
        Assert-DawnstrikeNoReparseComponents $item.FullName "Activation receipt"
        $candidateReceipt = Invoke-DawnstrikeActivationProcess $python @(
            (Join-Path $PSScriptRoot "runtime_activation_contract.py"),
            "verify-receipt", "--receipt", $item.FullName, "--expected-status", "COMPLETE"
        ) $PSScriptRoot "Capture rebind activation receipt verification" $ProcessTimeoutSeconds
        $parsedCandidateReceipt = [string]$candidateReceipt.Stdout | ConvertFrom-Json
        if (
            [string]$parsedCandidateReceipt.candidate_sha -eq $CandidateSha -and
            [string]$parsedCandidateReceipt.candidate_tree -eq [string]$runtimeContract.tree -and
            $parsedCandidateReceipt.auxiliary_capture_present -eq $true -and
            [string]$parsedCandidateReceipt.auxiliary_capture_state_after -eq "Disabled" -and
            [string]$parsedCandidateReceipt.auxiliary_capture_action -eq "DISABLED_UNTIL_EXACT_SHA_REBIND"
        ) {
            $activationReceipts += [pscustomobject]@{
                payload = $parsedCandidateReceipt
                path = $item.FullName
                name = $item.Name
                sha256 = Get-DawnstrikeSha256File $item.FullName
            }
        }
    }
    catch { }
}
if ($activationReceipts.Count -ne 1) {
    throw "Exact-SHA capture rebind requires one and only one matching COMPLETE activation receipt."
}
$activationReceipt = $activationReceipts[0]
$activationId = [string]$activationReceipt.payload.activation_id
$activationReceiptName = [string]$activationReceipt.name
$activationReceiptSha256 = [string]$activationReceipt.sha256

if ([string]::IsNullOrWhiteSpace($ReceiptPath)) {
    $ReceiptPath = Join-Path $state ("receipts\capture-task\capture-task-rebind-" + $CandidateSha + ".json")
}
$receiptRoot = [System.IO.Path]::GetFullPath((Join-Path $state "receipts\capture-task")).TrimEnd('\') + '\'
$receiptFull = [System.IO.Path]::GetFullPath($ReceiptPath)
Assert-DawnstrikeNoReparseComponents $receiptFull "Capture-task receipt"
if (-not $receiptFull.StartsWith($receiptRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Capture-task receipt must remain inside the durable capture-task receipt root."
}

$auxiliary = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
if (-not $auxiliary.present) { throw "Auxiliary capture task is absent; registration and rebind are separate governed actions." }
$captureContract = Join-Path $PSScriptRoot "capture_task_contract.py"
$hardeningContract = Join-Path $PSScriptRoot "capture_task_hardening_contract.py"
$hardeningReceipt = $null
$preparedPath = Join-Path $state ("receipts\capture-task\capture-task-rebind-" + $CandidateSha + ".prepared.json")
$failurePath = Join-Path $state ("receipts\capture-task\capture-task-rebind-" + $CandidateSha + ".failed.json")
$compensationReceiptPath = Join-Path $state ("receipts\capture-task\capture-task-rebind-" + $CandidateSha + ".compensated.json")
$compensationReceiptRelativePath = "receipts/capture-task/capture-task-rebind-$CandidateSha.compensated.json"
$operationJournalPath = Join-Path $state ("receipts\runtime-operation\capture-task-rebind-" + $CandidateSha + ".json")
$journalPreparedRelativePath = "receipts/capture-task/capture-task-rebind-$CandidateSha.prepared.json"
$journalCompleteRelativePath = "receipts/capture-task/capture-task-rebind-$CandidateSha.json"
$journalTaskContractSha256 = Get-DawnstrikeSha256File $captureContract
$journalEmptySha256 = Get-DawnstrikeSha256Text ""
Assert-DawnstrikeNoReparseComponents $preparedPath "Capture-task prepared record"
Assert-DawnstrikeNoReparseComponents $failurePath "Capture-task failure record"
Assert-DawnstrikeNoReparseComponents $operationJournalPath "Capture-task operation journal"
$original = Get-DawnstrikeCaptureOriginalFromActivationBackup $state $activationReceipt.payload
$previousCandidates = @(
    (Get-DawnstrikeCaptureActionRecords ([string]$original.xml)) | ForEach-Object {
        Get-DawnstrikeCaptureBindingValue ([string]$_.arguments) "candidate-sha"
    } | Where-Object { $null -ne $_ -and $_ -match '^[0-9a-f]{40}$' }
)
if ($previousCandidates.Count -ne 1) { throw "Activation-bound auxiliary action candidate SHA pin is missing or ambiguous." }
$previousSha = [string]$previousCandidates[0]
$lockInterpreter = Get-DawnstrikeApprovedLockInterpreter

$compensatedReceiptRecovered = $false
if (Test-Path -LiteralPath $receiptFull -PathType Leaf) {
    $existingReceipt = Invoke-DawnstrikeActivationProcess $python @(
        $captureContract, "verify-receipt", "--receipt", $receiptFull,
        "--candidate-sha", $CandidateSha, "--candidate-tree", $runtimeContract.tree
    ) $PSScriptRoot "Existing capture-task receipt verification" $ProcessTimeoutSeconds
    try { $existingPayload = [string]$existingReceipt.Stdout | ConvertFrom-Json }
    catch { throw "Existing capture-task receipt verification did not return valid JSON." }
    # A failed post-seal/pre-journal-complete attempt leaves the canonical
    # receipt present while compensation has restored Disabled.  Resolve the
    # COMPENSATED tombstone before applying the normal COMPLETE/Ready fast
    # path, and preserve the canonical bytes under an immutable hash name.
    if (Test-Path -LiteralPath $operationJournalPath -PathType Leaf) {
        $preExistingJournal = Get-DawnstrikeStrictRuntimeOperationJournal $operationJournalPath $lockInterpreter.path $lockInterpreter.sha256
        if ([string]$preExistingJournal.payload.phase -eq "COMPENSATED") {
            $compensatedPath = Join-Path $state ([string]$preExistingJournal.payload.compensation_receipt_relative_path).Replace('/', '\')
            $compensationCheck = & $lockInterpreter.path -I -B (Join-Path $PSScriptRoot "runtime_operation_journal.py") verify-compensation `
                --receipt $compensatedPath --state-root $state 2>$null
            if ($LASTEXITCODE -ne 0) { throw "Compensated rebind receipt failed strict validation." }
            $compensationPayload = (($compensationCheck -join "") | ConvertFrom-Json).payload
            if ($compensationPayload.operation -ne "capture_task_rebind" -or
                $compensationPayload.candidate_sha -ne $CandidateSha -or $compensationPayload.candidate_tree -ne [string]$runtimeContract.tree -or
                $compensationPayload.task_contract_sha256 -ne $journalTaskContractSha256 -or
                $compensationPayload.task_action_contract_sha256 -ne $auxiliary.action_contract_sha256 -or
                $compensationPayload.task_definition_contract_sha256 -ne $auxiliary.definition_contract_sha256 -or
                $compensationPayload.prior_journal_file_sha256 -ne $preExistingJournal.payload.prior_journal_file_sha256 -or
                $compensationPayload.task_state -ne $auxiliary.state -or $compensationPayload.task_xml_sha256 -ne $auxiliary.xml_sha256 -or
                $preExistingJournal.payload.compensation_receipt_sha256 -ne (Get-DawnstrikeSha256File $compensatedPath)) { throw "Compensated rebind receipt does not attest the current task." }
            $existingLockPath = Join-Path $state "locks\dawnstrike-runtime-activation.lock"
            if (Test-Path -LiteralPath $existingLockPath -PathType Leaf) {
                $existingLock = Get-DawnstrikeStrictRuntimeLock $existingLockPath $lockInterpreter.path $lockInterpreter.sha256
                if (-not (Test-DawnstrikeRuntimeLockOwnerDead $existingLock.payload)) { throw "Compensated rebind lock owner is still active." }
                $adoptedCompensationLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal -StateRoot $state -JournalPath $operationJournalPath `
                    -CandidateSha $CandidateSha -CandidateTree ([string]$runtimeContract.tree) -OriginIdentity (Convert-DawnstrikeCanonicalOriginIdentity $origin) `
                    -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
                Exit-DawnstrikeGovernedRuntimeLock -Lock $adoptedCompensationLock
            }
            $existingReceiptHash = Get-DawnstrikeSha256File $receiptFull
            $existingReceiptArchiveRoot = Join-Path $state "receipts\capture-task\archive"
            New-Item -ItemType Directory -Path $existingReceiptArchiveRoot -Force | Out-Null
            $existingReceiptArchive = Join-Path $existingReceiptArchiveRoot ("partial-rebind-" + $existingReceiptHash + ".json")
            if (Test-Path -LiteralPath $existingReceiptArchive) { throw "Partial capture receipt archive already exists." }
            [IO.File]::Move($receiptFull, $existingReceiptArchive)
            if ((Test-Path -LiteralPath $receiptFull) -or (Get-DawnstrikeSha256File $existingReceiptArchive) -ne $existingReceiptHash) { throw "Partial capture receipt archive was not proven." }
            Clear-DawnstrikeCompensatedJournalTombstone -StateRoot $state -JournalPath $operationJournalPath `
                -Operation capture_task_rebind -CandidateSha $CandidateSha -CandidateTree ([string]$runtimeContract.tree) `
                -OriginIdentity (Convert-DawnstrikeCanonicalOriginIdentity $origin) -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
            $compensatedReceiptRecovered = $true
        }
    }
    if (
        [string]$existingPayload.activation_id -eq $activationId -and
        [string]$existingPayload.activation_receipt_name -eq $activationReceiptName -and
        [string]$existingPayload.activation_receipt_sha256 -eq $activationReceiptSha256 -and
        [string]$existingPayload.symbols_manifest_sha256 -eq $SymbolsManifestSha256 -and
        [string]$existingPayload.entitlement_receipt_sha256 -eq $EntitlementReceiptSha256 -and
        [string]$existingPayload.source_config_sha256 -eq $SourceConfigSha256 -and
        [string]$existingPayload.changed_field -eq "candidate_sha_and_input_bindings" -and
        $auxiliary.state -eq "Ready" -and
        $auxiliary.action_contract_sha256 -eq [string]$existingPayload.action_after_sha256 -and
        (Get-DawnstrikeSha256Text ([string]$auxiliary.xml)) -eq [string]$existingPayload.xml_after_sha256
    ) {
        Assert-DawnstrikeCaptureActionTransformation `
            -OriginalXml ([string]$original.xml) -CurrentXml ([string]$auxiliary.xml) `
            -PreviousSha ([string]$existingPayload.previous_candidate_sha) -CandidateSha $CandidateSha `
            -SymbolsManifest $SymbolsManifest -SymbolsManifestSha256 $SymbolsManifestSha256 `
            -EntitlementReceipt $EntitlementReceipt -EntitlementReceiptSha256 $EntitlementReceiptSha256 `
            -SourceConfig $SourceConfig -SourceConfigSha256 $SourceConfigSha256 | Out-Null
        if (-not (Test-Path -LiteralPath $operationJournalPath -PathType Leaf)) {
            throw "Existing COMPLETE capture-task receipt has no durable operation journal."
        }
        $existingJournal = Get-DawnstrikeStrictRuntimeOperationJournal $operationJournalPath $lockInterpreter.path $lockInterpreter.sha256
        if (-not $compensatedReceiptRecovered -and [string]$existingJournal.payload.phase -eq "POST_ENABLE") {
            if (-not (Test-Path -LiteralPath (Join-Path $state "locks\dawnstrike-runtime-activation.lock") -PathType Leaf)) { throw "Sealed capture receipt has no recoverable operation lock." }
            $sealedRecoveryLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal -StateRoot $state -JournalPath $operationJournalPath `
                -CandidateSha $CandidateSha -CandidateTree ([string]$runtimeContract.tree) -OriginIdentity (Convert-DawnstrikeCanonicalOriginIdentity $origin) `
                -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
            $sealedTask = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
            if ($sealedTask.state -ne "Ready" -or $sealedTask.xml_sha256 -ne $existingPayload.xml_after_sha256) { throw "Sealed capture receipt recovery task is not exact Ready." }
            $sealedReceiptHash = Get-DawnstrikeSha256File $receiptFull
            $existingJournal = Set-DawnstrikeRuntimeOperationJournalPhase -StateRoot $state -JournalPath $operationJournalPath `
                -Lock $sealedRecoveryLock -Operation capture_task_rebind -Phase COMPLETE `
                -CandidateSha ([string]$existingJournal.payload.candidate_sha) -CandidateTree ([string]$existingJournal.payload.candidate_tree) `
                -CurrentSha ([string]$existingJournal.payload.current_sha) -CurrentTree ([string]$existingJournal.payload.current_tree) `
                -PreviousSha ([string]$existingJournal.payload.previous_sha) -PreviousTree ([string]$existingJournal.payload.previous_tree) `
                -OriginIdentity ([string]$existingJournal.payload.origin_identity) -PreparedReceiptRelativePath ([string]$existingJournal.payload.prepared_receipt_relative_path) `
                -PreparedReceiptSha256 ([string]$existingJournal.payload.prepared_receipt_sha256) -CompleteReceiptRelativePath $journalCompleteRelativePath `
                -CompleteReceiptSha256 $sealedReceiptHash -BackupContractSha256 ([string]$existingJournal.payload.backup_contract_sha256) `
                -TaskContractSha256 ([string]$existingJournal.payload.task_contract_sha256) -RuntimeStageContractSha256 ([string]$existingJournal.payload.runtime_stage_contract_sha256) `
                -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
            Exit-DawnstrikeGovernedRuntimeLock -Lock $sealedRecoveryLock
            if (Test-Path -LiteralPath $preparedPath -PathType Leaf) { Remove-DawnstrikeCapturePrepared $preparedPath }
            Write-Output ([string]$existingReceipt.Stdout).Trim()
            return
        }
        if (-not $compensatedReceiptRecovered) {
        if (
            [string]$existingJournal.payload.operation -ne "capture_task_rebind" -or
            [string]$existingJournal.payload.phase -ne "COMPLETE" -or
            [string]$existingJournal.payload.candidate_sha -ne $CandidateSha -or
            [string]$existingJournal.payload.candidate_tree -ne [string]$runtimeContract.tree -or
            [string]$existingJournal.payload.complete_receipt_relative_path -ne $journalCompleteRelativePath -or
            [string]$existingJournal.payload.complete_receipt_sha256 -ne (Get-DawnstrikeSha256File $receiptFull)
        ) { throw "Existing capture-task receipt is not bound to a COMPLETE operation journal." }
        $existingLockPath = Join-Path $state "locks\dawnstrike-runtime-activation.lock"
        if (Test-Path -LiteralPath $existingLockPath -PathType Leaf) {
            $existingLock = Get-DawnstrikeStrictRuntimeLock $existingLockPath $lockInterpreter.path $lockInterpreter.sha256
            $existingOrigin = Convert-DawnstrikeCanonicalOriginIdentity $origin
            if ($existingLock.payload.operation -ne "capture_task_rebind" -or
                $existingJournal.payload.lock_token -ne $existingLock.payload.lock_token -or
                $existingJournal.payload.lock_file_sha256 -ne $existingLock.raw_file_sha256 -or
                -not (Test-DawnstrikeRuntimeLockOwnerDead $existingLock.payload)) {
                throw "Existing COMPLETE capture-task lock is live, tampered, or not journal-bound."
            }
            $adoptedCompleteLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal -StateRoot $state `
                -JournalPath $operationJournalPath -CandidateSha $CandidateSha -CandidateTree ([string]$runtimeContract.tree) `
                -OriginIdentity $existingOrigin -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
            # Revalidate the exact Ready task after adoption, then release the
            # stale lock.  A crash before this release is recoverable adoption;
            # a crash after release leaves no lock to strand.
            $postAdopted = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
            if ($postAdopted.state -ne "Ready" -or $postAdopted.xml_sha256 -ne $existingPayload.xml_after_sha256) { throw "Adopted COMPLETE task identity changed." }
            Exit-DawnstrikeGovernedRuntimeLock -Lock $adoptedCompleteLock
            if ($env:DAWNSTRIKE_TEST_REBIND_CRASH_POINT -eq "after_complete_release") {
                if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rebind crash injection is test-only." }
                Stop-Process -Id $PID -Force
            }
        }
        if (Test-Path -LiteralPath $preparedPath -PathType Leaf) { Remove-DawnstrikeCapturePrepared $preparedPath }
        Write-Output ([string]$existingReceipt.Stdout).Trim()
        return
        }
    }
    if (-not $compensatedReceiptRecovered) { throw "Existing capture-task receipt does not match the current task, activation, or supplied input bindings; rebind is ambiguous." }
}

$lockOrigin = Convert-DawnstrikeCanonicalOriginIdentity $origin
$lockRoot = Join-Path $state "locks"
Assert-DawnstrikeNoReparseComponents $lockRoot "Capture-task lock root"
$dailyLocks = @(Get-ChildItem -LiteralPath $lockRoot -Filter "dawnstrike-daily-*.lock" -File -Force -ErrorAction SilentlyContinue)
if ($dailyLocks.Count -ne 0) { throw "A daily run lock exists; capture-task rebind is not permitted." }
if (Test-Path -LiteralPath (Join-Path $state "locks\dawnstrike-runtime-activation.lock") -PathType Leaf) {
    $rebindLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal -StateRoot $state `
        -JournalPath $operationJournalPath -CandidateSha $CandidateSha -CandidateTree ([string]$runtimeContract.tree) `
        -OriginIdentity $lockOrigin -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
}
else {
    $rebindLock = Enter-DawnstrikeGovernedRuntimeLockWithJournal -StateRoot $state `
        -JournalPath $operationJournalPath -Operation capture_task_rebind `
        -CandidateSha $CandidateSha -CandidateTree ([string]$runtimeContract.tree) `
        -CurrentSha $CandidateSha -CurrentTree ([string]$runtimeContract.tree) `
        -PreviousSha ([string]$previousSha) -PreviousTree ([string]$runtimeContract.tree) `
        -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $journalPreparedRelativePath `
        -CompleteReceiptRelativePath $journalCompleteRelativePath -TaskContractSha256 $journalTaskContractSha256 `
        -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
}
$startJournal = Get-DawnstrikeStrictRuntimeOperationJournal $operationJournalPath $lockInterpreter.path $lockInterpreter.sha256
if ($startJournal.payload.phase -eq "COMPENSATED") {
    $compensationReceiptRelativePath = [string]$startJournal.payload.compensation_receipt_relative_path
    if ($compensationReceiptRelativePath -eq "NONE") { throw "Compensated rebind journal has no receipt path." }
    $compensationReceiptPath = Join-Path $state ($compensationReceiptRelativePath.Replace('/', '\'))
    $compensationCheck = & $lockInterpreter.path -I -B (Join-Path $PSScriptRoot "runtime_operation_journal.py") verify-compensation `
        --receipt $compensationReceiptPath --state-root $state 2>$null
    if ($LASTEXITCODE -ne 0) { throw "Compensation receipt failed strict validation." }
    $compensationPayload = (($compensationCheck -join "") | ConvertFrom-Json).payload
    $restoredStart = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
    if ($compensationPayload.operation -ne "capture_task_rebind" -or
        $compensationPayload.candidate_sha -ne $CandidateSha -or $compensationPayload.candidate_tree -ne [string]$runtimeContract.tree -or
        $compensationPayload.task_state -ne $restoredStart.state -or $compensationPayload.task_xml_sha256 -ne $restoredStart.xml_sha256 -or
        $startJournal.payload.compensation_receipt_sha256 -ne (Get-DawnstrikeSha256File $compensationReceiptPath)) {
        throw "Compensated rebind tombstone does not attest the exact restored task."
    }
    if ($rebindLock.acquired) { Exit-DawnstrikeGovernedRuntimeLock -Lock $rebindLock }
    if ($env:DAWNSTRIKE_TEST_REBIND_CRASH_POINT -eq "after_compensated_release") {
        if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rebind crash injection is test-only." }
        Stop-Process -Id $PID -Force
    }
    Clear-DawnstrikeCompensatedJournalTombstone -StateRoot $state -JournalPath $operationJournalPath `
        -Operation capture_task_rebind -CandidateSha $CandidateSha -CandidateTree ([string]$runtimeContract.tree) `
        -OriginIdentity $lockOrigin -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
    $rebindLock = Enter-DawnstrikeGovernedRuntimeLockWithJournal -StateRoot $state `
        -JournalPath $operationJournalPath -Operation capture_task_rebind `
        -CandidateSha $CandidateSha -CandidateTree ([string]$runtimeContract.tree) `
        -CurrentSha $CandidateSha -CurrentTree ([string]$runtimeContract.tree) `
        -PreviousSha ([string]$previousSha) -PreviousTree ([string]$runtimeContract.tree) `
        -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $journalPreparedRelativePath `
        -CompleteReceiptRelativePath $journalCompleteRelativePath -TaskContractSha256 $journalTaskContractSha256 `
        -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
}
try {
    $operationJournal = Get-DawnstrikeStrictRuntimeOperationJournal $operationJournalPath $lockInterpreter.path $lockInterpreter.sha256
    $journalPhase = [string]$operationJournal.payload.phase
    if ($env:DAWNSTRIKE_TEST_REBIND_CRASH_POINT -eq "after_init") {
        if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rebind crash injection is test-only." }
        Stop-Process -Id $PID -Force
    }
    $lockedAuxiliary = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
    if (
        -not $lockedAuxiliary.present -or
        $lockedAuxiliary.state -ne $auxiliary.state -or
        $lockedAuxiliary.xml_sha256 -ne $auxiliary.xml_sha256 -or
        $lockedAuxiliary.action_contract_sha256 -ne $auxiliary.action_contract_sha256 -or
        $lockedAuxiliary.definition_contract_sha256 -ne $auxiliary.definition_contract_sha256
    ) { throw "Auxiliary capture task changed while acquiring the rebind lock." }
    $auxiliary = $lockedAuxiliary
    # A second cooperative process may have observed no receipt before the
    # lock wait and then arrived after the first process sealed COMPLETE.  The
    # post-lock receipt recheck makes that ordinary race idempotent and avoids
    # treating the now-Ready task as an unexplained partial pair.
    if (Test-Path -LiteralPath $receiptFull -PathType Leaf) {
        Assert-DawnstrikeNoReparseComponents $receiptFull "Capture-task receipt"
        $postLockReceipt = Invoke-DawnstrikeActivationProcess $python @(
            $captureContract, "verify-receipt", "--receipt", $receiptFull,
            "--candidate-sha", $CandidateSha, "--candidate-tree", $runtimeContract.tree
        ) $PSScriptRoot "Post-lock capture-task receipt verification" $ProcessTimeoutSeconds
        try { $postLockPayload = [string]$postLockReceipt.Stdout | ConvertFrom-Json }
        catch { throw "Post-lock capture-task receipt verification did not return valid JSON." }
        if (
            [string]$postLockPayload.activation_id -eq $activationId -and
            [string]$postLockPayload.activation_receipt_name -eq $activationReceiptName -and
            [string]$postLockPayload.activation_receipt_sha256 -eq $activationReceiptSha256 -and
            [string]$postLockPayload.symbols_manifest_sha256 -eq $SymbolsManifestSha256 -and
            [string]$postLockPayload.entitlement_receipt_sha256 -eq $EntitlementReceiptSha256 -and
            [string]$postLockPayload.source_config_sha256 -eq $SourceConfigSha256 -and
            [string]$postLockPayload.changed_field -eq "candidate_sha_and_input_bindings" -and
            $auxiliary.state -eq "Ready" -and
            [string]$auxiliary.action_contract_sha256 -eq [string]$postLockPayload.action_after_sha256 -and
            [string]$auxiliary.xml_sha256 -eq [string]$postLockPayload.xml_after_sha256
        ) {
            Assert-DawnstrikeCaptureActionTransformation `
                -OriginalXml ([string]$original.xml) -CurrentXml ([string]$auxiliary.xml) `
                -PreviousSha ([string]$postLockPayload.previous_candidate_sha) -CandidateSha $CandidateSha `
                -SymbolsManifest $SymbolsManifest -SymbolsManifestSha256 $SymbolsManifestSha256 `
                -EntitlementReceipt $EntitlementReceipt -EntitlementReceiptSha256 $EntitlementReceiptSha256 `
                -SourceConfig $SourceConfig -SourceConfigSha256 $SourceConfigSha256 | Out-Null
            Write-Output ([string]$postLockReceipt.Stdout).Trim()
            return
        }
        throw "Post-lock capture-task receipt does not match the current task or supplied input bindings."
    }
    $original = Get-DawnstrikeCaptureOriginalFromActivationBackup $state $activationReceipt.payload
    if (Test-Path -LiteralPath $preparedPath -PathType Leaf) {
        # A crash after Set-ScheduledTask leaves the candidate-bound action on
        # the Disabled task. Validate the immutable hardening receipt itself
        # here; the prepared-chain check below validates the original XML and
        # all input bindings before any compensation or retry mutation.
        $hardeningReceiptRecord = Get-DawnstrikeHardeningReceipt `
            -StateRoot $state -PythonPath $python -ContractPath $hardeningContract `
            -CandidateSha $CandidateSha -CandidateTree ([string]$runtimeContract.tree) `
            -TimeoutSeconds $ProcessTimeoutSeconds
        $hardeningReceipt = $hardeningReceiptRecord.payload
        $hardeningReceipt | Add-Member -NotePropertyName __path -NotePropertyValue $hardeningReceiptRecord.path -Force
    }
    else {
        $hardeningReceipt = Assert-DawnstrikeCaptureHardeningBoundary `
            -Current $auxiliary -ActivationReceipt $activationReceipt.payload -OriginalXml ([string]$original.xml) -StateRoot $state `
            -PythonPath $python -ContractPath $hardeningContract -CandidateSha $CandidateSha `
            -CandidateTree ([string]$runtimeContract.tree) -OriginUrl $origin -RuntimeRoot $runtime -TimeoutSeconds $ProcessTimeoutSeconds
    }
    $resolvedRebindPrincipal = Resolve-DawnstrikeTaskPrincipal -Credential $RunAsCredential
    $principalDocument = [System.Xml.XmlDocument]::new()
    $principalDocument.LoadXml([string]$auxiliary.xml)
    $principalUserNodes = @($principalDocument.SelectNodes("//*[local-name()='Principal']/*[local-name()='UserId']"))
    if ($principalUserNodes.Count -ne 1 -or (Get-DawnstrikePrincipalSid ([string]$principalUserNodes[0].InnerText)) -ne (Get-DawnstrikePrincipalSid $resolvedRebindPrincipal)) {
        throw "RunAsCredential does not match the hardened auxiliary task principal."
    }

    $task = @(Get-ScheduledTask -TaskName $script:DawnstrikeAuxiliaryCaptureTaskName -ErrorAction Stop)
    if ($task.Count -ne 1) { throw "Auxiliary capture task name is not unique." }
    $actions = @($task[0].Actions)
    if ($actions.Count -lt 1) { throw "Auxiliary capture task has no action." }
    $preparedRecord = $null
    if (Test-Path -LiteralPath $preparedPath -PathType Leaf) {
        $preparedRecord = Get-DawnstrikeCapturePreparedRecord `
            -PythonPath $python -ContractPath $captureContract -PreparedPath $preparedPath `
            -CandidateSha $CandidateSha -CandidateTree ([string]$runtimeContract.tree) `
            -TimeoutSeconds $ProcessTimeoutSeconds
        Assert-DawnstrikeCapturePreparedChain `
            -Prepared $preparedRecord -ActivationReceipt $activationReceipt.payload `
            -ActivationReceiptName $activationReceiptName -ActivationReceiptSha256 $activationReceiptSha256 `
            -Original $original -CandidateSha $CandidateSha -CandidateTree ([string]$runtimeContract.tree) `
            -SymbolsManifest $SymbolsManifest -SymbolsManifestSha256 $SymbolsManifestSha256 `
            -EntitlementReceipt $EntitlementReceipt -EntitlementReceiptSha256 $EntitlementReceiptSha256 `
            -SourceConfig $SourceConfig -SourceConfigSha256 $SourceConfigSha256
        if ($auxiliary.state -eq "Ready") {
            Assert-DawnstrikeCaptureActionTransformation `
                -OriginalXml ([string]$original.xml) -CurrentXml ([string]$auxiliary.xml) `
                -PreviousSha ([string]$preparedRecord.previous_candidate_sha) -CandidateSha $CandidateSha `
                -SymbolsManifest $SymbolsManifest -SymbolsManifestSha256 $SymbolsManifestSha256 `
                -EntitlementReceipt $EntitlementReceipt -EntitlementReceiptSha256 $EntitlementReceiptSha256 `
                -SourceConfig $SourceConfig -SourceConfigSha256 $SourceConfigSha256 | Out-Null
            $recoveryPayload = New-DawnstrikeCaptureReceiptPayload `
                -Prepared $preparedRecord -Final $auxiliary -CandidateSha $CandidateSha `
                -CandidateTree ([string]$runtimeContract.tree) -ActivationId $activationId `
                -ActivationReceiptName $activationReceiptName -ActivationReceiptSha256 $activationReceiptSha256 `
                -RuntimeOriginSha256 (Get-DawnstrikeSha256Text $origin) -HardeningReceipt $hardeningReceipt
            $recoveryInput = "$receiptFull.$([guid]::NewGuid().ToString('N')).input.json"
            try {
                Write-DawnstrikeActivationJson $recoveryPayload $recoveryInput
                $sealed = Invoke-DawnstrikeActivationProcess $python @(
                    $captureContract, "seal-receipt", "--input", $recoveryInput, "--output", $receiptFull
                ) $PSScriptRoot "Recovered capture-task receipt sealing" $ProcessTimeoutSeconds
                $recoveryReceiptHash = Get-DawnstrikeSha256File $receiptFull
                $recoveryPreparedHash = Get-DawnstrikeSha256File $preparedPath
                $operationJournal = Set-DawnstrikeRuntimeOperationJournalPhase -StateRoot $state -JournalPath $operationJournalPath `
                    -Lock $rebindLock -Operation capture_task_rebind -Phase COMPLETE `
                    -CandidateSha $CandidateSha -CandidateTree ([string]$runtimeContract.tree) `
                    -CurrentSha $CandidateSha -CurrentTree ([string]$runtimeContract.tree) `
                    -PreviousSha ([string]$preparedRecord.previous_candidate_sha) -PreviousTree ([string]$runtimeContract.tree) `
                    -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $journalPreparedRelativePath `
                    -PreparedReceiptSha256 $recoveryPreparedHash -CompleteReceiptRelativePath $journalCompleteRelativePath `
                    -CompleteReceiptSha256 $recoveryReceiptHash -BackupContractSha256 $recoveryPreparedHash `
                    -TaskContractSha256 $journalTaskContractSha256 -RuntimeStageContractSha256 $journalEmptySha256 `
                    -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
                Remove-DawnstrikeCapturePrepared $preparedPath
                [string]$sealed.Stdout | ConvertFrom-Json | ConvertTo-Json -Depth 8 -Compress
                return
            }
            catch {
                try {
                    $null = Restore-DawnstrikeAuxiliaryCaptureTask `
                        -Expected $original -RuntimeRoot $runtime -StateRoot $state `
                        -RunAsCredential $RunAsCredential
                    $recoveredDisabled = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
                    if (
                        $recoveredDisabled.state -ne "Disabled" -or
                        [string]$recoveredDisabled.xml_sha256 -ne [string]$original.xml_sha256 -or
                        [string]$recoveredDisabled.action_contract_sha256 -ne [string]$original.action_contract_sha256 -or
                        [string]$recoveredDisabled.definition_contract_sha256 -ne [string]$original.definition_contract_sha256
                    ) { throw "Recovered capture task is not the exact Disabled original." }
                }
                catch {
                    throw "Capture-task COMPLETE recovery failed and exact Disabled compensation could not be proven; operator recovery is required."
                }
                throw "Capture-task COMPLETE recovery failed; exact Disabled compensation was proven."
            }
            finally {
                if (Test-Path -LiteralPath $recoveryInput -PathType Leaf) {
                    Remove-Item -LiteralPath $recoveryInput -Force
                }
            }
        }
        if ($auxiliary.state -ne "Disabled") {
            throw "PREPARED capture-task rebind is neither exact Disabled nor an allowed Ready transformation."
        }
        $isOriginalDisabled = (
            [string]$auxiliary.xml_sha256 -eq [string]$original.xml_sha256 -and
            [string]$auxiliary.action_contract_sha256 -eq [string]$original.action_contract_sha256 -and
            [string]$auxiliary.definition_contract_sha256 -eq [string]$original.definition_contract_sha256
        )
        if (-not $isOriginalDisabled) {
            Assert-DawnstrikeCaptureActionTransformation `
                -OriginalXml ([string]$original.xml) -CurrentXml ([string]$auxiliary.xml) `
                -PreviousSha ([string]$preparedRecord.previous_candidate_sha) -CandidateSha $CandidateSha `
                -SymbolsManifest $SymbolsManifest -SymbolsManifestSha256 $SymbolsManifestSha256 `
                -EntitlementReceipt $EntitlementReceipt -EntitlementReceiptSha256 $EntitlementReceiptSha256 `
                -SourceConfig $SourceConfig -SourceConfigSha256 $SourceConfigSha256 | Out-Null
            $null = Restore-DawnstrikeAuxiliaryCaptureTask -Expected $original -RuntimeRoot $runtime -StateRoot $state -RunAsCredential $RunAsCredential
            $auxiliary = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
            if (
                $auxiliary.state -ne "Disabled" -or
                [string]$auxiliary.xml_sha256 -ne [string]$original.xml_sha256 -or
                [string]$auxiliary.action_contract_sha256 -ne [string]$original.action_contract_sha256 -or
                [string]$auxiliary.definition_contract_sha256 -ne [string]$original.definition_contract_sha256
            ) { throw "PREPARED recovery could not restore the exact original Disabled task." }
            $task = @(Get-ScheduledTask -TaskName $script:DawnstrikeAuxiliaryCaptureTaskName -ErrorAction Stop)
            $actions = @($task[0].Actions)
        }
    }
    else {
        if ($auxiliary.state -ne "Disabled") { throw "Auxiliary capture task must be Disabled before exact-SHA rebind." }
        if (
            [string]$auxiliary.xml_sha256 -ne [string]$original.xml_sha256 -or
            [string]$auxiliary.action_contract_sha256 -ne [string]$original.action_contract_sha256 -or
            [string]$auxiliary.definition_contract_sha256 -ne [string]$original.definition_contract_sha256
        ) { throw "Disabled capture task does not match the exact activation-bound original XML." }
        $originalRecords = @(Get-DawnstrikeCaptureActionRecords ([string]$original.xml))
        $previousCandidates = @(
            $originalRecords | ForEach-Object { Get-DawnstrikeCaptureBindingValue ([string]$_.arguments) "candidate-sha" } |
                Where-Object { $null -ne $_ -and $_ -match '^[0-9a-f]{40}$' }
        )
        if ($previousCandidates.Count -ne 1) { throw "Auxiliary capture action candidate SHA pin is missing or ambiguous." }
        $previousSha = [string]$previousCandidates[0]
        $xmlBefore = [string]$original.xml
        $preparedPayload = [ordered]@{
            schema_version = "dawnstrike.capture_task_rebind_prepared.v1"
            status = "PREPARED"
            task_name = $script:DawnstrikeAuxiliaryCaptureTaskName
            candidate_sha = $CandidateSha
            candidate_tree = [string]$runtimeContract.tree
            activation_id = $activationId
            activation_receipt_name = $activationReceiptName
            activation_receipt_sha256 = $activationReceiptSha256
            previous_candidate_sha = $previousSha
            xml_before_sha256 = Get-DawnstrikeSha256Text $xmlBefore
            action_before_sha256 = [string]$original.action_contract_sha256
            definition_before_sha256 = [string]$original.definition_contract_sha256
            normalized_definition_before_sha256 = Get-DawnstrikeCaptureNormalizedDefinitionHash $xmlBefore
            principal_sha256 = Get-DawnstrikeAuxiliarySectionHash $xmlBefore "Principal"
            trigger_sha256 = Get-DawnstrikeAuxiliarySectionHash $xmlBefore "Triggers"
            settings_sha256 = Get-DawnstrikeAuxiliarySectionHash $xmlBefore "Settings"
            symbols_manifest_path = [System.IO.Path]::GetFullPath($SymbolsManifest)
            symbols_manifest_sha256 = $SymbolsManifestSha256
            entitlement_receipt_path = [System.IO.Path]::GetFullPath($EntitlementReceipt)
            entitlement_receipt_sha256 = $EntitlementReceiptSha256
            source_config_path = [System.IO.Path]::GetFullPath($SourceConfig)
            source_config_sha256 = $SourceConfigSha256
            enablement_before = "Disabled"
            compensation = "RESTORE_EXACT_XML_AND_DISABLED"
            prepared_at_utc = [DateTime]::UtcNow.ToString("o")
            research_only = $true
            broker_execution_enabled = $false
        }
        $preparedInput = "$preparedPath.$([guid]::NewGuid().ToString('N')).input.json"
        try {
            Write-DawnstrikeActivationJson $preparedPayload $preparedInput
            $preparedResult = Invoke-DawnstrikeActivationProcess $python @(
                $captureContract, "seal-prepared", "--input", $preparedInput, "--output", $preparedPath
            ) $PSScriptRoot "Capture-task PREPARED record sealing" $ProcessTimeoutSeconds
            $preparedRecord = [string]$preparedResult.Stdout | ConvertFrom-Json
        }
        finally {
            if (Test-Path -LiteralPath $preparedInput -PathType Leaf) {
                Remove-Item -LiteralPath $preparedInput -Force
            }
        }
    }

    if ($null -eq $preparedRecord) { throw "Capture-task rebind did not establish a PREPARED record." }
    if ($auxiliary.state -ne "Disabled") { throw "Capture-task rebind mutation must begin from exact Disabled." }
    $preparedInputValues = @{
        SymbolsManifest = [string]$preparedRecord.symbols_manifest_path
        SymbolsManifestSha256 = [string]$preparedRecord.symbols_manifest_sha256
        EntitlementReceipt = [string]$preparedRecord.entitlement_receipt_path
        EntitlementReceiptSha256 = [string]$preparedRecord.entitlement_receipt_sha256
        SourceConfig = [string]$preparedRecord.source_config_path
        SourceConfigSha256 = [string]$preparedRecord.source_config_sha256
    }
    if (
        [System.IO.Path]::GetFullPath($preparedInputValues.SymbolsManifest) -ne [System.IO.Path]::GetFullPath($SymbolsManifest) -or
        [System.IO.Path]::GetFullPath($preparedInputValues.EntitlementReceipt) -ne [System.IO.Path]::GetFullPath($EntitlementReceipt) -or
        [System.IO.Path]::GetFullPath($preparedInputValues.SourceConfig) -ne [System.IO.Path]::GetFullPath($SourceConfig) -or
        $preparedInputValues.SymbolsManifestSha256.ToLowerInvariant() -ne $SymbolsManifestSha256.ToLowerInvariant() -or
        $preparedInputValues.EntitlementReceiptSha256.ToLowerInvariant() -ne $EntitlementReceiptSha256.ToLowerInvariant() -or
        $preparedInputValues.SourceConfigSha256.ToLowerInvariant() -ne $SourceConfigSha256.ToLowerInvariant()
    ) { throw "Supplied capture inputs do not match the durable PREPARED contract." }

    $inputReceipt = ""
    $receiptSealed = $false
    try {
        if ($journalPhase -eq "INIT") {
            $preparedHash = Get-DawnstrikeSha256File $preparedPath
            $operationJournal = Set-DawnstrikeRuntimeOperationJournalPhase -StateRoot $state -JournalPath $operationJournalPath `
                -Lock $rebindLock -Operation capture_task_rebind -Phase PRE_ENABLE `
                -CandidateSha $CandidateSha -CandidateTree ([string]$runtimeContract.tree) `
                -CurrentSha $CandidateSha -CurrentTree ([string]$runtimeContract.tree) `
                -PreviousSha ([string]$previousSha) -PreviousTree ([string]$runtimeContract.tree) `
                -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $journalPreparedRelativePath `
                -PreparedReceiptSha256 $preparedHash -CompleteReceiptRelativePath $journalCompleteRelativePath `
                -CompleteReceiptSha256 $journalEmptySha256 -BackupContractSha256 $preparedHash `
                -TaskContractSha256 $journalTaskContractSha256 -RuntimeStageContractSha256 $journalEmptySha256 `
                -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
            $journalPhase = "PRE_ENABLE"
        }
        elseif ($journalPhase -notin @("PRE_ENABLE", "POST_ENABLE")) {
            throw "Capture-task rebind journal is not at a recoverable enablement phase."
        }
        $newActions = New-DawnstrikeCaptureReboundActions `
            -Actions $actions -CandidateSha $CandidateSha `
            -SymbolsManifest $SymbolsManifest -SymbolsManifestSha256 $SymbolsManifestSha256 `
            -EntitlementReceipt $EntitlementReceipt -EntitlementReceiptSha256 $EntitlementReceiptSha256 `
            -SourceConfig $SourceConfig -SourceConfigSha256 $SourceConfigSha256
        Set-ScheduledTask -TaskName $script:DawnstrikeAuxiliaryCaptureTaskName `
            -TaskPath ([string]$auxiliary.task_path) -Action $newActions `
            -User $resolvedRebindPrincipal -Password $rebindPassword -ErrorAction Stop | Out-Null
        $boundDisabled = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
        if ($boundDisabled.state -ne "Disabled") { throw "Capture task became enabled before rebind verification." }
        if ($env:DAWNSTRIKE_TEST_REBIND_CRASH_POINT -eq "after_set") {
            if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rebind crash injection is test-only." }
            Stop-Process -Id $PID -Force
        }
        Assert-DawnstrikeCaptureActionTransformation `
            -OriginalXml ([string]$original.xml) -CurrentXml ([string]$boundDisabled.xml) `
            -PreviousSha ([string]$preparedRecord.previous_candidate_sha) -CandidateSha $CandidateSha `
            -SymbolsManifest $SymbolsManifest -SymbolsManifestSha256 $SymbolsManifestSha256 `
            -EntitlementReceipt $EntitlementReceipt -EntitlementReceiptSha256 $EntitlementReceiptSha256 `
            -SourceConfig $SourceConfig -SourceConfigSha256 $SourceConfigSha256 | Out-Null
        if (
            $boundDisabled.action_contract_sha256 -eq [string]$preparedRecord.action_before_sha256 -and
            [string]$preparedRecord.previous_candidate_sha -ne $CandidateSha
        ) {
            throw "Capture-task action SHA was not changed despite a candidate transition."
        }
        $null = Assert-DawnstrikeCaptureHardeningBoundary `
            -Current $boundDisabled -ActivationReceipt $activationReceipt.payload -OriginalXml ([string]$original.xml) -StateRoot $state `
            -PythonPath $python -ContractPath $hardeningContract -CandidateSha $CandidateSha `
            -CandidateTree ([string]$runtimeContract.tree) -OriginUrl $origin -RuntimeRoot $runtime -TimeoutSeconds $ProcessTimeoutSeconds
        if ($InjectFailureAfterMutation) { throw "Injected capture-task post-mutation failure." }
        Enable-ScheduledTask -TaskName $script:DawnstrikeAuxiliaryCaptureTaskName `
            -TaskPath ([string]$boundDisabled.task_path) -ErrorAction Stop | Out-Null
        if ($InjectCrashAfterEnable) { exit 137 }
        $final = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
        if ($final.state -ne "Ready") { throw "Capture task did not become exactly Ready after rebind." }
        Assert-DawnstrikeCaptureActionTransformation `
            -OriginalXml ([string]$original.xml) -CurrentXml ([string]$final.xml) `
            -PreviousSha ([string]$preparedRecord.previous_candidate_sha) -CandidateSha $CandidateSha `
            -SymbolsManifest $SymbolsManifest -SymbolsManifestSha256 $SymbolsManifestSha256 `
            -EntitlementReceipt $EntitlementReceipt -EntitlementReceiptSha256 $EntitlementReceiptSha256 `
            -SourceConfig $SourceConfig -SourceConfigSha256 $SourceConfigSha256 | Out-Null
        $finalDocument = [System.Xml.XmlDocument]::new()
        $finalDocument.LoadXml([string]$final.xml)
        $finalPrincipal = @($finalDocument.SelectNodes("//*[local-name()='Principal']/*[local-name()='UserId']"))
        if ($finalPrincipal.Count -ne 1) { throw "Final capture task principal is ambiguous." }
        $null = Assert-DawnstrikeCaptureTaskSafety -Xml ([string]$final.xml) -RuntimeRoot $runtime -StateRoot $state -ExpectedPrincipal ([string]$finalPrincipal[0].InnerText) -ExpectedCandidateSha $CandidateSha -ExpectedInterpreterPath ([string]$hardeningReceipt.interpreter_path) -ExpectedInterpreterSha256 ([string]$hardeningReceipt.interpreter_sha256) -ExpectedInterpreterSignerThumbprint ([string]$hardeningReceipt.interpreter_signer_thumbprint) -ExpectedEnabled "true" -RequirePasswordPrincipal -RequireRunner
        if ($journalPhase -eq "PRE_ENABLE") {
            $operationJournal = Set-DawnstrikeRuntimeOperationJournalPhase -StateRoot $state -JournalPath $operationJournalPath `
                -Lock $rebindLock -Operation capture_task_rebind -Phase POST_ENABLE `
                -CandidateSha $CandidateSha -CandidateTree ([string]$runtimeContract.tree) `
                -CurrentSha $CandidateSha -CurrentTree ([string]$runtimeContract.tree) `
                -PreviousSha ([string]$previousSha) -PreviousTree ([string]$runtimeContract.tree) `
                -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $journalPreparedRelativePath `
                -PreparedReceiptSha256 (Get-DawnstrikeSha256File $preparedPath) -CompleteReceiptRelativePath $journalCompleteRelativePath `
                -CompleteReceiptSha256 $journalEmptySha256 -BackupContractSha256 (Get-DawnstrikeSha256File $preparedPath) `
                -TaskContractSha256 $journalTaskContractSha256 -RuntimeStageContractSha256 $journalEmptySha256 `
                -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
            $journalPhase = "POST_ENABLE"
        }
        if ($env:DAWNSTRIKE_TEST_REBIND_CRASH_POINT -eq "after_post_enable") {
            if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rebind crash injection is test-only." }
            Stop-Process -Id $PID -Force
        }
        $payload = New-DawnstrikeCaptureReceiptPayload `
            -Prepared $preparedRecord -Final $final -CandidateSha $CandidateSha `
            -CandidateTree ([string]$runtimeContract.tree) -ActivationId $activationId `
            -ActivationReceiptName $activationReceiptName -ActivationReceiptSha256 $activationReceiptSha256 `
            -RuntimeOriginSha256 (Get-DawnstrikeSha256Text $origin) -HardeningReceipt $hardeningReceipt
        $inputReceipt = "$receiptFull.$([guid]::NewGuid().ToString('N')).input.json"
        Write-DawnstrikeActivationJson $payload $inputReceipt
        $result = Invoke-DawnstrikeActivationProcess $python @(
            $captureContract, "seal-receipt", "--input", $inputReceipt, "--output", $receiptFull
        ) $PSScriptRoot "Capture-task rebind receipt sealing" $ProcessTimeoutSeconds
        $receiptSealed = $true
        $completeReceiptHash = Get-DawnstrikeSha256File $receiptFull
        if ($env:DAWNSTRIKE_TEST_REBIND_CRASH_POINT -eq "after_receipt_seal_before_complete") {
            if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rebind crash injection is test-only." }
            Stop-Process -Id $PID -Force
        }
        $preparedHash = Get-DawnstrikeSha256File $preparedPath
        $operationJournal = Set-DawnstrikeRuntimeOperationJournalPhase -StateRoot $state -JournalPath $operationJournalPath `
            -Lock $rebindLock -Operation capture_task_rebind -Phase COMPLETE `
            -CandidateSha $CandidateSha -CandidateTree ([string]$runtimeContract.tree) `
            -CurrentSha $CandidateSha -CurrentTree ([string]$runtimeContract.tree) `
            -PreviousSha ([string]$previousSha) -PreviousTree ([string]$runtimeContract.tree) `
            -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $journalPreparedRelativePath `
            -PreparedReceiptSha256 $preparedHash -CompleteReceiptRelativePath $journalCompleteRelativePath `
            -CompleteReceiptSha256 $completeReceiptHash -BackupContractSha256 $preparedHash `
            -TaskContractSha256 $journalTaskContractSha256 -RuntimeStageContractSha256 $journalEmptySha256 `
            -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
        $journalPhase = "COMPLETE"
        if ($env:DAWNSTRIKE_TEST_REBIND_CRASH_POINT -eq "after_complete") {
            if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rebind crash injection is test-only." }
            Stop-Process -Id $PID -Force
        }
        Remove-DawnstrikeCapturePrepared $preparedPath
        [string]$result.Stdout | ConvertFrom-Json | ConvertTo-Json -Depth 8 -Compress
    }
    catch {
        $failure = $_
        try {
            $null = Restore-DawnstrikeAuxiliaryCaptureTask -Expected $original -RuntimeRoot $runtime -StateRoot $state -RunAsCredential $RunAsCredential
            $restored = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
            if (
                $restored.state -ne "Disabled" -or
                [string]$restored.xml_sha256 -ne [string]$original.xml_sha256 -or
                [string]$restored.definition_contract_sha256 -ne [string]$original.definition_contract_sha256 -or
                [string]$restored.action_contract_sha256 -ne [string]$original.action_contract_sha256
            ) { throw "Capture-task compensation did not restore the exact disabled task." }
            $runtimeRestored = Get-DawnstrikeGitContract $git $runtime $ProcessTimeoutSeconds
            if ($runtimeRestored.head -ne $CandidateSha -or $runtimeRestored.tree -ne [string]$runtimeContract.tree) { throw "Compensation runtime identity changed." }
            $failureEvidence = [ordered]@{
                schema_version = "dawnstrike.capture_task_rebind_failure.v1"
                status = "FAILED_RESTORED_EXACT_DISABLED"
                task_name = $script:DawnstrikeAuxiliaryCaptureTaskName
                candidate_sha = $CandidateSha
                candidate_tree = [string]$runtimeContract.tree
                activation_id = $activationId
                activation_receipt_name = $activationReceiptName
                activation_receipt_sha256 = $activationReceiptSha256
                original_xml_sha256 = [string]$original.xml_sha256
                original_action_sha256 = [string]$original.action_contract_sha256
                recovery_evidence = "EXACT_XML_RESTORED_AND_DISABLED"
                error_type = $failure.Exception.GetType().Name
                research_only = $true
                broker_execution_enabled = $false
            }
            Write-DawnstrikeActivationJson $failureEvidence $failurePath
            $journalBefore = Get-DawnstrikeStrictRuntimeOperationJournal $operationJournalPath $lockInterpreter.path $lockInterpreter.sha256
            if ([string]$journalBefore.payload.phase -in @("COMPLETE", "COMPENSATED")) { throw "Compensation cannot replace a terminal operation journal." }
            $empty = Get-DawnstrikeSha256Text ""
            $compensationReceiptRelativePath = "receipts/capture-task/capture-task-rebind-$([string]$journalBefore.raw_file_sha256).compensated.json"
            $compensationReceiptPath = Join-Path $state ($compensationReceiptRelativePath.Replace('/', '\'))
            $priorReceiptRelative = "NONE"
            $priorReceiptHash = $empty
            if (Test-Path -LiteralPath $receiptFull -PathType Leaf) {
                $priorReceiptHash = Get-DawnstrikeSha256File $receiptFull
                $priorReceiptRelative = "receipts/capture-task/archive/partial-rebind-$priorReceiptHash.json"
                $priorReceiptArchiveRoot = Join-Path $state "receipts\capture-task\archive"
                New-Item -ItemType Directory -Path $priorReceiptArchiveRoot -Force | Out-Null
                $priorReceiptArchive = Join-Path $state ($priorReceiptRelative.Replace('/', '\'))
                if (Test-Path -LiteralPath $priorReceiptArchive) { throw "Partial capture receipt archive already exists." }
                [IO.File]::Move($receiptFull, $priorReceiptArchive)
                if ((Test-Path -LiteralPath $receiptFull) -or (Get-DawnstrikeSha256File $priorReceiptArchive) -ne $priorReceiptHash) { throw "Partial capture receipt archive was not proven." }
            }
            $compensationPayload = [ordered]@{
                schema_version = "dawnstrike.runtime_compensation_receipt.v1"
                status = "COMPENSATED"
                operation = "capture_task_rebind"
                candidate_sha = $CandidateSha
                candidate_tree = [string]$runtimeContract.tree
                prior_journal_file_sha256 = [string]$journalBefore.raw_file_sha256
                task_contract_sha256 = $journalTaskContractSha256
                task_state = [string]$restored.state
                task_xml_sha256 = [string]$restored.xml_sha256
                task_action_contract_sha256 = [string]$restored.action_contract_sha256
                task_definition_contract_sha256 = [string]$restored.definition_contract_sha256
                prior_receipt_relative_path = $priorReceiptRelative
                prior_receipt_sha256 = $priorReceiptHash
                failure_type = $failure.Exception.GetType().Name
                research_only = $true
                broker_execution_enabled = $false
            }
            $compensationInput = "$compensationReceiptPath.$([guid]::NewGuid().ToString('N')).input.json"
            try {
                Write-DawnstrikeActivationJson $compensationPayload $compensationInput
                & $lockInterpreter.path -I -B (Join-Path $PSScriptRoot "runtime_operation_journal.py") seal-compensation `
                    --input $compensationInput --output $compensationReceiptPath --state-root $state 2>$null | Out-Null
                if ($LASTEXITCODE -ne 0) { throw "Compensation receipt strict sealing failed." }
            } finally { if (Test-Path -LiteralPath $compensationInput) { Remove-Item -LiteralPath $compensationInput -Force } }
            $compensationHash = Get-DawnstrikeSha256File $compensationReceiptPath
            $operationJournal = Set-DawnstrikeRuntimeOperationJournalPhase -StateRoot $state -JournalPath $operationJournalPath `
                -Lock $rebindLock -Operation capture_task_rebind -Phase COMPENSATED `
                -CandidateSha ([string]$journalBefore.payload.candidate_sha) -CandidateTree ([string]$journalBefore.payload.candidate_tree) `
                -CurrentSha ([string]$journalBefore.payload.current_sha) -CurrentTree ([string]$journalBefore.payload.current_tree) `
                -PreviousSha ([string]$journalBefore.payload.previous_sha) -PreviousTree ([string]$journalBefore.payload.previous_tree) `
                -OriginIdentity ([string]$journalBefore.payload.origin_identity) -PreparedReceiptRelativePath ([string]$journalBefore.payload.prepared_receipt_relative_path) `
                -PreparedReceiptSha256 ([string]$journalBefore.payload.prepared_receipt_sha256) -CompleteReceiptRelativePath ([string]$journalBefore.payload.complete_receipt_relative_path) `
                -CompleteReceiptSha256 ([string]$journalBefore.payload.complete_receipt_sha256) -BackupContractSha256 ([string]$journalBefore.payload.backup_contract_sha256) `
                -TaskContractSha256 ([string]$journalBefore.payload.task_contract_sha256) -RuntimeStageContractSha256 ([string]$journalBefore.payload.runtime_stage_contract_sha256) `
                -CompensationReceiptRelativePath $compensationReceiptRelativePath -CompensationReceiptSha256 $compensationHash `
                -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
            $journalPhase = "COMPENSATED"
            if ($env:DAWNSTRIKE_TEST_REBIND_CRASH_POINT -eq "after_compensated_before_release") {
                if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rebind crash injection is test-only." }
                Stop-Process -Id $PID -Force
            }
        }
        catch {
            throw "Capture-task rebind failed and exact disabled XML compensation could not be proven; operator recovery is required."
        }
        throw "Capture-task rebind failed; exact disabled XML compensation was proven."
    }
    finally {
        if ($inputReceipt -and (Test-Path -LiteralPath $inputReceipt -PathType Leaf)) {
            Remove-Item -LiteralPath $inputReceipt -Force
        }
    }
}
finally {
    if ($rebindLock -and $rebindLock.acquired -and $journalPhase -notin @("COMPLETE", "COMPENSATED")) {
        throw "Capture-task rebind lock and nonterminal journal are retained for governed recovery."
    }
    Exit-DawnstrikeGovernedRuntimeLock $rebindLock
    if (Test-Path -LiteralPath $rebindLock.path -PathType Leaf) {
        throw "Capture-task rebind lock could not be released; operator recovery is required."
    }
    if ($journalPhase -eq "COMPENSATED") {
        if ($env:DAWNSTRIKE_TEST_REBIND_CRASH_POINT -eq "after_compensated_release") {
            if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rebind crash injection is test-only." }
            Stop-Process -Id $PID -Force
        }
        Clear-DawnstrikeCompensatedJournalTombstone -StateRoot $state -JournalPath $operationJournalPath `
            -Operation capture_task_rebind -CandidateSha $CandidateSha -CandidateTree ([string]$runtimeContract.tree) `
            -OriginIdentity $lockOrigin -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
    }
}
