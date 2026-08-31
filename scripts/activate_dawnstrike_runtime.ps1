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
    return Ensure-DawnstrikeActivationArtifactRoot $fullPath $Label
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
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedRoot
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
        Assert-DawnstrikeActivationJsonPath $Path $ExpectedRoot "Scheduler backup task XML" -AllowMissingLeaf
        [System.IO.File]::WriteAllText($Path, $Xml, $encoding)
        Assert-DawnstrikeActivationJsonPath $Path $ExpectedRoot "Scheduler backup task XML"
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
    $root = Ensure-DawnstrikeActivationArtifactRoot $root "Scheduler backup root"
    $final = Join-Path $root $BackupName
    if (Test-Path -LiteralPath $final) {
        throw "Scheduler XML backup already exists and requires review."
    }
    $temporary = Join-Path $root (".incomplete-$BackupName-" + [guid]::NewGuid().ToString("N"))
    Assert-DawnstrikeNoReparsePath $root "Scheduler backup root"
    Assert-DawnstrikeNoReparsePath $temporary "Scheduler backup temporary directory" -AllowMissingLeaf
    New-Item -ItemType Directory -Path $temporary -ErrorAction Stop | Out-Null
    Assert-DawnstrikeNoReparsePath $temporary "Scheduler backup temporary directory"
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
            $xmlEncoding = Write-DawnstrikeTaskXmlFile $xml $xmlPath $temporary
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
        Assert-DawnstrikeNoReparsePath $temporary "Scheduler backup temporary directory"
        Assert-DawnstrikeNoReparsePath $manifestPath "Scheduler backup manifest" -AllowMissingLeaf
        Write-DawnstrikeActivationJson `
            -Payload $manifest `
            -Path $manifestPath `
            -ExpectedRoot $temporary
        Assert-DawnstrikeNoReparsePath $temporary "Scheduler backup temporary directory"
        Assert-DawnstrikeNoReparsePath $final "Scheduler backup destination" -AllowMissingLeaf
        [System.IO.Directory]::Move($temporary, $final)
        $finalManifest = Join-Path $final "manifest.json"
        Assert-DawnstrikeNoReparsePath $final "Scheduler backup destination"
        Assert-DawnstrikeNoReparsePath $finalManifest "Scheduler backup manifest"
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
            try {
                Assert-DawnstrikeNoReparsePath $temporary "Scheduler backup temporary directory"
                Remove-Item -LiteralPath $temporary -Recurse -Force
            }
            catch { }
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
    Assert-DawnstrikeNoReparsePath $StateRoot "StateRoot"
    Assert-DawnstrikeNoReparsePath (Join-Path $StateRoot "scheduler-backups") "Scheduler backup root"
    Assert-DawnstrikeNoReparsePath $backupPath "Scheduler backup"
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
        $manifestRaw = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8
        Assert-DawnstrikeJsonUniqueProperties $manifestRaw
        $manifest = $manifestRaw | ConvertFrom-Json
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
        Assert-DawnstrikeNoReparsePath $xmlPath "Scheduler backup task XML"
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
    Assert-DawnstrikeNoReparsePath $StateRoot "StateRoot"
    Assert-DawnstrikeNoReparsePath $backup "BackupRoot"
    Assert-DawnstrikeNoReparsePath $rollbackRoot "Rollback root"
    $rollbackRootItem = Get-Item -LiteralPath $rollbackRoot -Force -ErrorAction Stop
    if (
        -not $rollbackRootItem.PSIsContainer -or
        ($rollbackRootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
        throw "Receipt-bound rollback root is missing or unsafe."
    }
    $bundle = Join-Path $rollbackRoot "previous-runtime.bundle"
    Assert-DawnstrikeNoReparsePath $bundle "Rollback bundle"
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
    Assert-DawnstrikeNoReparsePath $stateBundle "Durable-state backup"
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
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedRoot
    )

    $pathFull = [System.IO.Path]::GetFullPath($Path)
    Assert-DawnstrikeActivationJsonPath `
        -Path $Path `
        -ExpectedRoot $ExpectedRoot `
        -Label "Activation JSON path" `
        -AllowMissingLeaf
    $rootFull = [System.IO.Path]::GetFullPath($ExpectedRoot)
    $parent = Split-Path -Parent $pathFull
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        throw "Activation JSON parent directory is missing."
    }
    $temporary = "$pathFull.$([guid]::NewGuid().ToString('N')).tmp"
    Assert-DawnstrikeActivationJsonPath `
        -Path $temporary `
        -ExpectedRoot $rootFull `
        -Label "Activation JSON temporary path" `
        -AllowMissingLeaf
    try {
        $json = $Payload | ConvertTo-Json -Depth 12
        Assert-DawnstrikeActivationJsonPath `
            -Path $pathFull `
            -ExpectedRoot $rootFull `
            -Label "Activation JSON destination" `
            -AllowMissingLeaf
        Assert-DawnstrikeActivationJsonPath `
            -Path $temporary `
            -ExpectedRoot $rootFull `
            -Label "Activation JSON temporary path" `
            -AllowMissingLeaf
        [System.IO.File]::WriteAllText(
            $temporary,
            $json,
            [System.Text.UTF8Encoding]::new($false)
        )
        Assert-DawnstrikeActivationJsonPath `
            -Path $temporary `
            -ExpectedRoot $rootFull `
            -Label "Activation JSON temporary path"
        Assert-DawnstrikeActivationJsonPath `
            -Path $pathFull `
            -ExpectedRoot $rootFull `
            -Label "Activation JSON destination" `
            -AllowMissingLeaf
        [System.IO.File]::Move($temporary, $pathFull)
        Assert-DawnstrikeActivationJsonPath `
            -Path $pathFull `
            -ExpectedRoot $rootFull `
            -Label "Activation JSON destination"
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Assert-DawnstrikeActivationJsonPath `
                -Path $temporary `
                -ExpectedRoot $rootFull `
                -Label "Activation JSON temporary path"
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
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [ValidateSet("runtime_activation", "runtime_rollback")]
        [string]$Owner = "runtime_activation",
        [string]$ActivationId = "",
        [string]$PreparedReceiptName = "",
        [string]$PreparedReceiptSha256 = "",
        [string]$PreparedReceiptFileSha256 = ""
    )

    $lockRoot = Join-Path $StateRoot "locks"
    Assert-DawnstrikeNoReparsePath $StateRoot "StateRoot"
    $lockRoot = Ensure-DawnstrikeActivationArtifactRoot $lockRoot "Activation lock directory"
    $path = Join-Path $lockRoot "dawnstrike-runtime-activation.lock"
    Assert-DawnstrikeNoReparsePath $path "Activation lock" -AllowMissingLeaf
    if (Test-Path -LiteralPath $path) {
        throw "A runtime activation lock already exists and requires review."
    }
    $token = [guid]::NewGuid().ToString("N")
    if (-not [string]::IsNullOrWhiteSpace($ActivationId) -and $ActivationId -notmatch '^[0-9a-f]{24}$') {
        throw "ActivationId must be a lowercase 24-hex value."
    }
    if (-not [string]::IsNullOrWhiteSpace($PreparedReceiptName) -and $PreparedReceiptName -notmatch '^runtime-activation-[0-9a-f]{24}\.prepared\.json$') {
        throw "PreparedReceiptName is invalid."
    }
    if (-not [string]::IsNullOrWhiteSpace($ActivationId) -and [string]::IsNullOrWhiteSpace($PreparedReceiptName)) {
        throw "PreparedReceiptName is required when ActivationId is provided."
    }
    if (-not [string]::IsNullOrWhiteSpace($PreparedReceiptSha256) -and $PreparedReceiptSha256 -notmatch '^[0-9a-f]{64}$') {
        throw "PreparedReceiptSha256 is invalid."
    }
    if (-not [string]::IsNullOrWhiteSpace($PreparedReceiptFileSha256) -and $PreparedReceiptFileSha256 -notmatch '^[0-9a-f]{64}$') {
        throw "PreparedReceiptFileSha256 is invalid."
    }
    $payloadObject = [ordered]@{
        schema_version = if ([string]::IsNullOrWhiteSpace($ActivationId)) { "dawnstrike.runtime_activation_lock.v1" } else { "dawnstrike.runtime_activation_lock.v2" }
        process_id = $PID
        process_started_at_utc = (Get-Process -Id $PID).StartTime.ToUniversalTime().ToString("o")
        acquired_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
        lock_token = $token
        research_only = $true
        broker_execution_enabled = $false
    }
    if (-not [string]::IsNullOrWhiteSpace($ActivationId)) {
        $payloadObject.owner = $Owner
        $payloadObject.activation_id = $ActivationId
        $payloadObject.prepared_receipt_name = $PreparedReceiptName
        $payloadObject.prepared_receipt_sha256 = if ([string]::IsNullOrWhiteSpace($PreparedReceiptSha256)) { $null } else { $PreparedReceiptSha256 }
        $payloadObject.prepared_receipt_file_sha256 = if ([string]::IsNullOrWhiteSpace($PreparedReceiptFileSha256)) { $null } else { $PreparedReceiptFileSha256 }
        $payloadObject.receipt_binding_status = if ([string]::IsNullOrWhiteSpace($PreparedReceiptSha256)) { "UNBOUND" } else { "BOUND" }
    }
    $payload = $payloadObject | ConvertTo-Json -Depth 5
    $handle = $null
    try {
        Assert-DawnstrikeNoReparsePath $StateRoot "StateRoot"
        Assert-DawnstrikeNoReparsePath $lockRoot "Activation lock directory"
        Assert-DawnstrikeNoReparsePath $path "Activation lock" -AllowMissingLeaf
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
    Assert-DawnstrikeNoReparsePath $StateRoot "StateRoot"
    Assert-DawnstrikeNoReparsePath $lockRoot "Activation lock directory"
    Assert-DawnstrikeNoReparsePath $path "Activation lock"
    $created = Get-DawnstrikeFileSnapshotBytes $path "Activation lock"
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $expectedSha = ([System.BitConverter]::ToString(
            $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($payload))
        )).Replace("-", "").ToLowerInvariant()
    }
    finally { $sha.Dispose() }
    if ($created.file_sha256 -ne $expectedSha) {
        throw "Activation lock changed during creation."
    }
    try {
        $createdRaw = [System.Text.Encoding]::UTF8.GetString($created.bytes)
        Assert-DawnstrikeJsonUniqueProperties $createdRaw
        $createdPayload = $createdRaw | ConvertFrom-Json
    }
    catch {
        throw "Activation lock changed during creation."
    }
    if (
        [string]$createdPayload.lock_token -ne $token -or
        [string]$createdPayload.schema_version -ne [string]$payloadObject.schema_version
    ) {
        throw "Activation lock changed during creation."
    }
    return [pscustomobject]@{
        path = $path
        token = $token
        schema_version = [string]$payloadObject.schema_version
        owner = [string]$payloadObject.owner
    }
}

