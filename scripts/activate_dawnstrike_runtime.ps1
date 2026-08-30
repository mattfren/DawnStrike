[CmdletBinding()]
param(
    [ValidatePattern('^$|^[0-9a-f]{40}$')][string]$ExpectedSha = "",
    [ValidatePattern('^$|^\d{4}-\d{2}-\d{2}$')][string]$MarketDate = "",
    [string]$CiEvidencePath = "",
    [string]$SolEvidencePath = "",
    [string]$CandidateRoot = "",
    [string]$RuntimeRoot = "C:\r\dawnstrike-runtime",
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [string]$BackupRoot = "C:\r\dawnstrike-state-backups",
    [ValidateRange(1, 120)][int]$BackupRetention = 30,
    [ValidateRange(30, 1800)][int]$ProcessTimeoutSeconds = 300,
    [switch]$PreflightOnly
)

$ErrorActionPreference = "Stop"

$script:DawnstrikeCanonicalTaskNames = @(
    "Dawnstrike AlphaOps Morning",
    "Dawnstrike AlphaOps Monitor 5m",
    "Dawnstrike AlphaOps EOD Full Report",
    "Dawnstrike AlphaOps V6 Weekly Training",
    "Dawnstrike 10of10 Daily Finalize"
)

function Get-DawnstrikeSha256Text {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Text)

    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Get-DawnstrikeSha256File {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = $null
    try {
        $stream = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::Read
        )
        return ([System.BitConverter]::ToString($sha.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
        $sha.Dispose()
    }
}

function Resolve-DawnstrikeActivationRoot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (-not $item.PSIsContainer) {
        throw "$Label must be an existing directory."
    }
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label cannot be a reparse point."
    }
    return $item.FullName.TrimEnd('\')
}

function Get-DawnstrikeFutureActivationRoot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if ($Path -notmatch '^[A-Za-z]:\\') {
        throw "$Label must be an absolute drive-qualified directory."
    }
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $pathRoot = [System.IO.Path]::GetPathRoot($fullPath)
    if ([string]::IsNullOrWhiteSpace($pathRoot)) {
        throw "$Label does not have a valid filesystem root."
    }
    if ($fullPath.Length -gt $pathRoot.Length) {
        $fullPath = $fullPath.TrimEnd('\')
    }
    $missing = New-Object System.Collections.Generic.List[string]
    $cursor = $fullPath
    while (-not (Test-Path -LiteralPath $cursor -PathType Container)) {
        $missing.Add($cursor)
        $parent = Split-Path -Parent $cursor
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $cursor) {
            throw "$Label parent directory does not exist."
        }
        $cursor = $parent.TrimEnd('\')
    }
    $null = Resolve-DawnstrikeActivationRoot $cursor $Label
    return $fullPath
}

function Ensure-DawnstrikeActivationRoot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $fullPath = Get-DawnstrikeFutureActivationRoot $Path $Label
    if (-not (Test-Path -LiteralPath $fullPath -PathType Container)) {
        New-Item -ItemType Directory -Path $fullPath -ErrorAction Stop | Out-Null
    }
    return Resolve-DawnstrikeActivationRoot $fullPath $Label
}

function Assert-DawnstrikeRootIsolation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string[]]$OtherPaths,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if ($Path -notmatch '^[A-Za-z]:\\') {
        throw "$Label must be an absolute drive-qualified directory."
    }
    $candidate = [System.IO.Path]::GetFullPath($Path).TrimEnd('\') + '\'
    foreach ($otherPath in $OtherPaths) {
        $other = [System.IO.Path]::GetFullPath($otherPath).TrimEnd('\') + '\'
        if (
            [string]::Equals($candidate, $other, [System.StringComparison]::OrdinalIgnoreCase) -or
            $candidate.StartsWith($other, [System.StringComparison]::OrdinalIgnoreCase) -or
            $other.StartsWith($candidate, [System.StringComparison]::OrdinalIgnoreCase)
        ) {
            throw "$Label must be separate from candidate, runtime, and state roots."
        }
    }
}

function Assert-DawnstrikeSafeOrigin {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Origin)

    if ([string]::IsNullOrWhiteSpace($Origin)) {
        throw "Candidate origin is missing."
    }
    $lower = $Origin.ToLowerInvariant()
    if (
        $lower -match '(gh[pousr]_|oauth|password|access[_-]?token|private[_-]?key)' -or
        $Origin.Contains("?") -or
        $Origin.Contains("#")
    ) {
        throw "Candidate origin contains forbidden credential-like material."
    }
    if ($lower -match '^https?://[^/]*@') {
        throw "Candidate HTTPS origin cannot contain user information."
    }
}

function Invoke-DawnstrikeActivationProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter()][string[]]$ArgumentList = @(),
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $result = Invoke-DawnstrikeJobProcess `
        -FilePath $FilePath `
        -ArgumentList $ArgumentList `
        -WorkingDirectory $WorkingDirectory `
        -Label $Label `
        -TimeoutSeconds $TimeoutSeconds `
        -OutputDrainTimeoutSeconds 5 `
        -EnvironmentOverrides @{ PYTHONDONTWRITEBYTECODE = "1" }
    if ($result.ExitCode -ne 0) {
        # Do not echo native stderr. Remote helpers and environment-specific
        # tooling may include authentication material in their diagnostics.
        throw "$Label failed with exit code $($result.ExitCode)."
    }
    return $result
}

function Get-DawnstrikeGitValue {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $result = Invoke-DawnstrikeActivationProcess `
        -FilePath $GitPath `
        -ArgumentList (@("-C", $Root) + $Arguments) `
        -WorkingDirectory $Root `
        -Label $Label `
        -TimeoutSeconds $TimeoutSeconds
    return ([string]$result.Stdout).Trim()
}