function Exit-DawnstrikeRuntimeActivationLock {
    [CmdletBinding()]
    param([AllowNull()][object]$Lock)

    if ($null -eq $Lock) {
        return
    }
    try { Assert-DawnstrikeNoReparsePath $Lock.path "Activation lock" }
    catch { return }
    try {
        if (-not (Test-Path -LiteralPath $Lock.path -PathType Leaf)) { return }
        $first = Get-DawnstrikeFileSnapshotBytes $Lock.path "Activation lock"
        $raw = [System.Text.Encoding]::UTF8.GetString($first.bytes)
        Assert-DawnstrikeJsonUniqueProperties $raw
        $payload = $raw | ConvertFrom-Json
        $expectedSchema = [string]$Lock.schema_version
        if ([string]::IsNullOrWhiteSpace($expectedSchema)) {
            $expectedSchema = [string]$payload.schema_version
        }
        $expectedOwner = [string]$Lock.owner
        if (
            [string]$payload.lock_token -ne [string]$Lock.token -or
            [string]$payload.schema_version -ne $expectedSchema -or
            (-not [string]::IsNullOrWhiteSpace($expectedOwner) -and [string]$payload.owner -ne $expectedOwner)
        ) {
            return
        }
        $second = Get-DawnstrikeFileSnapshotBytes $Lock.path "Activation lock"
        if ($second.file_sha256 -ne $first.file_sha256 -or $second.bytes.Length -ne $first.bytes.Length) {
            return
        }
        for ($index = 0; $index -lt $first.bytes.Length; $index++) {
            if ($second.bytes[$index] -ne $first.bytes[$index]) { return }
        }
        Assert-DawnstrikeNoReparsePath $Lock.path "Activation lock"
        Remove-Item -LiteralPath $Lock.path -Force
    }
    catch {
        # Never delete a lock whose ownership cannot be proven.
    }
}

function Assert-DawnstrikeNoReparsePath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$AllowMissingLeaf
    )

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $driveRoot = [System.IO.Path]::GetPathRoot($fullPath)
    if ([string]::IsNullOrWhiteSpace($driveRoot)) {
        throw "$Label does not have a valid filesystem root."
    }
    $relative = $fullPath.Substring($driveRoot.Length).Trim('\')
    $current = $driveRoot.TrimEnd('\')
    $parts = if ($relative) { $relative -split '\\' } else { @() }
    for ($index = 0; $index -lt $parts.Count; $index++) {
        $current = Join-Path $current $parts[$index]
        if (-not (Test-Path -LiteralPath $current)) {
            if ($AllowMissingLeaf -and $index -eq ($parts.Count - 1)) {
                return
            }
            throw "$Label is missing or has a missing parent component."
        }
        $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label contains a reparse-point component."
        }
    }
}

function Assert-DawnstrikeActivationJsonPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedRoot,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$AllowMissingLeaf
    )

    $rootFull = [System.IO.Path]::GetFullPath($ExpectedRoot).TrimEnd('\')
    $pathFull = [System.IO.Path]::GetFullPath($Path)
    $rootPrefix = $rootFull + '\'
    if (
        [string]::Equals($rootFull, $pathFull, [System.StringComparison]::OrdinalIgnoreCase) -or
        -not $pathFull.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "$Label is outside the expected activation artifact root."
    }

    Assert-DawnstrikeNoReparsePath $rootFull "$Label root"
    if ($AllowMissingLeaf) {
        Assert-DawnstrikeNoReparsePath $pathFull $Label -AllowMissingLeaf
    }
    else {
        Assert-DawnstrikeNoReparsePath $pathFull $Label
    }
}

function Ensure-DawnstrikeActivationArtifactRoot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $fullPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    $missing = New-Object System.Collections.Generic.List[string]
    $cursor = $fullPath
    while (-not (Test-Path -LiteralPath $cursor -PathType Container)) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Label contains a reparse-point component."
            }
            throw "$Label must be a directory."
        }
        $missing.Add($cursor)
        $parent = Split-Path -Parent $cursor
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $cursor) {
            throw "$Label parent directory does not exist."
        }
        $cursor = $parent.TrimEnd('\')
    }
    Assert-DawnstrikeNoReparsePath $cursor "$Label existing parent"
    for ($index = $missing.Count - 1; $index -ge 0; $index--) {
        $target = $missing[$index]
        $parent = Split-Path -Parent $target
        Assert-DawnstrikeNoReparsePath $parent "$Label parent"
        Assert-DawnstrikeNoReparsePath $target $Label -AllowMissingLeaf
        New-Item -ItemType Directory -Path $target -ErrorAction Stop | Out-Null
        Assert-DawnstrikeNoReparsePath $target $Label
    }
    return $fullPath
}

function Skip-DawnstrikeJsonWhitespace {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Raw,
        [Parameter(Mandatory = $true)][ref]$Index
    )

    while ($Index.Value -lt $Raw.Length -and $Raw[$Index.Value] -in @(' ', "`t", "`r", "`n")) {
        $Index.Value++
    }
}

function Read-DawnstrikeJsonStringToken {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Raw,
        [Parameter(Mandatory = $true)][ref]$Index
    )

    if ($Index.Value -ge $Raw.Length -or $Raw[$Index.Value] -ne '"') {
        throw "JSON string token is missing."
    }
    $Index.Value++
    $builder = [System.Text.StringBuilder]::new()
    while ($Index.Value -lt $Raw.Length) {
        $character = $Raw[$Index.Value]
        $Index.Value++
        if ($character -eq '"') {
            return $builder.ToString()
        }
        if ([int][char]$character -lt 0x20) {
            throw "JSON string contains an unescaped control character."
        }
        if ($character -ne '\') {
            [void]$builder.Append($character)
            continue
        }
        if ($Index.Value -ge $Raw.Length) {
            throw "JSON string escape is incomplete."
        }
        $escape = $Raw[$Index.Value]
        $Index.Value++
        if ($escape -eq 'u') {
            if ($Index.Value + 4 -gt $Raw.Length) {
                throw "JSON unicode escape is incomplete."
            }
            $hex = $Raw.Substring($Index.Value, 4)
            if ($hex -notmatch '^[0-9A-Fa-f]{4}$') {
                throw "JSON unicode escape is invalid."
            }
            [void]$builder.Append([char][Convert]::ToInt32($hex, 16))
            $Index.Value += 4
            continue
        }
        switch ($escape) {
            '"' { [void]$builder.Append('"'); continue }
            '\' { [void]$builder.Append('\'); continue }
            '/' { [void]$builder.Append('/'); continue }
            'b' { [void]$builder.Append("`b"); continue }
            'f' { [void]$builder.Append("`f"); continue }
            'n' { [void]$builder.Append("`n"); continue }
            'r' { [void]$builder.Append("`r"); continue }
            't' { [void]$builder.Append("`t"); continue }
            default { throw "JSON string escape is invalid." }
        }
    }
    throw "JSON string is unterminated."
}

function Read-DawnstrikeJsonValue {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Raw,
        [Parameter(Mandatory = $true)][ref]$Index
    )

    Skip-DawnstrikeJsonWhitespace $Raw $Index
    if ($Index.Value -ge $Raw.Length) {
        throw "JSON value is missing."
    }
    $character = $Raw[$Index.Value]
    if ($character -eq '{') {
        $Index.Value++
        $names = [System.Collections.Generic.HashSet[string]]::new(
            [System.StringComparer]::OrdinalIgnoreCase
        )
        Skip-DawnstrikeJsonWhitespace $Raw $Index
        if ($Index.Value -lt $Raw.Length -and $Raw[$Index.Value] -eq '}') {
            $Index.Value++
            return
        }
        while ($true) {
            Skip-DawnstrikeJsonWhitespace $Raw $Index
            $name = Read-DawnstrikeJsonStringToken $Raw $Index
            try {
                $canonicalName = $name.Normalize([System.Text.NormalizationForm]::FormC).ToUpperInvariant()
            }
            catch {
                throw "JSON property name cannot be normalized."
            }
            if (-not $names.Add($canonicalName)) {
                throw "JSON contains duplicate properties."
            }
            Skip-DawnstrikeJsonWhitespace $Raw $Index
            if ($Index.Value -ge $Raw.Length -or $Raw[$Index.Value] -ne ':') {
                throw "JSON object property separator is missing."
            }
            $Index.Value++
            $null = Read-DawnstrikeJsonValue $Raw $Index
            Skip-DawnstrikeJsonWhitespace $Raw $Index
            if ($Index.Value -ge $Raw.Length) {
                throw "JSON object is unterminated."
            }
            if ($Raw[$Index.Value] -eq '}') {
                $Index.Value++
                return
            }
            if ($Raw[$Index.Value] -ne ',') {
                throw "JSON object separator is invalid."
            }
            $Index.Value++
        }
    }
    if ($character -eq '[') {
        $Index.Value++
        Skip-DawnstrikeJsonWhitespace $Raw $Index
        if ($Index.Value -lt $Raw.Length -and $Raw[$Index.Value] -eq ']') {
            $Index.Value++
            return
        }
        while ($true) {
            $null = Read-DawnstrikeJsonValue $Raw $Index
            Skip-DawnstrikeJsonWhitespace $Raw $Index
            if ($Index.Value -ge $Raw.Length) {
                throw "JSON array is unterminated."
            }
            if ($Raw[$Index.Value] -eq ']') {
                $Index.Value++
                return
            }
            if ($Raw[$Index.Value] -ne ',') {
                throw "JSON array separator is invalid."
            }
            $Index.Value++
        }
    }
    if ($character -eq '"') {
        $null = Read-DawnstrikeJsonStringToken $Raw $Index
        return
    }
    foreach ($literal in @('true', 'false', 'null')) {
        if ($Index.Value + $literal.Length -le $Raw.Length -and
            $Raw.Substring($Index.Value, $literal.Length) -ceq $literal) {
            $Index.Value += $literal.Length
            return
        }
    }
    $number = [regex]::Match(
        $Raw.Substring($Index.Value),
        '^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?'
    )
    if ($number.Success) {
        $Index.Value += $number.Length
        return
    }
    throw "JSON value is invalid."
}

function Assert-DawnstrikeJsonUniqueProperties {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Raw)

    $index = 0
    $null = Read-DawnstrikeJsonValue $Raw ([ref]$index)
    Skip-DawnstrikeJsonWhitespace $Raw ([ref]$index)
    if ($index -ne $Raw.Length) {
        throw "JSON contains trailing data."
    }
}

function Get-DawnstrikeFileSnapshotBytes {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    Assert-DawnstrikeNoReparsePath $Path $Label
    $stream = $null
    $bytes = $null
    try {
        $stream = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::Read
        )
        $bytes = [byte[]]::new($stream.Length)
        $offset = 0
        while ($offset -lt $bytes.Length) {
            $read = $stream.Read($bytes, $offset, $bytes.Length - $offset)
            if ($read -le 0) { throw "$Label could not be read completely." }
            $offset += $read
        }
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $fileSha256 = ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
    return [pscustomobject]@{ bytes = $bytes; file_sha256 = $fileSha256 }
}

function Assert-DawnstrikeSupportedRecoveryEngine {
    [CmdletBinding()]
    param()

    if ([string]$PSVersionTable.PSEdition -ne "Desktop") {
        throw "Receipt-bound lock recovery requires Windows PowerShell Desktop."
    }
}

function Get-DawnstrikeLockOwnerStateFromPayload {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Payload)

    # PowerShell Core may deserialize RFC3339 strings into DateTime values,
    # losing the JSON offset/fraction precision that proves process identity.
    # Recovery is therefore supported only by the engine whose JSON contract
    # preserves those fields as strings. Unknown engines must never evict.
    if ([string]$PSVersionTable.PSEdition -ne "Desktop") {
        return "UNKNOWN"
    }
    try {
        $processId = 0
        if (-not [int]::TryParse([string]$Payload.process_id, [ref]$processId) -or $processId -le 0) {
            return "UNKNOWN"
        }
        $startedValue = [string]$Payload.process_started_at_utc
        if ([string]::IsNullOrWhiteSpace($startedValue)) { return "UNKNOWN" }
        $recordedStart = [DateTimeOffset]::Parse($startedValue).ToUniversalTime()
        $ownerProcess = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($null -eq $ownerProcess) { return "DEAD" }
        $processStarted = [DateTimeOffset]$ownerProcess.StartTime.ToUniversalTime()
        if ($processStarted.UtcDateTime.Ticks -eq $recordedStart.UtcDateTime.Ticks) {
            return "ACTIVE"
        }
        return "DEAD"
    }
    catch {
        return "UNKNOWN"
    }
}

function Get-DawnstrikePreparedReceiptSnapshot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$PreparedReceiptPath,
        [Parameter(Mandatory = $true)][object]$ExpectedReceipt,
        [Parameter(Mandatory = $true)][string]$ExpectedFileSha256
    )

    if ($ExpectedFileSha256 -notmatch '^[0-9a-f]{64}$') {
        throw "Prepared receipt file hash is invalid."
    }
    Assert-DawnstrikeNoReparsePath $PreparedReceiptPath "Prepared receipt"
    $stream = $null
    $bytes = $null
    try {
        $stream = [System.IO.File]::Open(
            $PreparedReceiptPath,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::Read
        )
        $bytes = [byte[]]::new($stream.Length)
        $offset = 0
        while ($offset -lt $bytes.Length) {
            $read = $stream.Read($bytes, $offset, $bytes.Length - $offset)
            if ($read -le 0) { throw "Prepared receipt could not be read completely." }
            $offset += $read
        }
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $fileSha256 = ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
    if ($fileSha256 -ne $ExpectedFileSha256) {
        throw "Prepared receipt file changed after validation."
    }
    $raw = [System.Text.Encoding]::UTF8.GetString($bytes)
    Assert-DawnstrikeJsonUniqueProperties $raw
    try { $fresh = $raw | ConvertFrom-Json } catch { throw "Prepared receipt is not valid JSON." }
    if ([string]$fresh.status -ne "PREPARED") {
        throw "Prepared receipt is not in the PREPARED state."
    }
    if (
        [string]$fresh.activation_id -ne [string]$ExpectedReceipt.activation_id -or
        [string]$fresh.market_date -ne [string]$ExpectedReceipt.market_date -or
        [string]$fresh.receipt_sha256 -ne [string]$ExpectedReceipt.receipt_sha256
    ) {
        throw "Prepared receipt identity changed after validation."
    }
    $expectedJson = $ExpectedReceipt | ConvertTo-Json -Depth 30 -Compress
    $freshJson = $fresh | ConvertTo-Json -Depth 30 -Compress
    if ($freshJson -ne $expectedJson) {
        throw "Prepared receipt content changed after validation."
    }
    return [pscustomobject]@{ payload = $fresh; file_sha256 = $fileSha256 }
}