function Get-DawnstrikeGitContract {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [string]$ExpectedCommit = ""
    )

    $gitDirectory = Join-Path $Root ".git"
    if (-not (Test-Path -LiteralPath $gitDirectory -PathType Container)) {
        throw "Runtime activation accepts only a self-contained Git checkout."
    }
    $top = Get-DawnstrikeGitValue $GitPath $Root @("rev-parse", "--show-toplevel") "Git root verification" $TimeoutSeconds
    if (-not [string]::Equals(
        [System.IO.Path]::GetFullPath($top).TrimEnd('\'),
        [System.IO.Path]::GetFullPath($Root).TrimEnd('\'),
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Git checkout root does not match the requested activation root."
    }
    $head = (Get-DawnstrikeGitValue $GitPath $Root @("rev-parse", "HEAD") "Git HEAD verification" $TimeoutSeconds).ToLowerInvariant()
    $tree = (Get-DawnstrikeGitValue $GitPath $Root @("rev-parse", "HEAD^{tree}") "Git tree verification" $TimeoutSeconds).ToLowerInvariant()
    if ($head -notmatch '^[0-9a-f]{40}$' -or $tree -notmatch '^[0-9a-f]{40}$') {
        throw "Git checkout identity is invalid."
    }
    if ($ExpectedCommit -and $head -ne $ExpectedCommit) {
        throw "Git checkout HEAD does not equal the expected release SHA."
    }
    $status = Get-DawnstrikeGitValue $GitPath $Root @("status", "--porcelain=v1", "--untracked-files=all") "Git cleanliness verification" $TimeoutSeconds
    if ($status) {
        throw "Git checkout is not clean."
    }
    $ignored = Get-DawnstrikeGitValue $GitPath $Root @("ls-files", "--others", "--ignored", "--exclude-standard", "-z") "Ignored runtime artifact verification" $TimeoutSeconds
    $forbiddenIgnored = @(
        ([string]$ignored).Split([char]0, [System.StringSplitOptions]::RemoveEmptyEntries) |
            Where-Object {
                $name = [System.IO.Path]::GetFileName($_).ToLowerInvariant()
                $extension = [System.IO.Path]::GetExtension($_).ToLowerInvariant()
                $extension -in @(
                    ".ps1", ".psm1", ".py", ".pyc", ".pyd", ".dll", ".exe",
                    ".com", ".bat", ".cmd", ".sh", ".pth"
                ) -or $name -in @("sitecustomize.py", "usercustomize.py")
            }
    )
    if ($forbiddenIgnored.Count -gt 0) {
        throw "Git checkout contains ignored executable or Python-startup artifacts."
    }
    return [pscustomobject]@{ head = $head; tree = $tree }
}

function Get-DawnstrikeTaskDefinitionText {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Xml)

    try {
        $document = [System.Xml.XmlDocument]::new()
        $document.PreserveWhitespace = $true
        $document.LoadXml($Xml)
        $namespace = [string]$document.DocumentElement.NamespaceURI
        if ([string]::IsNullOrWhiteSpace($namespace)) {
            $enabledNodes = @($document.SelectNodes("/Task/Settings/Enabled"))
        }
        else {
            $manager = [System.Xml.XmlNamespaceManager]::new($document.NameTable)
            $manager.AddNamespace("task", $namespace)
            $enabledNodes = @($document.SelectNodes("/task:Task/task:Settings/task:Enabled", $manager))
        }
        if ($enabledNodes.Count -ne 1) {
            throw "Task XML must contain exactly one Settings/Enabled element."
        }
        $enabledNodes[0].InnerText = "DAWNSTRIKE_ENABLEMENT_STATE"
        return [string]$document.OuterXml
    }
    catch {
        throw "Canonical task XML cannot produce an enablement-independent definition contract."
    }
}

function Get-DawnstrikeTaskContract {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [switch]$AllowDisabled
    )

    $records = @()
    $definitionRecords = @()
    $actionRecords = @()
    $enabledCount = 0
    $disabledCount = 0
    foreach ($taskName in $script:DawnstrikeCanonicalTaskNames) {
        $matches = @(Get-ScheduledTask -TaskName $taskName -ErrorAction Stop)
        if ($matches.Count -ne 1) {
            throw "Canonical Dawnstrike task name is not unique: $taskName"
        }
        $task = $matches[0]
        $state = [string]$task.State
        if ($state -eq "Ready") {
            $enabledCount += 1
        }
        elseif ($AllowDisabled -and $state -eq "Disabled") {
            $disabledCount += 1
        }
        else {
            throw "Canonical Dawnstrike task is not in an approved exact state: $taskName state=$state"
        }
        $actions = @($task.Actions)
        if ($actions.Count -lt 1) {
            throw "Canonical Dawnstrike task has no action: $taskName"
        }
        $actionText = ($actions | ForEach-Object {
            "{0}|{1}|{2}" -f $_.Execute, $_.Arguments, $_.WorkingDirectory
        }) -join "`n"
        $lower = $actionText.ToLowerInvariant()
        if (
            -not $lower.Contains($RuntimeRoot.ToLowerInvariant()) -or
            -not $lower.Contains($StateRoot.ToLowerInvariant())
        ) {
            throw "Canonical Dawnstrike task does not retain the fixed runtime/state roots: $taskName"
        }
        $taskPath = [string]$task.TaskPath
        if ([string]::IsNullOrWhiteSpace($taskPath)) { $taskPath = "\" }
        $xml = [string](Export-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop)
        if ([string]::IsNullOrWhiteSpace($xml)) {
            throw "Canonical Dawnstrike task export is empty: $taskName"
        }
        $records += "$taskName`0$(Get-DawnstrikeSha256Text $xml)`n"
        $definition = Get-DawnstrikeTaskDefinitionText $xml
        $definitionRecords += "$taskName`0$(Get-DawnstrikeSha256Text $definition)`n"
        $actionRecords += "$taskName`0$taskPath`0$actionText`n"
    }
    return [pscustomobject]@{
        task_count = $script:DawnstrikeCanonicalTaskNames.Count
        task_contract_sha256 = Get-DawnstrikeSha256Text ($records -join "")
        task_definition_contract_sha256 = Get-DawnstrikeSha256Text ($definitionRecords -join "")
        task_action_contract_sha256 = Get-DawnstrikeSha256Text ($actionRecords -join "")
        enabled_count = $enabledCount
        disabled_count = $disabledCount
    }
}

function Write-DawnstrikeTaskXmlFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Xml,
        [Parameter(Mandatory = $true)][string]$Path
    )

    try {
        $document = [System.Xml.XmlDocument]::new()
        $document.PreserveWhitespace = $true
        $document.LoadXml($Xml)
        $declaration = @(
            $document.ChildNodes |
                Where-Object { $_.NodeType -eq [System.Xml.XmlNodeType]::XmlDeclaration }
        )
        if ($declaration.Count -gt 1) {
            throw "Task XML has multiple declarations."
        }
        $declaredEncoding = if ($declaration.Count -eq 1) {
            ([string]$declaration[0].Encoding).ToLowerInvariant()
        }
        else {
            ""
        }
        if ($declaredEncoding -in @("utf-16", "unicode", "utf-16le")) {
            $encoding = [System.Text.Encoding]::Unicode
            $encodingLabel = "utf-16le-bom"
        }
        elseif ($declaredEncoding -in @("", "utf-8")) {
            $encoding = [System.Text.UTF8Encoding]::new($false)
            $encodingLabel = "utf-8"
        }
        else {
            throw "Task XML declares an unsupported encoding."
        }
        [System.IO.File]::WriteAllText($Path, $Xml, $encoding)
        return $encodingLabel
    }
    catch {
        throw "Canonical task XML cannot be persisted with its declared encoding."
    }
}