function Get-DawnstrikeActivationReceiptSnapshot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ReceiptPath,
        [Parameter(Mandatory = $true)][object]$ExpectedReceipt,
        [Parameter(Mandatory = $true)][ValidateSet("PREPARED", "COMPLETE")][string]$ExpectedStatus
    )

    $fileSnapshot = Get-DawnstrikeFileSnapshotBytes $ReceiptPath "Activation receipt"
    try {
        $raw = [System.Text.Encoding]::UTF8.GetString($fileSnapshot.bytes)
        Assert-DawnstrikeJsonUniqueProperties $raw
        $fresh = $raw | ConvertFrom-Json
    }
    catch {
        throw "Activation receipt is not valid JSON."
    }
    if (
        [string]$fresh.status -ne $ExpectedStatus -or
        [string]$fresh.activation_id -ne [string]$ExpectedReceipt.activation_id -or
        [string]$fresh.market_date -ne [string]$ExpectedReceipt.market_date -or
        [string]$fresh.receipt_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$fresh.receipt_sha256 -ne [string]$ExpectedReceipt.receipt_sha256
    ) {
        throw "Activation receipt identity changed after validation."
    }
    if (($fresh | ConvertTo-Json -Depth 30 -Compress) -ne ($ExpectedReceipt | ConvertTo-Json -Depth 30 -Compress)) {
        throw "Activation receipt content changed after validation."
    }
    return [pscustomobject]@{
        payload = $fresh
        raw_bytes = $fileSnapshot.bytes
        file_sha256 = $fileSnapshot.file_sha256
    }
}

function Assert-DawnstrikeActivationReceiptSnapshotUnchanged {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ReceiptPath,
        [Parameter(Mandatory = $true)][object]$ExpectedReceipt,
        [Parameter(Mandatory = $true)][ValidateSet("PREPARED", "COMPLETE")][string]$ExpectedStatus,
        [Parameter(Mandatory = $true)][object]$OriginalSnapshot
    )

    $fresh = Get-DawnstrikeActivationReceiptSnapshot `
        -ReceiptPath $ReceiptPath `
        -ExpectedReceipt $ExpectedReceipt `
        -ExpectedStatus $ExpectedStatus
    if (
        $fresh.file_sha256 -ne [string]$OriginalSnapshot.file_sha256 -or
        $fresh.raw_bytes.Length -ne $OriginalSnapshot.raw_bytes.Length
    ) {
        throw "Activation receipt changed during recovery."
    }
    for ($index = 0; $index -lt $fresh.raw_bytes.Length; $index++) {
        if ($fresh.raw_bytes[$index] -ne $OriginalSnapshot.raw_bytes[$index]) {
            throw "Activation receipt changed during recovery."
        }
    }
    return $fresh
}

function Set-DawnstrikeReceiptBoundLock {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$LockPath,
        [Parameter(Mandatory = $true)][string]$LockToken,
        [Parameter(Mandatory = $true)][string]$ActivationId,
        [Parameter(Mandatory = $true)][string]$PreparedReceiptPath,
        [Parameter(Mandatory = $true)][string]$PreparedReceiptFileSha256,
        [Parameter(Mandatory = $true)][object]$Receipt
    )

    Assert-DawnstrikeNoReparsePath $PreparedReceiptPath "Prepared receipt"
    Assert-DawnstrikeNoReparsePath $LockPath "Receipt-bound lock"
    $receiptSnapshot = Get-DawnstrikePreparedReceiptSnapshot `
        -PreparedReceiptPath $PreparedReceiptPath `
        -ExpectedReceipt $Receipt `
        -ExpectedFileSha256 $PreparedReceiptFileSha256
    $receiptItem = Get-Item -LiteralPath $PreparedReceiptPath -Force -ErrorAction Stop
    if (
        -not $receiptItem.PSIsContainer -and
        ($receiptItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0
    ) {
        $receiptName = $receiptItem.Name
    }
    else {
        throw "Prepared receipt is missing or unsafe."
    }
    if ($receiptName -ne "runtime-activation-$ActivationId.prepared.json") {
        throw "Prepared receipt name does not match the activation."
    }
    $receiptSha = [string]$receiptSnapshot.payload.receipt_sha256
    if ($receiptSha -notmatch '^[0-9a-f]{64}$') {
        throw "Prepared receipt self-hash is invalid."
    }
    $receiptFileSha = [string]$receiptSnapshot.file_sha256
    $payload = $null
    try {
        $lockFileSnapshot = Get-DawnstrikeFileSnapshotBytes $LockPath "Receipt-bound lock"
        $lockRaw = [System.Text.Encoding]::UTF8.GetString($lockFileSnapshot.bytes)
        Assert-DawnstrikeJsonUniqueProperties $lockRaw
        $payload = $lockRaw | ConvertFrom-Json
    }
    catch {
        throw "Receipt binding cannot read the lock."
    }
    if ([string]$payload.lock_token -ne $LockToken) {
        throw "Receipt binding lock token mismatch."
    }
    if ([string]$payload.activation_id -ne $ActivationId) {
        throw "Receipt binding activation id mismatch."
    }
    if ([string]$payload.prepared_receipt_name -ne $receiptName) {
        throw "Receipt binding prepared receipt mismatch."
    }
    $payload.prepared_receipt_sha256 = $receiptSha
    $payload.prepared_receipt_file_sha256 = $receiptFileSha
    $payload.receipt_binding_status = "BOUND"
    $temporary = "$LockPath.$([guid]::NewGuid().ToString('N')).tmp"
    $replacementBackup = "$LockPath.$([guid]::NewGuid().ToString('N')).bak"
    try {
        Assert-DawnstrikeNoReparsePath $temporary "Receipt-bound lock temporary" -AllowMissingLeaf
        Assert-DawnstrikeNoReparsePath $replacementBackup "Receipt-bound lock backup" -AllowMissingLeaf
        $json = $payload | ConvertTo-Json -Depth 8
        Assert-DawnstrikeNoReparsePath $temporary "Receipt-bound lock temporary" -AllowMissingLeaf
        [System.IO.File]::WriteAllText($temporary, $json, [System.Text.UTF8Encoding]::new($false))
        Assert-DawnstrikeNoReparsePath $LockPath "Receipt-bound lock"
        Assert-DawnstrikeNoReparsePath $temporary "Receipt-bound lock temporary"
        Assert-DawnstrikeNoReparsePath $replacementBackup "Receipt-bound lock backup" -AllowMissingLeaf
        $currentLockSnapshot = Get-DawnstrikeFileSnapshotBytes $LockPath "Receipt-bound lock"
        if (
            $currentLockSnapshot.file_sha256 -ne $lockFileSnapshot.file_sha256 -or
            $currentLockSnapshot.bytes.Length -ne $lockFileSnapshot.bytes.Length
        ) {
            throw "Receipt-bound lock changed during binding."
        }
        for ($byteIndex = 0; $byteIndex -lt $currentLockSnapshot.bytes.Length; $byteIndex++) {
            if ($currentLockSnapshot.bytes[$byteIndex] -ne $lockFileSnapshot.bytes[$byteIndex]) {
                throw "Receipt-bound lock changed during binding."
            }
        }
        $null = Get-DawnstrikePreparedReceiptSnapshot `
            -PreparedReceiptPath $PreparedReceiptPath `
            -ExpectedReceipt $Receipt `
            -ExpectedFileSha256 $PreparedReceiptFileSha256
        [System.IO.File]::Replace($temporary, $LockPath, $replacementBackup)
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            try {
                Assert-DawnstrikeNoReparsePath $temporary "Receipt-bound lock temporary"
                Remove-Item -LiteralPath $temporary -Force
            }
            catch { }
        }
        if (Test-Path -LiteralPath $replacementBackup -PathType Leaf) {
            try {
                Assert-DawnstrikeNoReparsePath $replacementBackup "Receipt-bound lock backup"
                Remove-Item -LiteralPath $replacementBackup -Force
            }
            catch { }
        }
    }
}

function Get-DawnstrikeLockSnapshot {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    Assert-DawnstrikeNoReparsePath $fullPath "Lock"
    $item = Get-Item -LiteralPath $fullPath -Force -ErrorAction Stop
    if ($item.PSIsContainer -or ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Lock path is missing or unsafe."
    }
    $fileSnapshot = Get-DawnstrikeFileSnapshotBytes $fullPath "Lock"
    $payload = $null
    try {
        $raw = [System.Text.Encoding]::UTF8.GetString($fileSnapshot.bytes)
        Assert-DawnstrikeJsonUniqueProperties $raw
        $payload = $raw | ConvertFrom-Json
    }
    catch {
        throw "Lock payload is not valid JSON."
    }
    $token = [string]$payload.lock_token
    if ($token -notmatch '^[0-9a-f]{32}$') {
        throw "Lock token is invalid."
    }
    $ownerState = Get-DawnstrikeLockOwnerStateFromPayload $payload
    [pscustomobject]@{
        path = $fullPath
        payload = $payload
        raw_bytes = $fileSnapshot.bytes
        file_sha256 = $fileSnapshot.file_sha256
        owner_state = $ownerState
    }
}

function Assert-DawnstrikeReceiptBoundLock {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Snapshot,
        [Parameter(Mandatory = $true)][object]$Receipt,
        [Parameter(Mandatory = $true)][string]$PreparedReceiptPath,
        [Parameter(Mandatory = $true)][string]$PreparedReceiptFileSha256,
        [Parameter(Mandatory = $true)][string]$ActivationId,
        [Parameter(Mandatory = $true)][string]$Kind,
        [switch]$AllowUnbound
    )

    if ($Snapshot.owner_state -ne "DEAD") {
        throw "Receipt-bound $Kind lock owner is not proven dead."
    }
    $preparedSnapshot = Get-DawnstrikePreparedReceiptSnapshot `
        -PreparedReceiptPath $PreparedReceiptPath `
        -ExpectedReceipt $Receipt `
        -ExpectedFileSha256 $PreparedReceiptFileSha256
    $payload = $Snapshot.payload
    $schema = if ($Kind -eq "activation") { "dawnstrike.runtime_activation_lock.v2" } else { "dawnstrike.daily_run_lock.v4" }
    if ([string]$payload.schema_version -ne $schema) {
        throw "Receipt-bound $Kind lock schema is not recoverable."
    }
    if ([string]$payload.activation_id -ne $ActivationId) {
        throw "Receipt-bound $Kind lock activation id mismatch."
    }
    $receiptName = Split-Path -Leaf $PreparedReceiptPath
    if ([string]$payload.prepared_receipt_name -ne $receiptName) {
        throw "Receipt-bound $Kind lock receipt name mismatch."
    }
    if ([string]$payload.receipt_binding_status -eq "BOUND") {
        if ([string]$payload.prepared_receipt_sha256 -ne [string]$Receipt.receipt_sha256) {
            throw "Receipt-bound $Kind lock receipt self-hash mismatch."
        }
        $receiptFileSha = [string]$preparedSnapshot.file_sha256
        if ([string]$payload.prepared_receipt_file_sha256 -ne $receiptFileSha) {
            throw "Receipt-bound $Kind lock receipt file hash mismatch."
        }
    }
    elseif (-not $AllowUnbound -or [string]$payload.receipt_binding_status -ne "UNBOUND") {
        throw "Receipt-bound $Kind lock is not bound to a sealed PREPARED receipt."
    }
    else {
        # During the two-lock binding transition, the exact activation id and
        # receipt name are already present but the sealed receipt hashes are
        # intentionally empty. Recovery may complete this one lock binding
        # before archival, but only after the dead-owner and receipt checks
        # above have passed.
        if (
            $null -ne $payload.prepared_receipt_sha256 -or
            $null -ne $payload.prepared_receipt_file_sha256
        ) {
            throw "Receipt-bound $Kind lock has partial or tampered receipt hashes."
        }
    }
    if ([string]$payload.research_only -ne "True" -or [string]$payload.broker_execution_enabled -ne "False") {
        throw "Receipt-bound $Kind lock violates research-only safety."
    }
    if ([string]$payload.owner -notin @("runtime_activation", "runtime_rollback")) {
        throw "Receipt-bound $Kind lock owner is not allowlisted."
    }
    if ($Kind -eq "daily" -and [string]$payload.market_date -ne [string]$Receipt.market_date) {
        throw "Receipt-bound daily lock market date mismatch."
    }
}

function Complete-DawnstrikeReceiptBindingTransition {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Snapshot,
        [Parameter(Mandatory = $true)][object]$Receipt,
        [Parameter(Mandatory = $true)][string]$PreparedReceiptPath,
        [Parameter(Mandatory = $true)][string]$PreparedReceiptFileSha256,
        [Parameter(Mandatory = $true)][string]$ActivationId,
        [Parameter(Mandatory = $true)][string]$Kind
    )

    Assert-DawnstrikeReceiptBoundLock `
        -Snapshot $Snapshot `
        -Receipt $Receipt `
        -PreparedReceiptPath $PreparedReceiptPath `
        -PreparedReceiptFileSha256 $PreparedReceiptFileSha256 `
        -ActivationId $ActivationId `
        -Kind $Kind `
        -AllowUnbound
    if ([string]$Snapshot.payload.receipt_binding_status -eq "UNBOUND") {
        Set-DawnstrikeReceiptBoundLock `
            -LockPath $Snapshot.path `
            -LockToken ([string]$Snapshot.payload.lock_token) `
            -ActivationId $ActivationId `
            -PreparedReceiptPath $PreparedReceiptPath `
            -PreparedReceiptFileSha256 $PreparedReceiptFileSha256 `
            -Receipt $Receipt
        $Snapshot = Get-DawnstrikeLockSnapshot $Snapshot.path
    }
    Assert-DawnstrikeReceiptBoundLock `
        -Snapshot $Snapshot `
        -Receipt $Receipt `
        -PreparedReceiptPath $PreparedReceiptPath `
        -PreparedReceiptFileSha256 $PreparedReceiptFileSha256 `
        -ActivationId $ActivationId `
        -Kind $Kind
    return $Snapshot
}

function Find-DawnstrikeArchivedLock {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$LockPath,
        [Parameter(Mandatory = $true)][object]$Receipt,
        [Parameter(Mandatory = $true)][string]$PreparedReceiptPath,
        [Parameter(Mandatory = $true)][string]$PreparedReceiptFileSha256,
        [Parameter(Mandatory = $true)][string]$ActivationId,
        [Parameter(Mandatory = $true)][string]$Kind
    )

    $parent = Split-Path -Parent $LockPath
    $leaf = Split-Path -Leaf $LockPath
    $matches = @()
    foreach ($candidate in @(Get-ChildItem -LiteralPath $parent -Filter "$leaf.archived.*" -File -Force -ErrorAction SilentlyContinue)) {
        try {
            $snapshot = Get-DawnstrikeLockSnapshot $candidate.FullName
            if ([string]$snapshot.payload.activation_id -eq $ActivationId) {
                Assert-DawnstrikeReceiptBoundLock `
                    $snapshot $Receipt $PreparedReceiptPath $PreparedReceiptFileSha256 `
                    $ActivationId $Kind -AllowUnbound
                $matches += $snapshot
            }
        }
        catch {
            throw "Archived $Kind lock evidence is invalid or tampered."
        }
    }
    if ($matches.Count -gt 1) {
        throw "Multiple archived $Kind locks match the activation receipt."
    }
    if ($matches.Count -eq 1) {
        return $matches[0]
    }
    return $null
}

function Assert-DawnstrikeLockPairSnapshotsUnchanged {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object[]]$Members)

    foreach ($member in $Members) {
        Assert-DawnstrikeNoReparsePath $member.path "$($member.kind) lock"
        $fresh = Get-DawnstrikeFileSnapshotBytes $member.path "$($member.kind) lock"
        if (
            $fresh.file_sha256 -ne [string]$member.snapshot.file_sha256 -or
            $fresh.bytes.Length -ne $member.snapshot.raw_bytes.Length
        ) {
            throw "Receipt-bound $($member.kind) lock pair changed during recovery."
        }
        for ($index = 0; $index -lt $fresh.bytes.Length; $index++) {
            if ($fresh.bytes[$index] -ne $member.snapshot.raw_bytes[$index]) {
                throw "Receipt-bound $($member.kind) lock pair changed during recovery."
            }
        }
    }
}

function Archive-DawnstrikeReceiptBoundStaleLocks {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$ActivationReceiptPath,
        [Parameter(Mandatory = $true)][object]$Receipt,
        [Parameter(Mandatory = $true)][string]$PreparedReceiptFileSha256,
        [object]$PreparedReceipt = $null
    )

    Assert-DawnstrikeSupportedRecoveryEngine
    Assert-DawnstrikeNoReparsePath $StateRoot "StateRoot"
    $lockRoot = Join-Path $StateRoot "locks"
    $receiptRoot = Join-Path $StateRoot "receipts\runtime-activation"
    Assert-DawnstrikeNoReparsePath $lockRoot "Lock directory"
    Assert-DawnstrikeNoReparsePath $receiptRoot "Activation receipt directory"
    $activationId = [string]$Receipt.activation_id
    $marketDate = [string]$Receipt.market_date
    if ($activationId -notmatch '^[0-9a-f]{24}$' -or $marketDate -notmatch '^\d{4}-\d{2}-\d{2}$') {
        throw "Stale lock recovery receipt identity is invalid."
    }
    $activationLockPath = Join-Path $lockRoot "dawnstrike-runtime-activation.lock"
    $dailyLockPath = Join-Path $lockRoot ("dawnstrike-daily-" + $marketDate + ".lock")
    Assert-DawnstrikeNoReparsePath $activationLockPath "Activation lock" -AllowMissingLeaf
    Assert-DawnstrikeNoReparsePath $dailyLockPath "Daily lock" -AllowMissingLeaf

    $receiptStatus = [string]$Receipt.status
    if ($receiptStatus -notin @("PREPARED", "COMPLETE")) {
        if ((Test-Path -LiteralPath $activationLockPath -PathType Leaf) -or (Test-Path -LiteralPath $dailyLockPath -PathType Leaf)) {
            throw "Stale lock recovery requires the exact sealed PREPARED or COMPLETE activation receipt."
        }
        return [pscustomobject]@{ status = "NO_LOCKS"; activation_archived = $false; daily_archived = $false }
    }

    $approvedReceiptRoot = [System.IO.Path]::GetFullPath(
        $receiptRoot
    ).TrimEnd('\') + '\'
    Assert-DawnstrikeNoReparsePath $ActivationReceiptPath "Activation receipt"
    $receiptItem = Get-Item -LiteralPath $ActivationReceiptPath -Force -ErrorAction Stop
    $receiptFullPath = [System.IO.Path]::GetFullPath($receiptItem.FullName)
    if (-not $receiptFullPath.StartsWith($approvedReceiptRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Stale lock recovery receipt is outside the durable activation receipt root."
    }
    $expectedReceiptName = if ($receiptStatus -eq "PREPARED") {
        "runtime-activation-$activationId.prepared.json"
    } else {
        "runtime-activation-$activationId.json"
    }
    if (
        $receiptItem.PSIsContainer -or
        ($receiptItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $receiptItem.Name -ne $expectedReceiptName
    ) {
        throw "Stale lock recovery requires the exact sealed activation receipt path."
    }
    $preparedReceiptPath = if ($receiptStatus -eq "PREPARED") {
        $ActivationReceiptPath
    } else {
        Join-Path $receiptRoot "runtime-activation-$activationId.prepared.json"
    }
    $completeReceiptSnapshot = $null
    if ($receiptStatus -eq "COMPLETE") {
        $completeReceiptSnapshot = Get-DawnstrikeActivationReceiptSnapshot `
            -ReceiptPath $ActivationReceiptPath `
            -ExpectedReceipt $Receipt `
            -ExpectedStatus "COMPLETE"
    }
    if ($null -eq $PreparedReceipt) { $PreparedReceipt = $Receipt }
    Assert-DawnstrikeNoReparsePath $preparedReceiptPath "Prepared activation receipt"
    $preparedSnapshot = Get-DawnstrikePreparedReceiptSnapshot `
        -PreparedReceiptPath $preparedReceiptPath `
        -ExpectedReceipt $PreparedReceipt `
        -ExpectedFileSha256 $PreparedReceiptFileSha256
    $PreparedReceipt = $preparedSnapshot.payload

    $activationCurrent = if (Test-Path -LiteralPath $activationLockPath -PathType Leaf) { Get-DawnstrikeLockSnapshot $activationLockPath } else { $null }
    $dailyCurrent = if (Test-Path -LiteralPath $dailyLockPath -PathType Leaf) { Get-DawnstrikeLockSnapshot $dailyLockPath } else { $null }
    $activationArchived = if ($null -eq $activationCurrent) {
        Find-DawnstrikeArchivedLock `
            -LockPath $activationLockPath -Receipt $PreparedReceipt `
            -PreparedReceiptPath $preparedReceiptPath `
            -PreparedReceiptFileSha256 $PreparedReceiptFileSha256 `
            -ActivationId $activationId -Kind "activation"
    } else { $null }
    $dailyArchived = if ($null -eq $dailyCurrent) {
        Find-DawnstrikeArchivedLock `
            -LockPath $dailyLockPath -Receipt $PreparedReceipt `
            -PreparedReceiptPath $preparedReceiptPath `
            -PreparedReceiptFileSha256 $PreparedReceiptFileSha256 `
            -ActivationId $activationId -Kind "daily"
    } else { $null }
    if ($null -eq $activationCurrent -and $null -eq $activationArchived -and $null -eq $dailyCurrent -and $null -eq $dailyArchived) {
        return [pscustomobject]@{ status = "NO_LOCKS"; activation_archived = $false; daily_archived = $false }
    }
    if ($null -eq $activationCurrent -and $null -eq $activationArchived) {
        throw "Receipt-bound activation lock is missing from current and archived state."
    }
    if ($null -eq $dailyCurrent -and $null -eq $dailyArchived) {
        throw "Receipt-bound daily lock is missing from current and archived state."
    }
    $activationSnapshot = if ($null -ne $activationCurrent) { $activationCurrent } else { $activationArchived }
    $dailySnapshot = if ($null -ne $dailyCurrent) { $dailyCurrent } else { $dailyArchived }
    $pairMembers = @(
        [pscustomobject]@{ kind = "activation"; path = $activationSnapshot.path; snapshot = $activationSnapshot },
        [pscustomobject]@{ kind = "daily"; path = $dailySnapshot.path; snapshot = $dailySnapshot }
    )
    Assert-DawnstrikeLockPairSnapshotsUnchanged $pairMembers
    Assert-DawnstrikeReceiptBoundLock `
        -Snapshot $activationSnapshot -Receipt $PreparedReceipt `
        -PreparedReceiptPath $preparedReceiptPath `
        -PreparedReceiptFileSha256 $PreparedReceiptFileSha256 `
        -ActivationId $activationId -Kind "activation" -AllowUnbound
    Assert-DawnstrikeReceiptBoundLock `
        -Snapshot $dailySnapshot -Receipt $PreparedReceipt `
        -PreparedReceiptPath $preparedReceiptPath `
        -PreparedReceiptFileSha256 $PreparedReceiptFileSha256 `
        -ActivationId $activationId -Kind "daily" -AllowUnbound
    $allowedOwners = @("runtime_activation", "runtime_rollback")
    $activationOwner = [string]$activationSnapshot.payload.owner
    $dailyOwner = [string]$dailySnapshot.payload.owner
    if ($activationOwner -notin $allowedOwners -or $dailyOwner -notin $allowedOwners -or $activationOwner -ne $dailyOwner) {
        throw "Receipt-bound activation locks do not share one allowlisted operation owner."
    }
    if ($receiptStatus -eq "COMPLETE") {
        $null = Assert-DawnstrikeActivationReceiptSnapshotUnchanged `
            -ReceiptPath $ActivationReceiptPath -ExpectedReceipt $Receipt `
            -ExpectedStatus "COMPLETE" -OriginalSnapshot $completeReceiptSnapshot
    }
    $null = Get-DawnstrikePreparedReceiptSnapshot `
        -PreparedReceiptPath $preparedReceiptPath `
        -ExpectedReceipt $PreparedReceipt `
        -ExpectedFileSha256 $PreparedReceiptFileSha256
    Assert-DawnstrikeLockPairSnapshotsUnchanged $pairMembers
    $activationSnapshot = Complete-DawnstrikeReceiptBindingTransition `
        -Snapshot $activationSnapshot `
        -Receipt $PreparedReceipt `
        -PreparedReceiptPath $preparedReceiptPath `
        -PreparedReceiptFileSha256 $PreparedReceiptFileSha256 `
        -ActivationId $activationId `
        -Kind "activation"
    $pairMembers[0].snapshot = $activationSnapshot
    Assert-DawnstrikeLockPairSnapshotsUnchanged $pairMembers
    if ($receiptStatus -eq "COMPLETE") {
        $null = Assert-DawnstrikeActivationReceiptSnapshotUnchanged `
            -ReceiptPath $ActivationReceiptPath -ExpectedReceipt $Receipt `
            -ExpectedStatus "COMPLETE" -OriginalSnapshot $completeReceiptSnapshot
    }
    $dailySnapshot = Complete-DawnstrikeReceiptBindingTransition `
        -Snapshot $dailySnapshot `
        -Receipt $PreparedReceipt `
        -PreparedReceiptPath $preparedReceiptPath `
        -PreparedReceiptFileSha256 $PreparedReceiptFileSha256 `
        -ActivationId $activationId `
        -Kind "daily"
    $pairMembers[1].snapshot = $dailySnapshot
    if (
        [int]$activationSnapshot.payload.process_id -ne [int]$dailySnapshot.payload.process_id -or
        [string]$activationSnapshot.payload.process_started_at_utc -ne [string]$dailySnapshot.payload.process_started_at_utc
    ) {
        throw "Receipt-bound activation locks do not share one dead owner identity."
    }
    $timestamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
    $entries = @()
    if ($null -ne $activationCurrent) {
        $entries += [pscustomobject]@{
            kind = "activation"
            snapshot = $activationSnapshot
            destination = "$activationLockPath.archived.$timestamp.$([guid]::NewGuid().ToString('N'))"
        }
    }
    if ($null -ne $dailyCurrent) {
        $entries += [pscustomobject]@{
            kind = "daily"
            snapshot = $dailySnapshot
            destination = "$dailyLockPath.archived.$timestamp.$([guid]::NewGuid().ToString('N'))"
        }
    }
    foreach ($entry in $entries) {
        if ($receiptStatus -eq "COMPLETE") {
            $null = Assert-DawnstrikeActivationReceiptSnapshotUnchanged `
                -ReceiptPath $ActivationReceiptPath -ExpectedReceipt $Receipt `
                -ExpectedStatus "COMPLETE" -OriginalSnapshot $completeReceiptSnapshot
        }
        $null = Get-DawnstrikePreparedReceiptSnapshot `
            -PreparedReceiptPath $preparedReceiptPath `
            -ExpectedReceipt $PreparedReceipt `
            -ExpectedFileSha256 $PreparedReceiptFileSha256
        Assert-DawnstrikeLockPairSnapshotsUnchanged $pairMembers
        Assert-DawnstrikeNoReparsePath $entry.snapshot.path "$($entry.kind) lock"
        Assert-DawnstrikeNoReparsePath $entry.destination "$($entry.kind) lock archive" -AllowMissingLeaf
        Assert-DawnstrikeNoReparsePath $entry.snapshot.path "$($entry.kind) lock"
        Assert-DawnstrikeNoReparsePath $entry.destination "$($entry.kind) lock archive" -AllowMissingLeaf
        [System.IO.File]::Move($entry.snapshot.path, $entry.destination)
        Assert-DawnstrikeNoReparsePath $entry.destination "$($entry.kind) lock archive"
        $member = @($pairMembers | Where-Object { $_.kind -eq $entry.kind })[0]
        $member.path = $entry.destination
        $archivedFileSnapshot = Get-DawnstrikeFileSnapshotBytes $entry.destination "$($entry.kind) lock archive"
        if (
            $archivedFileSnapshot.file_sha256 -ne [string]$entry.snapshot.file_sha256 -or
            $archivedFileSnapshot.bytes.Length -ne $entry.snapshot.raw_bytes.Length
        ) {
            throw "Archived $($entry.kind) lock failed integrity verification."
        }
        for ($byteIndex = 0; $byteIndex -lt $archivedFileSnapshot.bytes.Length; $byteIndex++) {
            if ($archivedFileSnapshot.bytes[$byteIndex] -ne $entry.snapshot.raw_bytes[$byteIndex]) {
                throw "Archived $($entry.kind) lock failed integrity verification."
            }
        }
        Assert-DawnstrikeLockPairSnapshotsUnchanged $pairMembers
    }
    if ($receiptStatus -eq "COMPLETE") {
        $null = Assert-DawnstrikeActivationReceiptSnapshotUnchanged `
            -ReceiptPath $ActivationReceiptPath -ExpectedReceipt $Receipt `
            -ExpectedStatus "COMPLETE" -OriginalSnapshot $completeReceiptSnapshot
    }
    $null = Get-DawnstrikePreparedReceiptSnapshot `
        -PreparedReceiptPath $preparedReceiptPath `
        -ExpectedReceipt $PreparedReceipt `
        -ExpectedFileSha256 $PreparedReceiptFileSha256
    Assert-DawnstrikeLockPairSnapshotsUnchanged $pairMembers
    return [pscustomobject]@{
        status = if ($entries.Count -eq 0) { "ALREADY_ARCHIVED" } else { "ARCHIVED" }
        activation_archived = $true
        daily_archived = $true
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
        Assert-DawnstrikeNoReparsePath $state "StateRoot"
        Assert-DawnstrikeNoReparsePath $receiptRoot "Activation receipt directory"
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
                        -TimeoutSeconds $ProcessTimeoutSeconds
                    $existingRollbackCheckout = Join-Path $state "runtime-rollbacks\$([string]$receipt.activation_id)\previous-runtime"
                    Assert-DawnstrikeNoReparsePath $existingRollbackCheckout "Rollback checkout" -AllowMissingLeaf
                    if (Test-Path -LiteralPath $existingRollbackCheckout -PathType Container) {
                        $null = Assert-DawnstrikeReceiptRecoveryArtifacts `
                            -Receipt $receipt `
                            -StateRoot $state `
                            -BackupRoot $backupRoot `
                            -ToolRoot $candidate `
                            -GitPath $gitPath `
                            -PythonPath $pythonPath `
                            -TimeoutSeconds $ProcessTimeoutSeconds `
                            -RequireRollbackCheckout
                    }
                    $existingPreparedPath = Join-Path $receiptRoot "runtime-activation-$([string]$receipt.activation_id).prepared.json"
                    Assert-DawnstrikeNoReparsePath $existingPreparedPath "Prepared activation receipt"
                    $existingPrepared = Invoke-DawnstrikeContractCli `
                        -PythonPath $pythonPath `
                        -CandidateRoot $candidate `
                        -Arguments @("verify-receipt", "--receipt", $existingPreparedPath, "--expected-status", "PREPARED") `
                        -Label "Existing prepared activation receipt verification" `
                        -TimeoutSeconds $ProcessTimeoutSeconds
                    $null = Archive-DawnstrikeReceiptBoundStaleLocks `
                        -StateRoot $state `
                        -ActivationReceiptPath $item.FullName `
                        -Receipt $receipt `
                        -PreparedReceipt $existingPrepared `
                        -PreparedReceiptFileSha256 (Get-DawnstrikeSha256File $existingPreparedPath)
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
    $receiptRoot = Ensure-DawnstrikeActivationArtifactRoot $receiptRoot "Activation receipt directory"
    Assert-DawnstrikeActivationJsonPath $preparedReceipt $receiptRoot "Prepared activation receipt" -AllowMissingLeaf
    Assert-DawnstrikeActivationJsonPath $completeReceipt $receiptRoot "Complete activation receipt" -AllowMissingLeaf
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
        $existingPreparedPath = Join-Path $receiptRoot "runtime-activation-$([string]$existing.activation_id).prepared.json"
        Assert-DawnstrikeNoReparsePath $existingPreparedPath "Prepared activation receipt"
        $existingPrepared = Invoke-DawnstrikeContractCli `
            -PythonPath $pythonPath `
            -CandidateRoot $candidate `
            -Arguments @("verify-receipt", "--receipt", $existingPreparedPath, "--expected-status", "PREPARED") `
            -Label "Existing prepared activation receipt verification" `
            -TimeoutSeconds $ProcessTimeoutSeconds
        $null = Archive-DawnstrikeReceiptBoundStaleLocks `
            -StateRoot $state `
            -ActivationReceiptPath $completeReceipt `
            -Receipt $existing `
            -PreparedReceipt $existingPrepared `
            -PreparedReceiptFileSha256 (Get-DawnstrikeSha256File $existingPreparedPath)
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
    Assert-DawnstrikeNoReparsePath (Split-Path -Parent $stage) "Runtime staging parent"
    Assert-DawnstrikeNoReparsePath $stage "Runtime staging path" -AllowMissingLeaf
    Assert-DawnstrikeNoReparsePath (Split-Path -Parent $rollbackRoot) "Runtime rollback parent" -AllowMissingLeaf
    Assert-DawnstrikeNoReparsePath (Split-Path -Parent $schedulerBackupPath) "Scheduler backup parent" -AllowMissingLeaf

    $null = Invoke-DawnstrikeActivationProcess `
        -FilePath $gitPath `
        -ArgumentList @("clone", "--no-local", "--no-hardlinks", "--no-checkout", "--quiet", $candidate, $stage) `
        -WorkingDirectory (Split-Path -Parent $runtime) `
        -Label "Candidate runtime staging" `
        -TimeoutSeconds $ProcessTimeoutSeconds
    Assert-DawnstrikeNoReparsePath $stage "Runtime staging path"
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
            $preparedReceiptName = "runtime-activation-$activationId.prepared.json"
            $activationLock = Enter-DawnstrikeRuntimeActivationLock `
                -StateRoot $state `
                -ActivationId $activationId `
                -PreparedReceiptName $preparedReceiptName
            Assert-DawnstrikeNoDailyLocks $state
            $dailyLock = Enter-DawnstrikeDailyRunLock `
                -StateRoot $state `
                -MarketDate $MarketDate `
                -Owner "runtime_activation" `
                -ActivationId $activationId `
                -PreparedReceiptName $preparedReceiptName
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

            $rollbackParent = Ensure-DawnstrikeActivationArtifactRoot `
                (Join-Path $state "runtime-rollbacks") `
                "Runtime rollback directory"
            $rollbackRoot = Ensure-DawnstrikeActivationArtifactRoot $rollbackRoot "Runtime rollback root"
            Assert-DawnstrikeNoReparsePath $rollbackRoot "Runtime rollback root"
            Assert-DawnstrikeNoReparsePath $rollbackBundle "Rollback bundle" -AllowMissingLeaf
            $bundleTemporary = "$rollbackBundle.$([guid]::NewGuid().ToString('N')).tmp"
            Assert-DawnstrikeNoReparsePath $bundleTemporary "Rollback bundle temporary" -AllowMissingLeaf
            $null = Invoke-DawnstrikeActivationProcess $gitPath @("-C", $runtime, "bundle", "create", $bundleTemporary, "HEAD") $runtime "Rollback bundle creation" $ProcessTimeoutSeconds
            Assert-DawnstrikeNoReparsePath $bundleTemporary "Rollback bundle temporary"
            $null = Invoke-DawnstrikeActivationProcess $gitPath @("bundle", "verify", $bundleTemporary) $runtime "Rollback bundle verification" $ProcessTimeoutSeconds
            Assert-DawnstrikeNoReparsePath $bundleTemporary "Rollback bundle temporary"
            Assert-DawnstrikeNoReparsePath $rollbackBundle "Rollback bundle" -AllowMissingLeaf
            [System.IO.File]::Move($bundleTemporary, $rollbackBundle)
            Assert-DawnstrikeNoReparsePath $rollbackBundle "Rollback bundle"
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
            Write-DawnstrikeActivationJson `
                -Payload $receiptPayload `
                -Path $inputReceipt `
                -ExpectedRoot $receiptRoot
            try {
                Assert-DawnstrikeActivationJsonPath $inputReceipt $receiptRoot "Activation input receipt"
                Assert-DawnstrikeActivationJsonPath $preparedReceipt $receiptRoot "Prepared activation receipt" -AllowMissingLeaf
                $prepared = Invoke-DawnstrikeContractCli $pythonPath $candidate @("seal-receipt", "--input", $inputReceipt, "--output", $preparedReceipt) "Prepared activation receipt sealing" $ProcessTimeoutSeconds
                Assert-DawnstrikeActivationJsonPath $preparedReceipt $receiptRoot "Prepared activation receipt"
            }
            finally {
                if (Test-Path -LiteralPath $inputReceipt -PathType Leaf) {
                    Assert-DawnstrikeActivationJsonPath $inputReceipt $receiptRoot "Activation input receipt"
                    Remove-Item -LiteralPath $inputReceipt -Force
                }
            }
            $preparedReceiptFileSha256 = Get-DawnstrikeSha256File $preparedReceipt
            Set-DawnstrikeReceiptBoundLock `
                -LockPath $activationLock.path `
                -LockToken $activationLock.token `
                -ActivationId $activationId `
                -PreparedReceiptPath $preparedReceipt `
                -PreparedReceiptFileSha256 $preparedReceiptFileSha256 `
                -Receipt $prepared
            Set-DawnstrikeReceiptBoundLock `
                -LockPath $dailyLock.lock_path `
                -LockToken $dailyLock.lock_token `
                -ActivationId $activationId `
                -PreparedReceiptPath $preparedReceipt `
                -PreparedReceiptFileSha256 $preparedReceiptFileSha256 `
                -Receipt $prepared

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
            Assert-DawnstrikeNoReparsePath $runtime "Runtime root"
            Assert-DawnstrikeNoReparsePath $rollbackCheckout "Rollback checkout" -AllowMissingLeaf
            Assert-DawnstrikeNoReparsePath $stage "Runtime staging path"
            [System.IO.Directory]::Move($runtime, $rollbackCheckout)
            Assert-DawnstrikeNoReparsePath $rollbackCheckout "Rollback checkout"
            Assert-DawnstrikeNoReparsePath $runtime "Runtime root" -AllowMissingLeaf
            Assert-DawnstrikeNoReparsePath $stage "Runtime staging path"
            [System.IO.Directory]::Move($stage, $runtime)
            Assert-DawnstrikeNoReparsePath $runtime "Runtime root"
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
            Write-DawnstrikeActivationJson `
                -Payload $receiptPayload `
                -Path $inputReceipt `
                -ExpectedRoot $receiptRoot
            try {
                Assert-DawnstrikeActivationJsonPath $inputReceipt $receiptRoot "Activation input receipt"
                Assert-DawnstrikeActivationJsonPath $completeReceipt $receiptRoot "Complete activation receipt" -AllowMissingLeaf
                $complete = Invoke-DawnstrikeContractCli $pythonPath $runtime @("seal-receipt", "--input", $inputReceipt, "--output", $completeReceipt) "Complete activation receipt sealing" $ProcessTimeoutSeconds
                Assert-DawnstrikeActivationJsonPath $completeReceipt $receiptRoot "Complete activation receipt"
            }
            finally {
                if (Test-Path -LiteralPath $inputReceipt -PathType Leaf) {
                    Assert-DawnstrikeActivationJsonPath $inputReceipt $receiptRoot "Activation input receipt"
                    Remove-Item -LiteralPath $inputReceipt -Force
                }
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
                        Assert-DawnstrikeNoReparsePath $runtime "Runtime root"
                        Assert-DawnstrikeNoReparsePath $failedCandidate "Failed candidate preservation path" -AllowMissingLeaf
                        if (Test-Path -LiteralPath $failedCandidate) {
                            throw "Failed-candidate preservation path already exists."
                        }
                        [System.IO.Directory]::Move($runtime, $failedCandidate)
                        Assert-DawnstrikeNoReparsePath $failedCandidate "Failed candidate preservation path"
                    }
                    if (
                        -not (Test-Path -LiteralPath $runtime) -and
                        (Test-Path -LiteralPath $rollbackCheckout -PathType Container)
                    ) {
                        Assert-DawnstrikeNoReparsePath $rollbackCheckout "Rollback checkout"
                        Assert-DawnstrikeNoReparsePath $runtime "Runtime root" -AllowMissingLeaf
                        [System.IO.Directory]::Move($rollbackCheckout, $runtime)
                        Assert-DawnstrikeNoReparsePath $runtime "Runtime root"
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