function New-DawnstrikeTaskXmlBackup {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$BackupName,
        [Parameter(Mandatory = $true)][string]$ActivationId,
        [Parameter(Mandatory = $true)][object]$TaskContract
    )

    if ($BackupName -notmatch '^runtime-(activation|rollback)-[0-9a-f]{24}$') {
        throw "Scheduler backup name is invalid."
    }
    $root = Join-Path $StateRoot "scheduler-backups"
    if (Test-Path -LiteralPath $root) {
        $rootItem = Get-Item -LiteralPath $root -Force -ErrorAction Stop
        if (
            -not $rootItem.PSIsContainer -or
            ($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
        ) {
            throw "Scheduler backup root is not a safe directory."
        }
    }
    else {
        New-Item -ItemType Directory -Path $root -ErrorAction Stop | Out-Null
    }
    $final = Join-Path $root $BackupName
    if (Test-Path -LiteralPath $final) {
        throw "Scheduler XML backup already exists and requires review."
    }
    $temporary = Join-Path $root (".incomplete-$BackupName-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $temporary -ErrorAction Stop | Out-Null
    try {
        $entries = @()
        foreach ($taskName in $script:DawnstrikeCanonicalTaskNames) {
            $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
            if ([string]$task.State -ne "Ready") {
                throw "Scheduler XML backup requires every canonical task to be exactly Ready."
            }
            $taskPath = [string]$task.TaskPath
            if ([string]::IsNullOrWhiteSpace($taskPath)) { $taskPath = "\" }
            $xml = [string](Export-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop)
            $safeName = ($taskName -replace '[^A-Za-z0-9_.-]', '_') + ".xml"
            $xmlPath = Join-Path $temporary $safeName
            $xmlEncoding = Write-DawnstrikeTaskXmlFile $xml $xmlPath
            $entries += [ordered]@{
                task_name = $taskName
                task_path = $taskPath
                enabled = $true
                file_name = $safeName
                xml_encoding = $xmlEncoding
                xml_sha256 = Get-DawnstrikeSha256Text $xml
                xml_file_sha256 = Get-DawnstrikeSha256File $xmlPath
            }
        }
        $manifest = [ordered]@{
            schema_version = "dawnstrike.scheduler_xml_backup.v1"
            activation_id = $ActivationId
            created_at_utc = [DateTime]::UtcNow.ToString("o")
            task_count = [int]$TaskContract.task_count
            task_contract_sha256 = [string]$TaskContract.task_contract_sha256
            task_definition_contract_sha256 = [string]$TaskContract.task_definition_contract_sha256
            task_action_contract_sha256 = [string]$TaskContract.task_action_contract_sha256
            tasks = $entries
            research_only = $true
            broker_execution_enabled = $false
        }
        $manifestPath = Join-Path $temporary "manifest.json"
        Write-DawnstrikeActivationJson $manifest $manifestPath
        [System.IO.Directory]::Move($temporary, $final)
        $finalManifest = Join-Path $final "manifest.json"
        $result = [pscustomobject]@{
            backup_name = $BackupName
            backup_path = $final
            manifest_sha256 = Get-DawnstrikeSha256File $finalManifest
        }
        $null = Assert-DawnstrikeTaskXmlBackup `
            -StateRoot $StateRoot `
            -BackupName $result.backup_name `
            -ExpectedManifestSha256 $result.manifest_sha256 `
            -ExpectedTaskContractSha256 ([string]$TaskContract.task_contract_sha256) `
            -ExpectedTaskDefinitionContractSha256 ([string]$TaskContract.task_definition_contract_sha256) `
            -ExpectedTaskActionContractSha256 ([string]$TaskContract.task_action_contract_sha256)
        return $result
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Container) {
            Remove-Item -LiteralPath $temporary -Recurse -Force
        }
    }
}

function Assert-DawnstrikeTaskXmlBackup {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$BackupName,
        [Parameter(Mandatory = $true)][string]$ExpectedManifestSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedTaskContractSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedTaskDefinitionContractSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedTaskActionContractSha256
    )

    if ($BackupName -notmatch '^runtime-(activation|rollback)-[0-9a-f]{24}$') {
        throw "Scheduler backup name is invalid."
    }
    foreach ($hash in @(
        $ExpectedManifestSha256,
        $ExpectedTaskContractSha256,
        $ExpectedTaskDefinitionContractSha256,
        $ExpectedTaskActionContractSha256
    )) {
        if ($hash -notmatch '^[0-9a-f]{64}$') {
            throw "Scheduler backup expected hash is invalid."
        }
    }
    $backupPath = Join-Path $StateRoot "scheduler-backups\$BackupName"
    $backupItem = Get-Item -LiteralPath $backupPath -Force -ErrorAction Stop
    if (
        -not $backupItem.PSIsContainer -or
        ($backupItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
        throw "Scheduler XML backup is not a safe directory."
    }
    $manifestPath = Join-Path $backupPath "manifest.json"
    $manifestItem = Get-Item -LiteralPath $manifestPath -Force -ErrorAction Stop
    if (
        $manifestItem.PSIsContainer -or
        ($manifestItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
        (Get-DawnstrikeSha256File $manifestPath) -ne $ExpectedManifestSha256
    ) {
        throw "Scheduler XML backup manifest does not match its receipt-bound hash."
    }
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "Scheduler XML backup manifest is invalid JSON."
    }
    if (
        [string]$manifest.schema_version -ne "dawnstrike.scheduler_xml_backup.v1" -or
        [string]$manifest.activation_id -ne $BackupName.Substring($BackupName.Length - 24) -or
        [int]$manifest.task_count -ne 5 -or
        [string]$manifest.task_contract_sha256 -ne $ExpectedTaskContractSha256 -or
        [string]$manifest.task_definition_contract_sha256 -ne
            $ExpectedTaskDefinitionContractSha256 -or
        [string]$manifest.task_action_contract_sha256 -ne $ExpectedTaskActionContractSha256 -or
        $manifest.research_only -ne $true -or
        $manifest.broker_execution_enabled -ne $false
    ) {
        throw "Scheduler XML backup manifest violates the exact safety contract."
    }
    $entries = @($manifest.tasks)
    if ($entries.Count -ne $script:DawnstrikeCanonicalTaskNames.Count) {
        throw "Scheduler XML backup does not contain exactly five tasks."
    }
    $expectedChildren = @("manifest.json") + @($entries | ForEach-Object { [string]$_.file_name })
    $actualChildren = @(Get-ChildItem -LiteralPath $backupPath -Force)
    if (
        $actualChildren.Count -ne $expectedChildren.Count -or
        @($actualChildren | Where-Object { $_.PSIsContainer }).Count -ne 0 -or
        @(Compare-Object `
            ($expectedChildren | Sort-Object) `
            (@($actualChildren.Name) | Sort-Object)
        ).Count -ne 0
    ) {
        throw "Scheduler XML backup contains unexpected files or directories."
    }
    $records = @()
    $definitionRecords = @()
    for ($index = 0; $index -lt $entries.Count; $index += 1) {
        $entry = $entries[$index]
        $expectedName = $script:DawnstrikeCanonicalTaskNames[$index]
        $expectedFileName = ($expectedName -replace '[^A-Za-z0-9_.-]', '_') + ".xml"
        if (
            [string]$entry.task_name -ne $expectedName -or
            $entry.enabled -ne $true -or
            [string]$entry.file_name -ne $expectedFileName -or
            [string]$entry.xml_encoding -notin @("utf-8", "utf-16le-bom") -or
            [string]$entry.xml_sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]$entry.xml_file_sha256 -notmatch '^[0-9a-f]{64}$'
        ) {
            throw "Scheduler XML backup task entry violates the exact contract."
        }
        $xmlPath = Join-Path $backupPath $expectedFileName
        $xmlItem = Get-Item -LiteralPath $xmlPath -Force -ErrorAction Stop
        if (
            $xmlItem.PSIsContainer -or
            ($xmlItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
            (Get-DawnstrikeSha256File $xmlPath) -ne [string]$entry.xml_file_sha256
        ) {
            throw "Scheduler XML backup task file does not match its manifest."
        }
        $xml = [System.IO.File]::ReadAllText($xmlPath)
        if ((Get-DawnstrikeSha256Text $xml) -ne [string]$entry.xml_sha256) {
            throw "Scheduler XML backup task text does not match its manifest."
        }
        $records += "$expectedName`0$([string]$entry.xml_sha256)`n"
        $definition = Get-DawnstrikeTaskDefinitionText $xml
        $definitionRecords += "$expectedName`0$(Get-DawnstrikeSha256Text $definition)`n"
    }
    if ((Get-DawnstrikeSha256Text ($records -join "")) -ne $ExpectedTaskContractSha256) {
        throw "Scheduler XML backup files do not reproduce the task contract hash."
    }
    if (
        (Get-DawnstrikeSha256Text ($definitionRecords -join "")) -ne
            $ExpectedTaskDefinitionContractSha256
    ) {
        throw "Scheduler XML backup files do not reproduce the task definition hash."
    }
}

function Assert-DawnstrikeReceiptRecoveryArtifacts {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Receipt,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$BackupRoot,
        [Parameter(Mandatory = $true)][string]$ToolRoot,
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [switch]$RequireRollbackCheckout
    )

    $activationId = [string]$Receipt.activation_id
    if ($activationId -notmatch '^[0-9a-f]{24}$') {
        throw "Recovery artifact activation id is invalid."
    }
    $backup = Resolve-DawnstrikeActivationRoot $BackupRoot "BackupRoot"
    $rollbackRoot = Join-Path $StateRoot "runtime-rollbacks\$activationId"
    $rollbackRootItem = Get-Item -LiteralPath $rollbackRoot -Force -ErrorAction Stop
    if (
        -not $rollbackRootItem.PSIsContainer -or
        ($rollbackRootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
        throw "Receipt-bound rollback root is missing or unsafe."
    }
    $bundle = Join-Path $rollbackRoot "previous-runtime.bundle"
    $bundleItem = Get-Item -LiteralPath $bundle -Force -ErrorAction Stop
    if (
        $bundleItem.PSIsContainer -or
        ($bundleItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
        (Get-DawnstrikeSha256File $bundle) -ne [string]$Receipt.rollback_bundle_sha256
    ) {
        throw "Receipt-bound rollback bundle is missing or invalid."
    }
    $null = Invoke-DawnstrikeActivationProcess `
        -FilePath $GitPath `
        -ArgumentList @("bundle", "verify", $bundle) `
        -WorkingDirectory $ToolRoot `
        -Label "Receipt-bound rollback bundle verification" `
        -TimeoutSeconds $TimeoutSeconds

    $stateBundle = Join-Path $backup ([string]$Receipt.state_backup_id)
    $stateBundleItem = Get-Item -LiteralPath $stateBundle -Force -ErrorAction Stop
    if (
        -not $stateBundleItem.PSIsContainer -or
        ($stateBundleItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
        throw "Receipt-bound durable-state backup is missing or unsafe."
    }
    $stateVerification = Invoke-DawnstrikeActivationProcess `
        -FilePath $PythonPath `
        -ArgumentList @(
            (Join-Path $ToolRoot "scripts\state_disaster_recovery.py"),
            "restore-verify", "--bundle", $stateBundle,
            "--target-db", (Join-Path $StateRoot "shadow_real.sqlite"),
            "--backup-root", $backup, "--state-root", $StateRoot
        ) `
        -WorkingDirectory $ToolRoot `
        -Label "Receipt-bound durable-state backup verification" `
        -TimeoutSeconds $TimeoutSeconds
    try {
        $stateResult = [string]$stateVerification.Stdout | ConvertFrom-Json
    }
    catch {
        throw "Durable-state backup verification did not return valid JSON."
    }
    if (
        [string]$stateResult.status -ne "VERIFY" -or
        [string]$stateResult.backup_id -ne [string]$Receipt.state_backup_id -or
        [string]$stateResult.backup_db_sha256 -ne
            [string]$Receipt.state_backup_db_sha256 -or
        [string]$stateResult.source_release_sha -ne [string]$Receipt.previous_sha -or
        [int]$stateResult.schema_version -ne [int]$Receipt.state_schema_version -or
        [string]$stateResult.quick_check -ne "ok" -or
        $stateResult.write_performed -ne $false -or
        $stateResult.automatic_overwrite -ne $false
    ) {
        throw "Receipt-bound durable-state backup does not match the activation receipt."
    }

    if ($RequireRollbackCheckout) {
        $checkout = Resolve-DawnstrikeActivationRoot `
            (Join-Path $rollbackRoot "previous-runtime") `
            "RollbackCheckout"
        $checkoutContract = Get-DawnstrikeGitContract `
            $GitPath `
            $checkout `
            $TimeoutSeconds `
            ([string]$Receipt.previous_sha)
        if ($checkoutContract.tree -ne [string]$Receipt.previous_tree) {
            throw "Receipt-bound rollback checkout tree is invalid."
        }
        $checkoutOrigin = Get-DawnstrikeGitValue `
            $GitPath `
            $checkout `
            @("remote", "get-url", "origin") `
            "Receipt-bound rollback checkout origin verification" `
            $TimeoutSeconds
        if ((Get-DawnstrikeSha256Text $checkoutOrigin) -ne [string]$Receipt.runtime_origin_sha256) {
            throw "Receipt-bound rollback checkout origin is invalid."
        }
    }
    return $stateResult
}

function Disable-DawnstrikeCanonicalTasks {
    [CmdletBinding()]
    param()

    foreach ($taskName in $script:DawnstrikeCanonicalTaskNames) {
        $matches = @(Get-ScheduledTask -TaskName $taskName -ErrorAction Stop)
        if ($matches.Count -ne 1) {
            throw "Canonical Dawnstrike task name is not unique before disable: $taskName"
        }
        $task = $matches[0]
        if ([string]$task.State -ne "Ready") {
            throw "Canonical task is not exactly Ready before disable: $taskName"
        }
        Disable-ScheduledTask -TaskName $taskName -TaskPath ([string]$task.TaskPath) -ErrorAction Stop | Out-Null
    }
}

function Enable-DawnstrikeCanonicalTasks {
    [CmdletBinding()]
    param()

    foreach ($taskName in $script:DawnstrikeCanonicalTaskNames) {
        $matches = @(Get-ScheduledTask -TaskName $taskName -ErrorAction Stop)
        if ($matches.Count -ne 1) {
            throw "Canonical Dawnstrike task name is not unique before enable: $taskName"
        }
        $task = $matches[0]
        if ([string]$task.State -ne "Disabled") {
            throw "Canonical task is not exactly Disabled before enable: $taskName"
        }
        Enable-ScheduledTask -TaskName $taskName -TaskPath ([string]$task.TaskPath) -ErrorAction Stop | Out-Null
    }
}

function Set-DawnstrikeTasksFailClosedDisabled {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot
    )

    foreach ($taskName in $script:DawnstrikeCanonicalTaskNames) {
        try {
            $matches = @(Get-ScheduledTask -TaskName $taskName -ErrorAction Stop)
            if ($matches.Count -ne 1) { continue }
            $task = $matches[0]
            if ([string]$task.State -ne "Disabled") {
                Disable-ScheduledTask -TaskName $taskName -TaskPath ([string]$task.TaskPath) -ErrorAction Stop | Out-Null
            }
        }
        catch {
            # Best effort only: the caller still throws the ambiguous-state hard stop.
        }
    }
    $proof = Get-DawnstrikeTaskContract $RuntimeRoot $StateRoot -AllowDisabled
    if ($proof.disabled_count -ne 5 -or $proof.enabled_count -ne 0) {
        throw "Unable to prove that all canonical tasks are exactly Disabled."
    }
    return $proof
}

function Write-DawnstrikeActivationJson {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Payload,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $temporary = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        $json = $Payload | ConvertTo-Json -Depth 12
        [System.IO.File]::WriteAllText(
            $temporary,
            $json,
            [System.Text.UTF8Encoding]::new($false)
        )
        [System.IO.File]::Move($temporary, $Path)
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Invoke-DawnstrikeContractCli {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $contract = Join-Path $CandidateRoot "scripts\runtime_activation_contract.py"
    $result = Invoke-DawnstrikeActivationProcess `
        -FilePath $PythonPath `
        -ArgumentList (@($contract) + $Arguments) `
        -WorkingDirectory $CandidateRoot `
        -Label $Label `
        -TimeoutSeconds $TimeoutSeconds
    try {
        return ([string]$result.Stdout | ConvertFrom-Json)
    }
    catch {
        throw "$Label did not return valid JSON."
    }
}

function Enter-DawnstrikeRuntimeActivationLock {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$StateRoot)

    $lockRoot = Join-Path $StateRoot "locks"
    New-Item -ItemType Directory -Path $lockRoot -Force | Out-Null
    $path = Join-Path $lockRoot "dawnstrike-runtime-activation.lock"
    if (Test-Path -LiteralPath $path) {
        throw "A runtime activation lock already exists and requires review."
    }
    $token = [guid]::NewGuid().ToString("N")
    $payload = [ordered]@{
        schema_version = "dawnstrike.runtime_activation_lock.v1"
        process_id = $PID
        process_started_at_utc = (Get-Process -Id $PID).StartTime.ToUniversalTime().ToString("o")
        acquired_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
        lock_token = $token
        research_only = $true
        broker_execution_enabled = $false
    } | ConvertTo-Json -Depth 4
    $handle = $null
    try {
        $handle = [System.IO.File]::Open(
            $path,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
        $handle.Write($bytes, 0, $bytes.Length)
        $handle.Flush($true)
    }
    finally {
        if ($null -ne $handle) { $handle.Dispose() }
    }
    return [pscustomobject]@{ path = $path; token = $token }
}

function Exit-DawnstrikeRuntimeActivationLock {
    [CmdletBinding()]
    param([AllowNull()][object]$Lock)

    if ($null -eq $Lock -or -not (Test-Path -LiteralPath $Lock.path -PathType Leaf)) {
        return
    }
    try {
        $payload = Get-Content -LiteralPath $Lock.path -Raw | ConvertFrom-Json
        if ([string]$payload.lock_token -eq [string]$Lock.token) {
            Remove-Item -LiteralPath $Lock.path -Force
        }
    }
    catch {
        # Never delete a lock whose ownership cannot be proven.
    }
}

function Assert-DawnstrikeNoDailyLocks {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$StateRoot)

    $lockRoot = Join-Path $StateRoot "locks"
    if (-not (Test-Path -LiteralPath $lockRoot -PathType Container)) { return }
    $dailyLocks = @(Get-ChildItem -LiteralPath $lockRoot -Filter "dawnstrike-daily-*.lock" -File -Force)
    if ($dailyLocks.Count -gt 0) {
        throw "A daily run lock exists; runtime activation is not permitted."
    }
}

function Assert-DawnstrikeSameVolume {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string[]]$Paths)

    $roots = @($Paths | ForEach-Object {
        [System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath($_)).ToLowerInvariant()
    } | Select-Object -Unique)
    if ($roots.Count -ne 1) {
        throw "Runtime, stage, and rollback paths must share one volume for recoverable rename."
    }
}

function Invoke-DawnstrikeRuntimeActivation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedSha,
        [Parameter(Mandatory = $true)][string]$MarketDate,
        [Parameter(Mandatory = $true)][string]$CiEvidencePath,
        [Parameter(Mandatory = $true)][string]$SolEvidencePath,
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$BackupRoot,
        [Parameter(Mandatory = $true)][int]$BackupRetention,
        [Parameter(Mandatory = $true)][int]$ProcessTimeoutSeconds,
        [switch]$PreflightOnly
    )

    $candidate = Resolve-DawnstrikeActivationRoot $CandidateRoot "CandidateRoot"
    $runtime = Resolve-DawnstrikeActivationRoot $RuntimeRoot "RuntimeRoot"
    $state = Resolve-DawnstrikeActivationRoot $StateRoot "StateRoot"
    Assert-DawnstrikeRootIsolation $BackupRoot @($candidate, $runtime, $state) "BackupRoot"
    $backupRoot = if ($PreflightOnly) {
        Get-DawnstrikeFutureActivationRoot $BackupRoot "BackupRoot"
    }
    else {
        Ensure-DawnstrikeActivationRoot $BackupRoot "BackupRoot"
    }
    $toolRoot = Resolve-DawnstrikeActivationRoot (Join-Path $PSScriptRoot "..") "ToolRoot"
    if (-not [string]::Equals(
        $candidate,
        $toolRoot,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "CandidateRoot must be the exact checkout containing the activation tool."
    }
    foreach ($pair in @(@($candidate, $runtime), @($candidate, $state), @($runtime, $state))) {
        if ([string]::Equals($pair[0], $pair[1], [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Candidate, runtime, and state roots must be distinct."
        }
    }
    $gitCommand = @(Get-Command git.exe -CommandType Application -ErrorAction Stop)[0]
    $pythonCommand = @(Get-Command py.exe -CommandType Application -ErrorAction Stop)[0]
    $gitPath = $gitCommand.Source
    $pythonPath = $pythonCommand.Source
    . (Join-Path $PSScriptRoot "dawnstrike_job_process.ps1")

    $null = Invoke-DawnstrikeActivationProcess `
        -FilePath $gitPath `
        -ArgumentList @("-C", $candidate, "fetch", "--quiet", "--prune", "origin", "+refs/heads/main:refs/remotes/origin/main") `
        -WorkingDirectory $candidate `
        -Label "Candidate origin/main refresh" `
        -TimeoutSeconds $ProcessTimeoutSeconds
    $candidateContract = Get-DawnstrikeGitContract $gitPath $candidate $ProcessTimeoutSeconds $ExpectedSha
    . (Join-Path $PSScriptRoot "invoke_dawnstrike_stage.ps1")
    $remoteMain = (Get-DawnstrikeGitValue $gitPath $candidate @("rev-parse", "refs/remotes/origin/main") "origin/main verification" $ProcessTimeoutSeconds).ToLowerInvariant()
    if ($remoteMain -ne $ExpectedSha) {
        throw "Expected release SHA is not the current origin/main."
    }
    $null = Invoke-DawnstrikeActivationProcess `
        -FilePath $gitPath `
        -ArgumentList @("-C", $candidate, "merge-base", "--is-ancestor", $ExpectedSha, "refs/remotes/origin/main") `
        -WorkingDirectory $candidate `
        -Label "Candidate remote ancestry verification" `
        -TimeoutSeconds $ProcessTimeoutSeconds
    $origin = Get-DawnstrikeGitValue $gitPath $candidate @("remote", "get-url", "origin") "Candidate origin verification" $ProcessTimeoutSeconds
    Assert-DawnstrikeSafeOrigin $origin
    $originHash = Get-DawnstrikeSha256Text $origin

    $ci = (Resolve-Path -LiteralPath $CiEvidencePath -ErrorAction Stop).Path
    $sol = (Resolve-Path -LiteralPath $SolEvidencePath -ErrorAction Stop).Path
    $evidence = Invoke-DawnstrikeContractCli `
        -PythonPath $pythonPath `
        -CandidateRoot $candidate `
        -Arguments @("validate-evidence", "--ci", $ci, "--sol", $sol, "--candidate-sha", $ExpectedSha, "--candidate-tree", $candidateContract.tree) `
        -Label "Runtime activation evidence validation" `
        -TimeoutSeconds $ProcessTimeoutSeconds

    $runtimeContract = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds
    if ($runtimeContract.head -eq $ExpectedSha) {
        $receiptRoot = Join-Path $state "receipts\runtime-activation"
        $existing = @(Get-ChildItem -LiteralPath $receiptRoot -Filter "runtime-activation-*.json" -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTimeUtc -Descending)
        foreach ($item in $existing) {
            try {
                $receipt = Invoke-DawnstrikeContractCli $pythonPath $candidate @("verify-receipt", "--receipt", $item.FullName, "--expected-status", "COMPLETE") "Existing activation receipt verification" $ProcessTimeoutSeconds
                if ($receipt.candidate_sha -eq $ExpectedSha) {
                    if ($runtimeContract.tree -ne [string]$receipt.candidate_tree) {
                        throw "Existing activation receipt does not match the runtime tree."
                    }
                    $runtimeOrigin = Get-DawnstrikeGitValue $gitPath $runtime @("remote", "get-url", "origin") "Installed origin verification" $ProcessTimeoutSeconds
                    if ((Get-DawnstrikeSha256Text $runtimeOrigin) -ne [string]$receipt.runtime_origin_sha256) {
                        throw "Existing activation receipt does not match the runtime origin."
                    }
                    $existingTasks = Get-DawnstrikeTaskContract $runtime $state
                    if (
                        $existingTasks.task_contract_sha256 -ne
                            [string]$receipt.task_contract_sha256 -or
                        $existingTasks.task_definition_contract_sha256 -ne
                            [string]$receipt.task_definition_contract_sha256 -or
                        $existingTasks.task_action_contract_sha256 -ne
                            [string]$receipt.task_action_contract_sha256
                    ) {
                        throw "Existing activation receipt does not match exact Ready task XML."
                    }
                    $null = Assert-DawnstrikeTaskXmlBackup `
                        -StateRoot $state `
                        -BackupName ([string]$receipt.scheduler_backup_name) `
                        -ExpectedManifestSha256 ([string]$receipt.scheduler_backup_manifest_sha256) `
                        -ExpectedTaskContractSha256 ([string]$receipt.task_contract_sha256) `
                        -ExpectedTaskDefinitionContractSha256 ([string]$receipt.task_definition_contract_sha256) `
                        -ExpectedTaskActionContractSha256 ([string]$receipt.task_action_contract_sha256)
                    $null = Assert-DawnstrikeReceiptRecoveryArtifacts `
                        -Receipt $receipt `
                        -StateRoot $state `
                        -BackupRoot $backupRoot `
                        -ToolRoot $candidate `
                        -GitPath $gitPath `
                        -PythonPath $pythonPath `
                        -TimeoutSeconds $ProcessTimeoutSeconds `
                        -RequireRollbackCheckout
                    return $receipt
                }
            }
            catch {
                continue
            }
        }
        throw "Runtime already has the candidate SHA but no valid COMPLETE activation receipt exists."
    }

    $dbPath = Join-Path $state "shadow_real.sqlite"
    $stateInfo = Invoke-DawnstrikeContractCli $pythonPath $candidate @("inspect-state", "--db-path", $dbPath) "Durable state validation" $ProcessTimeoutSeconds
    Assert-DawnstrikeNoDailyLocks $state
    $taskBefore = Get-DawnstrikeTaskContract $runtime $state

    if ($PreflightOnly) {
        return [pscustomobject]@{
            schema_version = "dawnstrike.runtime_activation_preflight.v1"
            status = "PASS"
            candidate_sha = $ExpectedSha
            candidate_tree = $candidateContract.tree
            previous_sha = $runtimeContract.head
            previous_tree = $runtimeContract.tree
            state_schema_version = $stateInfo.schema_version
            state_quick_check = $stateInfo.quick_check
            task_count = $taskBefore.task_count
            task_contract_sha256 = $taskBefore.task_contract_sha256
            task_definition_contract_sha256 = $taskBefore.task_definition_contract_sha256
            ci_evidence_sha256 = $evidence.ci_evidence_sha256
            sol_evidence_sha256 = $evidence.sol_evidence_sha256
            research_only = $true
            broker_execution_enabled = $false
        }
    }

    $activationSeed = "$ExpectedSha`:$($runtimeContract.head)`:$MarketDate`:$($evidence.ci_evidence_sha256)`:$($evidence.sol_evidence_sha256)"
    $activationId = (Get-DawnstrikeSha256Text $activationSeed).Substring(0, 24)
    $stage = "$runtime.stage-$activationId"
    $rollbackRoot = Join-Path $state "runtime-rollbacks\$activationId"
    $rollbackCheckout = Join-Path $rollbackRoot "previous-runtime"
    $rollbackBundle = Join-Path $rollbackRoot "previous-runtime.bundle"
    $receiptRoot = Join-Path $state "receipts\runtime-activation"
    $schedulerBackupName = "runtime-activation-$activationId"
    $schedulerBackupPath = Join-Path $state "scheduler-backups\$schedulerBackupName"
    $preparedReceipt = Join-Path $receiptRoot "runtime-activation-$activationId.prepared.json"
    $completeReceipt = Join-Path $receiptRoot "runtime-activation-$activationId.json"
    Assert-DawnstrikeSameVolume @($runtime, $stage, $rollbackCheckout)

    if (Test-Path -LiteralPath $completeReceipt -PathType Leaf) {
        $existing = Invoke-DawnstrikeContractCli $pythonPath $candidate @("verify-receipt", "--receipt", $completeReceipt, "--expected-status", "COMPLETE") "Existing activation receipt verification" $ProcessTimeoutSeconds
        $current = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $ExpectedSha
        if (
            $existing.candidate_sha -ne $current.head -or
            [string]$existing.candidate_tree -ne $current.tree
        ) {
            throw "Existing activation receipt does not match the runtime."
        }
        $currentTasks = Get-DawnstrikeTaskContract $runtime $state
        if (
            $currentTasks.task_contract_sha256 -ne [string]$existing.task_contract_sha256 -or
            $currentTasks.task_definition_contract_sha256 -ne
                [string]$existing.task_definition_contract_sha256 -or
            $currentTasks.task_action_contract_sha256 -ne
                [string]$existing.task_action_contract_sha256
        ) {
            throw "Existing activation receipt does not match exact Ready task XML."
        }
        $null = Assert-DawnstrikeTaskXmlBackup `
            -StateRoot $state `
            -BackupName ([string]$existing.scheduler_backup_name) `
            -ExpectedManifestSha256 ([string]$existing.scheduler_backup_manifest_sha256) `
            -ExpectedTaskContractSha256 ([string]$existing.task_contract_sha256) `
            -ExpectedTaskDefinitionContractSha256 ([string]$existing.task_definition_contract_sha256) `
            -ExpectedTaskActionContractSha256 ([string]$existing.task_action_contract_sha256)
        $null = Assert-DawnstrikeReceiptRecoveryArtifacts `
            -Receipt $existing `
            -StateRoot $state `
            -BackupRoot $backupRoot `
            -ToolRoot $candidate `
            -GitPath $gitPath `
            -PythonPath $pythonPath `
            -TimeoutSeconds $ProcessTimeoutSeconds `
            -RequireRollbackCheckout
        return $existing
    }
    if (
        (Test-Path -LiteralPath $preparedReceipt) -or
        (Test-Path -LiteralPath $stage) -or
        (Test-Path -LiteralPath $rollbackRoot) -or
        (Test-Path -LiteralPath $schedulerBackupPath)
    ) {
        throw "A partial activation exists. Run the governed rollback tool before retrying."
    }

    $null = Invoke-DawnstrikeActivationProcess `
        -FilePath $gitPath `
        -ArgumentList @("clone", "--no-local", "--no-hardlinks", "--no-checkout", "--quiet", $candidate, $stage) `
        -WorkingDirectory (Split-Path -Parent $runtime) `
        -Label "Candidate runtime staging" `
        -TimeoutSeconds $ProcessTimeoutSeconds
    try {
        $null = Invoke-DawnstrikeActivationProcess $gitPath @("-C", $stage, "checkout", "--detach", "--quiet", $ExpectedSha) $stage "Candidate checkout staging" $ProcessTimeoutSeconds
        $null = Invoke-DawnstrikeActivationProcess $gitPath @("-C", $stage, "remote", "set-url", "origin", $origin) $stage "Candidate origin binding" $ProcessTimeoutSeconds
        $stagedContract = Get-DawnstrikeGitContract $gitPath $stage $ProcessTimeoutSeconds $ExpectedSha
        if ($stagedContract.tree -ne $candidateContract.tree) {
            throw "Staged runtime tree does not match the accepted candidate tree."
        }
        $stagedOrigin = Get-DawnstrikeGitValue $gitPath $stage @("remote", "get-url", "origin") "Staged origin verification" $ProcessTimeoutSeconds
        if ((Get-DawnstrikeSha256Text $stagedOrigin) -ne $originHash) {
            throw "Staged runtime origin does not match the accepted candidate origin."
        }

        $activationLock = $null
        $dailyLock = $null
        $swapStarted = $false
        $candidateInstalled = $false
        $tasksDisabled = $false
        $preserveLocks = $false
        try {
            $activationLock = Enter-DawnstrikeRuntimeActivationLock $state
            Assert-DawnstrikeNoDailyLocks $state
            $dailyLock = Enter-DawnstrikeDailyRunLock -StateRoot $state -MarketDate $MarketDate -Owner "runtime_activation"
            if (-not $dailyLock.acquired) {
                throw "Runtime activation could not acquire the daily run lock."
            }
            $otherDailyLocks = @(Get-ChildItem -LiteralPath (Join-Path $state "locks") -Filter "dawnstrike-daily-*.lock" -File -Force | Where-Object { $_.FullName -ne $dailyLock.lock_path })
            if ($otherDailyLocks.Count -gt 0) {
                throw "Another daily run lock appeared during runtime activation."
            }
            $taskLocked = Get-DawnstrikeTaskContract $runtime $state
            if ($taskLocked.task_contract_sha256 -ne $taskBefore.task_contract_sha256) {
                throw "Task definitions changed during activation preflight."
            }
            $taskBackup = New-DawnstrikeTaskXmlBackup `
                -StateRoot $state `
                -BackupName $schedulerBackupName `
                -ActivationId $activationId `
                -TaskContract $taskLocked
            $tasksDisabled = $true
            Disable-DawnstrikeCanonicalTasks
            $taskDisabled = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
            if (
                $taskDisabled.disabled_count -ne 5 -or
                $taskDisabled.enabled_count -ne 0 -or
                $taskDisabled.task_definition_contract_sha256 -ne
                    $taskLocked.task_definition_contract_sha256 -or
                $taskDisabled.task_action_contract_sha256 -ne
                    $taskLocked.task_action_contract_sha256
            ) {
                throw "Canonical tasks did not enter the exact disabled swap boundary."
            }
            $stateLocked = Invoke-DawnstrikeContractCli $pythonPath $candidate @("inspect-state", "--db-path", $dbPath) "Locked durable state validation" $ProcessTimeoutSeconds
            if ($stateLocked.main_file_sha256 -ne $stateInfo.main_file_sha256) {
                throw "Durable state changed during activation preflight."
            }

            $backupId = "runtime-activation-$activationId"
            $backup = Invoke-DawnstrikeActivationProcess `
                -FilePath $pythonPath `
                -ArgumentList @(
                    (Join-Path $candidate "scripts\state_disaster_recovery.py"),
                    "backup", "--source-db", $dbPath, "--backup-root", $backupRoot,
                    "--state-root", $state, "--retention", [string]$BackupRetention,
                    "--source-sha", $runtimeContract.head, "--backup-id", $backupId
                ) `
                -WorkingDirectory $candidate `
                -Label "SQLite-consistent pre-activation backup" `
                -TimeoutSeconds $ProcessTimeoutSeconds
            try { $backupResult = [string]$backup.Stdout | ConvertFrom-Json }
            catch { throw "SQLite backup did not return valid JSON." }
            if (
                $backupResult.status -ne "PASS" -or
                $backupResult.quick_check -ne "ok" -or
                [int]$backupResult.schema_version -ne [int]$stateInfo.schema_version -or
                [string]$backupResult.source_release_sha -ne $runtimeContract.head
            ) {
                throw "SQLite backup contract validation failed."
            }
            $stateAfterBackup = Invoke-DawnstrikeContractCli $pythonPath $candidate @("inspect-state", "--db-path", $dbPath) "Post-backup state validation" $ProcessTimeoutSeconds
            if ($stateAfterBackup.main_file_sha256 -ne $stateLocked.main_file_sha256) {
                throw "Durable state changed while creating the activation backup."
            }

            New-Item -ItemType Directory -Path $rollbackRoot -Force | Out-Null
            $bundleTemporary = "$rollbackBundle.$([guid]::NewGuid().ToString('N')).tmp"
            $null = Invoke-DawnstrikeActivationProcess $gitPath @("-C", $runtime, "bundle", "create", $bundleTemporary, "HEAD") $runtime "Rollback bundle creation" $ProcessTimeoutSeconds
            $null = Invoke-DawnstrikeActivationProcess $gitPath @("bundle", "verify", $bundleTemporary) $runtime "Rollback bundle verification" $ProcessTimeoutSeconds
            [System.IO.File]::Move($bundleTemporary, $rollbackBundle)
            $bundleHash = Get-DawnstrikeSha256File $rollbackBundle

            $preparedAt = [DateTime]::UtcNow.ToString("o")
            $receiptPayload = [ordered]@{
                schema_version = "dawnstrike.runtime_activation_receipt.v1"
                status = "PREPARED"
                activation_id = $activationId
                market_date = $MarketDate
                candidate_sha = $ExpectedSha
                candidate_tree = $candidateContract.tree
                previous_sha = $runtimeContract.head
                previous_tree = $runtimeContract.tree
                ci_evidence_sha256 = [string]$evidence.ci_evidence_sha256
                sol_evidence_sha256 = [string]$evidence.sol_evidence_sha256
                state_backup_id = [string]$backupResult.backup_id
                state_backup_db_sha256 = [string]$backupResult.backup_db_sha256
                state_schema_version = [int]$backupResult.schema_version
                state_quick_check = [string]$backupResult.quick_check
                rollback_bundle_sha256 = $bundleHash
                task_count = [int]$taskLocked.task_count
                task_contract_sha256 = [string]$taskLocked.task_contract_sha256
                task_definition_contract_sha256 = [string]$taskLocked.task_definition_contract_sha256
                task_action_contract_sha256 = [string]$taskLocked.task_action_contract_sha256
                task_paths_unchanged = $true
                task_enablement_restored = $false
                scheduler_backup_name = [string]$taskBackup.backup_name
                scheduler_backup_manifest_sha256 = [string]$taskBackup.manifest_sha256
                runtime_origin_sha256 = $originHash
                swap_contract = "same_volume_two_rename_with_immediate_restore"
                stage_name = Split-Path -Leaf $stage
                rollback_checkout_name = "previous-runtime"
                rollback_bundle_name = "previous-runtime.bundle"
                prepared_at_utc = $preparedAt
                completed_at_utc = $null
                research_only = $true
                broker_execution_enabled = $false
            }
            $inputReceipt = Join-Path $receiptRoot ".$activationId.input.json"
            Write-DawnstrikeActivationJson $receiptPayload $inputReceipt
            try {
                $null = Invoke-DawnstrikeContractCli $pythonPath $candidate @("seal-receipt", "--input", $inputReceipt, "--output", $preparedReceipt) "Prepared activation receipt sealing" $ProcessTimeoutSeconds
            }
            finally {
                if (Test-Path -LiteralPath $inputReceipt -PathType Leaf) { Remove-Item -LiteralPath $inputReceipt -Force }
            }

            $runtimeFinalCheck = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $runtimeContract.head
            if ($runtimeFinalCheck.tree -ne $runtimeContract.tree) {
                throw "Runtime changed after rollback evidence was sealed."
            }
            $taskFinalCheck = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
            if (
                $taskFinalCheck.disabled_count -ne 5 -or
                $taskFinalCheck.enabled_count -ne 0 -or
                $taskFinalCheck.task_definition_contract_sha256 -ne
                    $taskLocked.task_definition_contract_sha256 -or
                $taskFinalCheck.task_action_contract_sha256 -ne
                    $taskLocked.task_action_contract_sha256
            ) {
                throw "Task definitions changed immediately before runtime swap."
            }
            $null = Assert-DawnstrikeTaskXmlBackup `
                -StateRoot $state `
                -BackupName $taskBackup.backup_name `
                -ExpectedManifestSha256 $taskBackup.manifest_sha256 `
                -ExpectedTaskContractSha256 ([string]$taskLocked.task_contract_sha256) `
                -ExpectedTaskDefinitionContractSha256 ([string]$taskLocked.task_definition_contract_sha256) `
                -ExpectedTaskActionContractSha256 ([string]$taskLocked.task_action_contract_sha256)

            $swapStarted = $true
            [System.IO.Directory]::Move($runtime, $rollbackCheckout)
            [System.IO.Directory]::Move($stage, $runtime)
            $candidateInstalled = $true

            $installed = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $ExpectedSha
            if ($installed.tree -ne $candidateContract.tree) {
                throw "Installed runtime tree does not match the accepted candidate."
            }
            $installedOrigin = Get-DawnstrikeGitValue $gitPath $runtime @("remote", "get-url", "origin") "Installed origin verification" $ProcessTimeoutSeconds
            if ((Get-DawnstrikeSha256Text $installedOrigin) -ne $originHash) {
                throw "Installed runtime origin does not match the accepted origin."
            }
            $taskAfterDisabled = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
            if (
                $taskAfterDisabled.disabled_count -ne 5 -or
                $taskAfterDisabled.enabled_count -ne 0 -or
                $taskAfterDisabled.task_definition_contract_sha256 -ne
                    $taskLocked.task_definition_contract_sha256 -or
                $taskAfterDisabled.task_action_contract_sha256 -ne
                    $taskLocked.task_action_contract_sha256
            ) {
                throw "Task definitions changed across the runtime swap."
            }
            $null = Assert-DawnstrikeReceiptRecoveryArtifacts `
                -Receipt $receiptPayload `
                -StateRoot $state `
                -BackupRoot $backupRoot `
                -ToolRoot $candidate `
                -GitPath $gitPath `
                -PythonPath $pythonPath `
                -TimeoutSeconds $ProcessTimeoutSeconds `
                -RequireRollbackCheckout
            Enable-DawnstrikeCanonicalTasks
            $taskAfter = Get-DawnstrikeTaskContract $runtime $state
            if ($taskAfter.task_contract_sha256 -ne $taskLocked.task_contract_sha256) {
                throw "Task XML was not restored exactly after runtime activation."
            }
            $tasksDisabled = $false
            $receiptPayload.status = "COMPLETE"
            $receiptPayload.task_enablement_restored = $true
            $receiptPayload.completed_at_utc = [DateTime]::UtcNow.ToString("o")
            Write-DawnstrikeActivationJson $receiptPayload $inputReceipt
            try {
                $complete = Invoke-DawnstrikeContractCli $pythonPath $runtime @("seal-receipt", "--input", $inputReceipt, "--output", $completeReceipt) "Complete activation receipt sealing" $ProcessTimeoutSeconds
            }
            finally {
                if (Test-Path -LiteralPath $inputReceipt -PathType Leaf) { Remove-Item -LiteralPath $inputReceipt -Force }
            }
            return $complete
        }
        catch {
            $failure = $_
            if ($swapStarted -or $tasksDisabled) {
                try {
                    $null = Set-DawnstrikeTasksFailClosedDisabled $runtime $state
                    $tasksDisabled = $true
                }
                catch {
                    $preserveLocks = $true
                    throw "Runtime activation failed and exact task quiescence could not be proven; runtime recovery was not attempted."
                }
            }
            if ($swapStarted -or $tasksDisabled) {
                try {
                    if ($candidateInstalled -and (Test-Path -LiteralPath $runtime -PathType Container)) {
                        $failedCandidate = Join-Path $rollbackRoot "failed-candidate-runtime"
                        if (Test-Path -LiteralPath $failedCandidate) {
                            throw "Failed-candidate preservation path already exists."
                        }
                        [System.IO.Directory]::Move($runtime, $failedCandidate)
                    }
                    if (
                        -not (Test-Path -LiteralPath $runtime) -and
                        (Test-Path -LiteralPath $rollbackCheckout -PathType Container)
                    ) {
                        [System.IO.Directory]::Move($rollbackCheckout, $runtime)
                    }
                    $restoredRuntime = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $runtimeContract.head
                    if ($restoredRuntime.tree -ne $runtimeContract.tree) {
                        throw "Automatic restore did not recover the previous runtime tree."
                    }
                    if ($tasksDisabled) {
                        $null = Assert-DawnstrikeTaskXmlBackup `
                            -StateRoot $state `
                            -BackupName $taskBackup.backup_name `
                            -ExpectedManifestSha256 $taskBackup.manifest_sha256 `
                            -ExpectedTaskContractSha256 ([string]$taskLocked.task_contract_sha256) `
                            -ExpectedTaskDefinitionContractSha256 ([string]$taskLocked.task_definition_contract_sha256) `
                            -ExpectedTaskActionContractSha256 ([string]$taskLocked.task_action_contract_sha256)
                        $restoredDisabledTasks = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
                        if (
                            $restoredDisabledTasks.disabled_count -ne 5 -or
                            $restoredDisabledTasks.enabled_count -ne 0 -or
                            $restoredDisabledTasks.task_definition_contract_sha256 -ne
                                $taskLocked.task_definition_contract_sha256
                        ) {
                            throw "Automatic restore did not recover exact disabled task definitions."
                        }
                        Enable-DawnstrikeCanonicalTasks
                        $restoredTasks = Get-DawnstrikeTaskContract $runtime $state
                        if ($restoredTasks.task_contract_sha256 -ne $taskLocked.task_contract_sha256) {
                            throw "Automatic restore did not recover exact task XML."
                        }
                        $tasksDisabled = $false
                    }
                }
                catch {
                    try {
                        $null = Set-DawnstrikeTasksFailClosedDisabled $runtime $state
                    }
                    catch {
                        $preserveLocks = $true
                        throw "Runtime activation and automatic restore failed; exact task state is unverified and operator recovery is required."
                    }
                    throw "Runtime activation failed and automatic restore could not be completed; canonical tasks are proven Disabled and the prepared receipt/rollback tool are required."
                }
            }
            throw $failure
        }
        finally {
            if (-not $preserveLocks) {
                if ($null -ne $dailyLock) { Exit-DawnstrikeDailyRunLock -Lock $dailyLock }
                Exit-DawnstrikeRuntimeActivationLock $activationLock
            }
        }
    }
    catch {
        # Before the swap, a staged exact checkout is diagnostic evidence and
        # is intentionally retained. It is never promoted implicitly.
        throw
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    if (
        [string]::IsNullOrWhiteSpace($ExpectedSha) -or
        [string]::IsNullOrWhiteSpace($MarketDate) -or
        [string]::IsNullOrWhiteSpace($CiEvidencePath) -or
        [string]::IsNullOrWhiteSpace($SolEvidencePath)
    ) {
        throw "ExpectedSha, MarketDate, CiEvidencePath, and SolEvidencePath are required."
    }
    if ([string]::IsNullOrWhiteSpace($CandidateRoot)) {
        $CandidateRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    }
    $result = Invoke-DawnstrikeRuntimeActivation `
        -ExpectedSha $ExpectedSha `
        -MarketDate $MarketDate `
        -CiEvidencePath $CiEvidencePath `
        -SolEvidencePath $SolEvidencePath `
        -CandidateRoot $CandidateRoot `
        -RuntimeRoot $RuntimeRoot `
        -StateRoot $StateRoot `
        -BackupRoot $BackupRoot `
        -BackupRetention $BackupRetention `
        -ProcessTimeoutSeconds $ProcessTimeoutSeconds `
        -PreflightOnly:$PreflightOnly
    $result | ConvertTo-Json -Depth 12
}
