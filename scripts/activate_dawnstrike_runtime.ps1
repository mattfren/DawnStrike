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
    [pscredential]$RunAsCredential,
    [switch]$PreflightOnly
)

$ErrorActionPreference = "Stop"
if (
    [int]$PSVersionTable.PSVersion.Major -lt 5 -or
    [string]$PSVersionTable.PSEdition -ne "Desktop"
) {
    throw "Dawnstrike activation requires Windows PowerShell 5.1 or later (Desktop edition)."
}

$script:DawnstrikeCanonicalTaskNames = @(
    "Dawnstrike AlphaOps Morning",
    "Dawnstrike AlphaOps Monitor 5m",
    "Dawnstrike AlphaOps EOD Full Report",
    "Dawnstrike AlphaOps V6 Weekly Training",
    "Dawnstrike 10of10 Daily Finalize"
)
$script:DawnstrikeAuxiliaryCaptureTaskName = "Dawnstrike Delayed SIP Capture"
$script:DawnstrikeStatePreparationContractFile = "config\state_preparation_contract.json"

function Get-DawnstrikeSha256Text {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)

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

    Assert-DawnstrikeNoReparseComponents $Path $Label
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (-not $item.PSIsContainer) {
        throw "$Label must be an existing directory."
    }
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label cannot be a reparse point."
    }
    return $item.FullName.TrimEnd('\')
}

function Assert-DawnstrikeNoReparseComponents {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $full = [System.IO.Path]::GetFullPath($Path)
    $item = Get-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue
    if ($null -ne $item -and ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label contains a reparse-point path component."
    }
    $current = if ($null -ne $item -and $item.PSIsContainer) {
        [System.IO.DirectoryInfo]::new($item.FullName)
    }
    else {
        [System.IO.DirectoryInfo]::new((Split-Path -Parent $full))
    }
    while ($null -ne $current) {
        if ($current.Exists -and ($current.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label contains a reparse-point path component."
        }
        if ([string]::Equals($current.FullName.TrimEnd('\'), $current.Root.FullName.TrimEnd('\'), [System.StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $current = $current.Parent
    }
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
    Assert-DawnstrikeNoReparseComponents $Path $Label
    $candidate = [System.IO.Path]::GetFullPath($Path).TrimEnd('\') + '\'
    foreach ($otherPath in $OtherPaths) {
        Assert-DawnstrikeNoReparseComponents $otherPath "$Label comparison root"
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
        # Task Scheduler omits Settings/Enabled when the task is Ready because
        # the schema default is true, but emits it after Disable-ScheduledTask.
        # Parse structurally and remove that one state-only element so the
        # definition contract remains stable across the enable/disable window.
        $document.PreserveWhitespace = $false
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
        if ($enabledNodes.Count -gt 1) {
            throw "Task XML must contain at most one Settings/Enabled element."
        }
        if ($enabledNodes.Count -eq 1) {
            $null = $enabledNodes[0].ParentNode.RemoveChild($enabledNodes[0])
        }
        return [string]$document.OuterXml
    }
    catch {
        throw "Canonical task XML cannot produce an enablement-independent definition contract."
    }
}

function Get-DawnstrikeStatePreparationDeclaration {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [string]$GitPath = "",
        [string]$CandidateSha = "",
        [string]$CandidateTree = "",
        [string]$PythonPath = "",
        [ValidateRange(30, 1800)][int]$TimeoutSeconds = 300
    )

    if ([string]::IsNullOrWhiteSpace($GitPath)) {
        $GitPath = @(Get-Command git.exe -CommandType Application -ErrorAction Stop)[0].Source
    }
    if ([string]::IsNullOrWhiteSpace($CandidateSha) -or [string]::IsNullOrWhiteSpace($CandidateTree)) {
        $identity = Get-DawnstrikeGitContract $GitPath $CandidateRoot $TimeoutSeconds
        if ([string]::IsNullOrWhiteSpace($CandidateSha)) { $CandidateSha = [string]$identity.head }
        if ([string]::IsNullOrWhiteSpace($CandidateTree)) { $CandidateTree = [string]$identity.tree }
    }
    if ($CandidateSha -notmatch '^[0-9a-f]{40}$' -or $CandidateTree -notmatch '^[0-9a-f]{40}$') {
        throw "State-preparation declaration requires an exact candidate commit and tree."
    }
    $declaredTree = (Get-DawnstrikeGitValue $GitPath $CandidateRoot @(
        "rev-parse", ($CandidateSha + "^{tree}")
    ) "State-preparation declaration tree identity" $TimeoutSeconds).ToLowerInvariant()
    if ($declaredTree -ne $CandidateTree) {
        throw "State-preparation declaration candidate tree identity is invalid."
    }
    $path = Join-Path $CandidateRoot $script:DawnstrikeStatePreparationContractFile
    Assert-DawnstrikeNoReparseComponents $path "State-preparation declaration"

    # The working tree is not the authority for declaration presence.  A
    # delete/restore between the clean-check and this read must not turn a
    # sidecar-bearing commit into an implicit legacy activation (or substitute
    # a hostile declaration).  Bind both presence and the raw file bytes to
    # the exact commit object recorded by the exact tree.
    $relativePath = $script:DawnstrikeStatePreparationContractFile.Replace('\', '/')
    $treePaths = @(@(Get-DawnstrikeGitValue $GitPath $CandidateRoot @(
        "ls-tree", "-r", "--full-tree", "--name-only", $CandidateSha, "--", $relativePath
    ) "State-preparation declaration tree binding" $TimeoutSeconds) |
        ForEach-Object { ([string]$_).Split("`n", [System.StringSplitOptions]::RemoveEmptyEntries) } |
        Where-Object { $_ -ne "" })
    if ($treePaths.Count -eq 0) {
        if (Test-Path -LiteralPath $path) {
            throw "Candidate declaration exists but the exact candidate commit does not track it."
        }
        # Older runtimes predate the account/capture/trial sidecar.  Legacy
        # compatibility is valid only when the exact candidate commit truly
        # lacks the declaration.
        return [pscustomobject]@{
            required = $false
            path = $path
            declaration_present = $false
            declaration_blob_sha = ""
        }
    }
    if ($treePaths.Count -ne 1 -or $treePaths[0] -ne $relativePath) {
        throw "State-preparation declaration tree binding is not unique."
    }
    $declarationBlobSha = (Get-DawnstrikeGitValue $GitPath $CandidateRoot @(
        "rev-parse", ("{0}:{1}" -f $CandidateSha, $relativePath)
    ) "State-preparation declaration blob binding" $TimeoutSeconds).ToLowerInvariant()
    if ($declarationBlobSha -notmatch '^[0-9a-f]{40}$') {
        throw "State-preparation declaration blob identity is invalid."
    }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "State-preparation declaration is missing from the exact candidate checkout."
    }
    $workingBlobSha = (Get-DawnstrikeGitValue $GitPath $CandidateRoot @(
        "hash-object", ("--path={0}" -f $relativePath), "--", $path
    ) "State-preparation declaration working-tree binding" $TimeoutSeconds).ToLowerInvariant()
    if ($workingBlobSha -ne $declarationBlobSha) {
        throw "State-preparation declaration bytes do not match the exact candidate commit."
    }
    if ([string]::IsNullOrWhiteSpace($PythonPath)) {
        $PythonPath = @(Get-Command py.exe -CommandType Application -ErrorAction Stop)[0].Source
    }
    # Validate the raw bytes through the strict Python contract loader before
    # PowerShell parses them. ConvertFrom-Json silently accepts duplicate
    # properties (last value wins), which would let a hostile sidecar replace
    # an otherwise valid declaration at this activation boundary.
    $validated = Invoke-DawnstrikeContractCli `
        -PythonPath $PythonPath `
        -CandidateRoot $CandidateRoot `
        -Arguments @("validate-state-preparation-declaration", "--input", $path) `
        -Label "State-preparation declaration validation" `
        -TimeoutSeconds $TimeoutSeconds
    # Use the validated object returned by the same strict read.  Do not
    # reread the path with ConvertFrom-Json: a concurrent replacement between
    # reads would otherwise create a time-of-check/time-of-use gap.
    $declaration = $validated
    $workingBlobAfterValidation = (Get-DawnstrikeGitValue $GitPath $CandidateRoot @(
        "hash-object", ("--path={0}" -f $relativePath), "--", $path
    ) "State-preparation declaration post-validation binding" $TimeoutSeconds).ToLowerInvariant()
    if ($workingBlobAfterValidation -ne $declarationBlobSha) {
        throw "State-preparation declaration changed during strict validation."
    }
    return [pscustomobject]@{
        required = $true
        path = $path
        declaration_present = $true
        declaration_blob_sha = $declarationBlobSha
        sidecar_contract = [string]$declaration.sidecar_contract
    }
}

function Assert-DawnstrikeCandidateIdentityAndDeclaration {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [Parameter(Mandatory = $true)][string]$CandidateSha,
        [Parameter(Mandatory = $true)][string]$CandidateTree,
        [Parameter(Mandatory = $true)][object]$Declaration,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $identity = Get-DawnstrikeGitContract $GitPath $CandidateRoot $TimeoutSeconds $CandidateSha
    if ($identity.tree -ne $CandidateTree) {
        throw "Candidate checkout tree changed during activation."
    }
    $current = Get-DawnstrikeStatePreparationDeclaration `
        -CandidateRoot $CandidateRoot `
        -GitPath $GitPath `
        -CandidateSha $CandidateSha `
        -CandidateTree $CandidateTree `
        -PythonPath (Get-Command py.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1 -ExpandProperty Source) `
        -TimeoutSeconds $TimeoutSeconds
    if (
        [bool]$current.required -ne [bool]$Declaration.required -or
        [bool]$current.declaration_present -ne [bool]$Declaration.declaration_present -or
        [string]$current.declaration_blob_sha -ne [string]$Declaration.declaration_blob_sha
    ) {
        throw "Candidate declaration identity changed during activation."
    }
    return $identity
}

function Get-DawnstrikeAuxiliaryCaptureTask {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [switch]$AllowDisabled
    )

    $matches = @(Get-ScheduledTask -TaskName $script:DawnstrikeAuxiliaryCaptureTaskName -ErrorAction SilentlyContinue)
    if ($matches.Count -eq 0) {
        return [pscustomobject]@{
            present = $false
            task_name = $script:DawnstrikeAuxiliaryCaptureTaskName
            state = "ABSENT"
            enabled = $false
            xml = ""
            xml_sha256 = Get-DawnstrikeSha256Text ""
            xml_file_sha256 = Get-DawnstrikeSha256Text ""
            definition_contract_sha256 = Get-DawnstrikeSha256Text ""
            action_contract_sha256 = Get-DawnstrikeSha256Text ""
            task_path = "NONE"
        }
    }
    if ($matches.Count -ne 1) {
        throw "Auxiliary capture task name is not unique."
    }
    $task = $matches[0]
    $state = [string]$task.State
    if ($state -notin @("Ready", "Disabled")) {
        throw "Auxiliary capture task is not quiesceable: state=$state"
    }
    $actions = @($task.Actions)
    if ($actions.Count -lt 1) {
        throw "Auxiliary capture task has no action."
    }
    $actionText = ($actions | ForEach-Object {
        "{0}|{1}|{2}" -f $_.Execute, $_.Arguments, $_.WorkingDirectory
    }) -join "`n"
    if (
        -not $actionText.ToLowerInvariant().Contains($RuntimeRoot.ToLowerInvariant()) -or
        -not $actionText.ToLowerInvariant().Contains($StateRoot.ToLowerInvariant())
    ) {
        throw "Auxiliary capture task does not retain the fixed runtime/state roots."
    }
    $taskPath = [string]$task.TaskPath
    if ([string]::IsNullOrWhiteSpace($taskPath)) { $taskPath = "\" }
    $xml = [string](Export-ScheduledTask -TaskName $script:DawnstrikeAuxiliaryCaptureTaskName -TaskPath $taskPath -ErrorAction Stop)
    if ([string]::IsNullOrWhiteSpace($xml)) {
        throw "Auxiliary capture task export is empty."
    }
    return [pscustomobject]@{
        present = $true
        task_name = $script:DawnstrikeAuxiliaryCaptureTaskName
        state = $state
        enabled = ($state -eq "Ready")
        task_path = $taskPath
        xml = $xml
        xml_sha256 = Get-DawnstrikeSha256Text $xml
        xml_file_sha256 = Get-DawnstrikeSha256Text $xml
        definition_contract_sha256 = Get-DawnstrikeSha256Text (Get-DawnstrikeTaskDefinitionText $xml)
        action_contract_sha256 = Get-DawnstrikeSha256Text $actionText
    }
}

function Get-DawnstrikeAuxiliarySectionHash {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Xml,
        [Parameter(Mandatory = $true)][ValidateSet("Principal", "Triggers", "Settings")][string]$Name
    )
    try {
        $document = [System.Xml.XmlDocument]::new()
        $document.PreserveWhitespace = $true
        $document.LoadXml($Xml)
        $nodes = @($document.SelectNodes("//*[local-name()='$Name']"))
        if ($nodes.Count -ne 1) { throw "expected exactly one $Name section" }
        return Get-DawnstrikeSha256Text ([string]$nodes[0].OuterXml)
    }
    catch {
        throw "Auxiliary capture XML has an invalid $Name section."
    }
}

function Disable-DawnstrikeAuxiliaryCaptureTask {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot
    )
    $task = Get-DawnstrikeAuxiliaryCaptureTask $RuntimeRoot $StateRoot -AllowDisabled
    if (-not $task.present) { return $task }
    if ($task.state -eq "Ready") {
        Disable-ScheduledTask -TaskName $script:DawnstrikeAuxiliaryCaptureTaskName -TaskPath $task.task_path -ErrorAction Stop | Out-Null
    }
    $after = Get-DawnstrikeAuxiliaryCaptureTask $RuntimeRoot $StateRoot -AllowDisabled
    if (
        $after.state -ne "Disabled" -or
        $after.definition_contract_sha256 -ne $task.definition_contract_sha256 -or
        $after.action_contract_sha256 -ne $task.action_contract_sha256
    ) {
        throw "Auxiliary capture task did not enter the exact Disabled boundary."
    }
    return $after
}

function Restore-DawnstrikeAuxiliaryCaptureTask {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Expected,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [pscredential]$RunAsCredential
    )
    $current = Get-DawnstrikeAuxiliaryCaptureTask $RuntimeRoot $StateRoot -AllowDisabled
    if (-not $Expected.present) {
        if ($current.present) { throw "An auxiliary capture task appeared despite the absent-task policy." }
        return $current
    }
    if (-not $current.present) { throw "The receipt-bound auxiliary capture task is missing." }
    # Password-logon task XML cannot safely be re-registered without threading
    # a password through recovery.  Preserve the existing principal/triggers/
    # settings and restore only the action and enablement fields that governed
    # activation/rebind are allowed to change.
    $expectedDocument = [System.Xml.XmlDocument]::new()
    $expectedDocument.PreserveWhitespace = $true
    $expectedDocument.LoadXml([string]$Expected.xml)
    $currentDocument = [System.Xml.XmlDocument]::new()
    $currentDocument.PreserveWhitespace = $true
    $currentDocument.LoadXml([string]$current.xml)
    foreach ($sectionName in @("Principal", "Triggers", "Settings")) {
        $expectedNodes = @($expectedDocument.SelectNodes("//*[local-name()='$sectionName']"))
        $currentNodes = @($currentDocument.SelectNodes("//*[local-name()='$sectionName']"))
        if ($expectedNodes.Count -ne 1 -or $currentNodes.Count -ne 1) {
            throw "Auxiliary capture $sectionName policy is ambiguous during compensation."
        }
        if ($sectionName -eq "Settings") {
            $expectedEnabled = @($expectedNodes[0].ChildNodes | Where-Object { $_.LocalName -eq "Enabled" })
            $currentEnabled = @($currentNodes[0].ChildNodes | Where-Object { $_.LocalName -eq "Enabled" })
            if ($expectedEnabled.Count -gt 1 -or $currentEnabled.Count -gt 1) {
                throw "Auxiliary capture enablement policy is ambiguous during compensation."
            }
            if ($expectedEnabled.Count -eq 1 -and $currentEnabled.Count -eq 1) {
                $currentEnabled[0].InnerText = [string]$expectedEnabled[0].InnerText
            }
        }
        if ([string]$expectedNodes[0].OuterXml -ne [string]$currentNodes[0].OuterXml) {
            throw "Auxiliary capture principal, trigger, or settings policy drifted during compensation."
        }
    }
    $expectedActions = @($expectedDocument.SelectNodes("//*[local-name()='Actions']"))
    $currentActions = @($currentDocument.SelectNodes("//*[local-name()='Actions']"))
    if ($expectedActions.Count -ne 1 -or $currentActions.Count -ne 1) {
        throw "Auxiliary capture action policy is ambiguous during compensation."
    }
    if ([string]$expectedActions[0].OuterXml -ne [string]$currentActions[0].OuterXml) {
        if ($null -eq $RunAsCredential -or [string]::IsNullOrWhiteSpace($RunAsCredential.UserName)) {
            throw "Auxiliary action drift requires the locally prompted RunAsCredential for Password-task compensation."
        }
        $restorePassword = $RunAsCredential.GetNetworkCredential().Password
        if ([string]::IsNullOrWhiteSpace($restorePassword)) { throw "Auxiliary compensation credential is incomplete." }
        $expectedExec = @($expectedActions[0].ChildNodes | Where-Object { $_.LocalName -eq "Exec" })
        if ($expectedExec.Count -ne 1) { throw "Auxiliary capture action policy is invalid during compensation." }
        $command = @($expectedExec[0].ChildNodes | Where-Object { $_.LocalName -eq "Command" })
        $arguments = @($expectedExec[0].ChildNodes | Where-Object { $_.LocalName -eq "Arguments" })
        $working = @($expectedExec[0].ChildNodes | Where-Object { $_.LocalName -eq "WorkingDirectory" })
        if ($command.Count -ne 1 -or $arguments.Count -ne 1 -or $working.Count -ne 1) {
            throw "Auxiliary capture action contract is incomplete during compensation."
        }
        $restoreAction = New-ScheduledTaskAction `
            -Execute ([string]$command[0].InnerText) `
            -Argument ([string]$arguments[0].InnerText) `
            -WorkingDirectory ([string]$working[0].InnerText)
        Set-ScheduledTask -TaskName $script:DawnstrikeAuxiliaryCaptureTaskName `
            -TaskPath ([string]$Expected.task_path) -Action @($restoreAction) `
            -User $RunAsCredential.UserName -Password $restorePassword -ErrorAction Stop | Out-Null
    }
    $restored = Get-DawnstrikeAuxiliaryCaptureTask $RuntimeRoot $StateRoot -AllowDisabled
    $restoredDocument = [System.Xml.XmlDocument]::new()
    $restoredDocument.PreserveWhitespace = $true
    $restoredDocument.LoadXml([string]$restored.xml)
    foreach ($sectionName in @("Principal", "Triggers", "Actions")) {
        $expectedNodes = @($expectedDocument.SelectNodes("//*[local-name()='$sectionName']"))
        $restoredNodes = @($restoredDocument.SelectNodes("//*[local-name()='$sectionName']"))
        if ($expectedNodes.Count -ne 1 -or $restoredNodes.Count -ne 1 -or [string]$expectedNodes[0].OuterXml -ne [string]$restoredNodes[0].OuterXml) {
            throw "Auxiliary capture $sectionName was not restored exactly."
        }
    }
    $expectedSettings = @($expectedDocument.SelectNodes("//*[local-name()='Settings']"))
    $restoredSettings = @($restoredDocument.SelectNodes("//*[local-name()='Settings']"))
    if ($expectedSettings.Count -ne 1 -or $restoredSettings.Count -ne 1) { throw "Auxiliary capture settings are ambiguous after compensation." }
    $expectedEnabled = @($expectedSettings[0].ChildNodes | Where-Object { $_.LocalName -eq "Enabled" })
    $restoredEnabled = @($restoredSettings[0].ChildNodes | Where-Object { $_.LocalName -eq "Enabled" })
    if ($expectedEnabled.Count -eq 1 -and $restoredEnabled.Count -eq 1) { $restoredEnabled[0].InnerText = [string]$expectedEnabled[0].InnerText }
    if ($expectedEnabled.Count -ne $restoredEnabled.Count -or ($expectedEnabled.Count -eq 1 -and [string]$expectedSettings[0].OuterXml -ne [string]$restoredSettings[0].OuterXml)) {
        throw "Auxiliary capture settings were not restored exactly."
    }
    if ($Expected.enabled) {
        if ($restored.state -eq "Disabled") {
            Enable-ScheduledTask -TaskName $script:DawnstrikeAuxiliaryCaptureTaskName -TaskPath $restored.task_path -ErrorAction Stop | Out-Null
        }
    }
    else {
        if ($restored.state -eq "Ready") {
            Disable-ScheduledTask -TaskName $script:DawnstrikeAuxiliaryCaptureTaskName -TaskPath $restored.task_path -ErrorAction Stop | Out-Null
        }
    }
    $final = Get-DawnstrikeAuxiliaryCaptureTask $RuntimeRoot $StateRoot -AllowDisabled
    if (
        $final.task_path -ne [string]$Expected.task_path -or
        $final.definition_contract_sha256 -ne [string]$Expected.definition_contract_sha256 -or
        $final.enabled -ne [bool]$Expected.enabled -or
        $final.action_contract_sha256 -ne [string]$Expected.action_contract_sha256
    ) {
        throw "Auxiliary capture task action or enablement did not restore exactly."
    }
    return $final
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

    Assert-DawnstrikeNoReparseComponents $Path "Task XML backup file"
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
        Assert-DawnstrikeNoReparseComponents $Path "Task XML backup file"
        $parent = Split-Path -Parent ([System.IO.Path]::GetFullPath($Path))
        Assert-DawnstrikeNoReparseComponents $parent "Task XML backup root"
        if (Test-Path -LiteralPath $Path) {
            $existing = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
            if ($existing.PSIsContainer -or ($existing.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Task XML backup destination is not a regular file."
            }
        }
        [System.IO.File]::WriteAllText($Path, $Xml, $encoding)
        Assert-DawnstrikeNoReparseComponents $Path "Task XML backup file"
        $written = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
        if ($written.PSIsContainer -or ($written.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Task XML backup destination is not a regular file after write."
        }
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
        [Parameter(Mandatory = $true)][object]$TaskContract,
        [AllowNull()][object]$AuxiliaryCapture
    )

    if ($BackupName -notmatch '^runtime-(activation|rollback)-[0-9a-f]{24}$') {
        throw "Scheduler backup name is invalid."
    }
    $root = Join-Path $StateRoot "scheduler-backups"
    Assert-DawnstrikeNoReparseComponents $root "Scheduler backup root"
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
    Assert-DawnstrikeNoReparseComponents $root "Scheduler backup root"
    $final = Join-Path $root $BackupName
    Assert-DawnstrikeNoReparseComponents $final "Scheduler backup bundle"
    if (Test-Path -LiteralPath $final) {
        throw "Scheduler XML backup already exists and requires review."
    }
    $temporary = Join-Path $root (".incomplete-$BackupName-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $temporary -ErrorAction Stop | Out-Null
    Assert-DawnstrikeNoReparseComponents $temporary "Temporary scheduler backup bundle"
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
            Assert-DawnstrikeNoReparseComponents $xmlPath "Task XML backup file"
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
        if ($null -eq $AuxiliaryCapture) {
            $AuxiliaryCapture = Get-DawnstrikeAuxiliaryCaptureTask `
                -RuntimeRoot (Get-Location).Path -StateRoot $StateRoot
        }
        $auxiliaryEntry = [ordered]@{
            present = [bool]$AuxiliaryCapture.present
            task_name = $script:DawnstrikeAuxiliaryCaptureTaskName
            state_before = if ($AuxiliaryCapture.present) { [string]$AuxiliaryCapture.state } else { "ABSENT" }
            enabled_before = if ($AuxiliaryCapture.present) { [bool]$AuxiliaryCapture.enabled } else { $false }
            action = if ($AuxiliaryCapture.present) { "DISABLED_UNTIL_EXACT_SHA_REBIND" } else { "ABSENT_ALLOWED" }
        }
        if ($AuxiliaryCapture.present) {
            $auxiliaryFileName = "Dawnstrike_Delayed_SIP_Capture.xml"
            $auxiliaryPath = Join-Path $temporary $auxiliaryFileName
            $null = Write-DawnstrikeTaskXmlFile `
                -Xml ([string]$AuxiliaryCapture.xml) -Path $auxiliaryPath
            $auxiliaryEntry.file_name = $auxiliaryFileName
            $auxiliaryEntry.xml_sha256 = [string]$AuxiliaryCapture.xml_sha256
            $auxiliaryEntry.xml_file_sha256 = Get-DawnstrikeSha256File $auxiliaryPath
            $auxiliaryEntry.definition_contract_sha256 = [string]$AuxiliaryCapture.definition_contract_sha256
            $auxiliaryEntry.action_contract_sha256 = [string]$AuxiliaryCapture.action_contract_sha256
            $auxiliaryEntry.task_path = [string]$AuxiliaryCapture.task_path
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
            auxiliary_capture = $auxiliaryEntry
            research_only = $true
            broker_execution_enabled = $false
        }
        $manifestPath = Join-Path $temporary "manifest.json"
        Assert-DawnstrikeNoReparseComponents $manifestPath "Scheduler backup manifest"
        Write-DawnstrikeActivationJson $manifest $manifestPath
        Assert-DawnstrikeNoReparseComponents $temporary "Temporary scheduler backup bundle"
        Assert-DawnstrikeNoReparseComponents $final "Scheduler backup bundle"
        [System.IO.Directory]::Move($temporary, $final)
        Assert-DawnstrikeNoReparseComponents $final "Scheduler backup bundle"
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
    Assert-DawnstrikeNoReparseComponents $backupPath "Scheduler backup bundle"
    $backupItem = Get-Item -LiteralPath $backupPath -Force -ErrorAction Stop
    if (
        -not $backupItem.PSIsContainer -or
        ($backupItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
        throw "Scheduler XML backup is not a safe directory."
    }
    $manifestPath = Join-Path $backupPath "manifest.json"
    Assert-DawnstrikeNoReparseComponents $manifestPath "Scheduler backup manifest"
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
        Assert-DawnstrikeNoReparseComponents $manifestPath "Scheduler backup manifest"
        if ((Get-DawnstrikeSha256File $manifestPath) -ne $ExpectedManifestSha256) {
            throw "Scheduler backup manifest changed during read."
        }
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
    $auxiliary = $manifest.auxiliary_capture
    if ($null -eq $auxiliary) {
        throw "Scheduler XML backup does not attest auxiliary capture task policy."
    }
    $expectedChildren = @("manifest.json") + @($entries | ForEach-Object { [string]$_.file_name })
    if ($auxiliary.present -eq $true) {
        if ([string]$auxiliary.task_name -ne $script:DawnstrikeAuxiliaryCaptureTaskName) {
            throw "Scheduler XML backup auxiliary task name is invalid."
        }
        $expectedChildren += [string]$auxiliary.file_name
        if (
            [string]$auxiliary.state_before -notin @("Ready", "Disabled") -or
            [string]$auxiliary.action -ne "DISABLED_UNTIL_EXACT_SHA_REBIND" -or
            [string]$auxiliary.xml_sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]$auxiliary.xml_file_sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]$auxiliary.definition_contract_sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]$auxiliary.action_contract_sha256 -notmatch '^[0-9a-f]{64}$'
        ) {
            throw "Scheduler XML backup auxiliary task entry violates the exact contract."
        }
        $auxiliaryXmlPath = Join-Path $backupPath ([string]$auxiliary.file_name)
        Assert-DawnstrikeNoReparseComponents $auxiliaryXmlPath "Auxiliary scheduler XML backup"
        $auxiliaryXmlItem = Get-Item -LiteralPath $auxiliaryXmlPath -Force -ErrorAction Stop
        if (
            $auxiliaryXmlItem.PSIsContainer -or
            ($auxiliaryXmlItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
            (Get-DawnstrikeSha256File $auxiliaryXmlPath) -ne [string]$auxiliary.xml_file_sha256 -or
            (Get-DawnstrikeSha256Text ([System.IO.File]::ReadAllText($auxiliaryXmlPath))) -ne [string]$auxiliary.xml_sha256
        ) {
            throw "Scheduler XML backup auxiliary task file does not match its manifest."
        }
        Assert-DawnstrikeNoReparseComponents $auxiliaryXmlPath "Auxiliary scheduler XML backup"
        if ((Get-DawnstrikeSha256File $auxiliaryXmlPath) -ne [string]$auxiliary.xml_file_sha256) {
            throw "Scheduler XML backup auxiliary task file changed during read."
        }
    }
    elseif (
        $auxiliary.present -ne $false -or
        [string]$auxiliary.task_name -ne $script:DawnstrikeAuxiliaryCaptureTaskName -or
        [string]$auxiliary.state_before -ne "ABSENT" -or
        [string]$auxiliary.action -ne "ABSENT_ALLOWED"
    ) {
        throw "Scheduler XML backup absent auxiliary policy is invalid."
    }
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
        Assert-DawnstrikeNoReparseComponents $xmlPath "Scheduler task XML backup"
        $xmlItem = Get-Item -LiteralPath $xmlPath -Force -ErrorAction Stop
        if (
            $xmlItem.PSIsContainer -or
            ($xmlItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
            (Get-DawnstrikeSha256File $xmlPath) -ne [string]$entry.xml_file_sha256
        ) {
            throw "Scheduler XML backup task file does not match its manifest."
        }
        $xml = [System.IO.File]::ReadAllText($xmlPath)
        Assert-DawnstrikeNoReparseComponents $xmlPath "Scheduler task XML backup"
        if ((Get-DawnstrikeSha256File $xmlPath) -ne [string]$entry.xml_file_sha256) {
            throw "Scheduler task XML backup changed during read."
        }
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
    Assert-DawnstrikeNoReparseComponents $rollbackRoot "Receipt-bound rollback root"
    $rollbackRootItem = Get-Item -LiteralPath $rollbackRoot -Force -ErrorAction Stop
    if (
        -not $rollbackRootItem.PSIsContainer -or
        ($rollbackRootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
        throw "Receipt-bound rollback root is missing or unsafe."
    }
    $bundle = Join-Path $rollbackRoot "previous-runtime.bundle"
    Assert-DawnstrikeNoReparseComponents $bundle "Receipt-bound rollback bundle"
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
    Assert-DawnstrikeNoReparseComponents $stateBundle "Receipt-bound durable-state backup"
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
    if ($Receipt.PSObject.Properties.Name -contains "state_backup_bundle_path") {
        Assert-DawnstrikeNoReparseComponents ([string]$Receipt.state_backup_bundle_path) `
            "Receipt-bound durable-state backup path"
        $receiptStateBundle = Resolve-DawnstrikeActivationRoot `
            ([string]$Receipt.state_backup_bundle_path) `
            "Receipt-bound durable-state backup path"
        if (
            -not [string]::Equals(
                $receiptStateBundle,
                $stateBundle,
                [System.StringComparison]::OrdinalIgnoreCase
            ) -or
            [string]$stateResult.manifest_sha256 -ne [string]$Receipt.state_backup_manifest_sha256 -or
            [string]$stateResult.backup_logical_snapshot_sha256 -ne
                [string]$Receipt.state_backup_logical_snapshot_sha256 -or
            [string]$stateResult.source_logical_snapshot_sha256 -ne
                [string]$Receipt.state_backup_source_logical_snapshot_sha256
        ) {
            throw "Receipt-bound durable-state backup manifest or logical lineage does not match the activation receipt."
        }
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

    Assert-DawnstrikeNoReparseComponents $Path "Receipt output"
    $parent = Split-Path -Parent $Path
    Assert-DawnstrikeNoReparseComponents $parent "Receipt output root"
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    Assert-DawnstrikeNoReparseComponents $parent "Receipt output root"
    $temporary = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    Assert-DawnstrikeNoReparseComponents $temporary "Temporary receipt output"
    try {
        $json = $Payload | ConvertTo-Json -Depth 12
        [System.IO.File]::WriteAllText(
            $temporary,
            $json,
            [System.Text.UTF8Encoding]::new($false)
        )
        Assert-DawnstrikeNoReparseComponents $temporary "Temporary receipt output"
        $temporaryItem = Get-Item -LiteralPath $temporary -Force -ErrorAction Stop
        if ($temporaryItem.PSIsContainer -or ($temporaryItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Temporary receipt output is not a regular file."
        }
        Assert-DawnstrikeNoReparseComponents $Path "Receipt output"
        if (Test-Path -LiteralPath $Path) {
            throw "Receipt output already exists."
        }
        [System.IO.File]::Move($temporary, $Path)
        Assert-DawnstrikeNoReparseComponents $Path "Receipt output"
        $writtenItem = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
        if ($writtenItem.PSIsContainer -or ($writtenItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Receipt output is not a regular file after move."
        }
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

function Get-DawnstrikeStatePreparationProof {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$BackupRoot,
        [Parameter(Mandatory = $true)][string]$CandidateSha,
        [Parameter(Mandatory = $true)][string]$CandidateTree,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $tool = Join-Path $CandidateRoot "scripts\state_preparation.py"
    if (-not (Test-Path -LiteralPath $tool -PathType Leaf)) {
        throw "Candidate declares the sidecar contract but state-preparation tool is missing."
    }
    $receiptPath = Join-Path $StateRoot ("receipts\state-preparation\state-preparation-" + $CandidateSha + ".json")
    Assert-DawnstrikeNoReparseComponents $receiptPath "State-preparation receipt"
    if (-not (Test-Path -LiteralPath $receiptPath -PathType Leaf)) {
        throw "Matching COMPLETE state-preparation receipt is required before activation."
    }
    $receipt = Invoke-DawnstrikeActivationProcess `
        -FilePath $PythonPath `
        -ArgumentList @(
            $tool, "--db-path", (Join-Path $StateRoot "shadow_real.sqlite"),
            "--state-root", $StateRoot, "--backup-root", $BackupRoot,
            "--candidate-sha", $CandidateSha, "--candidate-tree", $CandidateTree,
            "--verify-receipt", $receiptPath
        ) `
        -WorkingDirectory $CandidateRoot `
        -Label "State-preparation receipt verification" `
        -TimeoutSeconds $TimeoutSeconds
    try { $parsed = [string]$receipt.Stdout | ConvertFrom-Json }
    catch { throw "State-preparation receipt verification did not return valid JSON." }
    if ($parsed.status -ne "COMPLETE") { throw "State-preparation receipt is not COMPLETE." }
    $liveProcess = Invoke-DawnstrikeActivationProcess `
        -FilePath $PythonPath `
        -ArgumentList @($tool, "--db-path", (Join-Path $StateRoot "shadow_real.sqlite"), "--inspect-live") `
        -WorkingDirectory $CandidateRoot `
        -Label "Live state-preparation inventory verification" `
        -TimeoutSeconds $TimeoutSeconds
    try { $live = [string]$liveProcess.Stdout | ConvertFrom-Json }
    catch { throw "Live state-preparation inventory verification did not return valid JSON." }
    if (
        [string]$live.db_sha256 -ne [string]$parsed.after_db_sha256 -or
        [string]$live.wal_sha256 -ne [string]$parsed.after_wal_sha256 -or
        [string]$live.shm_sha256 -ne [string]$parsed.after_shm_sha256 -or
        [string]$live.inventory_sha256 -ne [string]$parsed.inventory_sha256 -or
        [string]$live.logical_snapshot_sha256 -ne [string]$parsed.after_logical_snapshot_sha256 -or
        [int]$live.schema_marker -ne 30 -or
        [string]$live.quick_check -ne "ok"
    ) {
        throw "Live state does not match the COMPLETE state-preparation receipt."
    }
    $bundle = Resolve-DawnstrikeActivationRoot ([string]$parsed.backup_bundle_path) "State-preparation backup bundle"
    $backupRootResolved = Resolve-DawnstrikeActivationRoot $BackupRoot "BackupRoot"
    if (-not $bundle.StartsWith($backupRootResolved.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "State-preparation backup bundle is outside the supplied backup root."
    }
    $manifestPath = Join-Path $bundle "manifest.json"
    Assert-DawnstrikeNoReparseComponents $manifestPath "State-preparation backup manifest"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "State-preparation backup manifest is missing."
    }
    if ((Get-DawnstrikeSha256File $manifestPath) -ne [string]$parsed.backup_manifest_file_sha256) {
        throw "State-preparation backup manifest file hash does not match the receipt."
    }
    $stateVerification = Invoke-DawnstrikeActivationProcess `
        -FilePath $PythonPath `
        -ArgumentList @(
            (Join-Path $CandidateRoot "scripts\state_disaster_recovery.py"),
            "restore-verify", "--bundle", $bundle,
            "--target-db", (Join-Path $StateRoot "shadow_real.sqlite"),
            "--backup-root", $backupRootResolved, "--state-root", $StateRoot
        ) `
        -WorkingDirectory $CandidateRoot `
        -Label "State-preparation backup verification" `
        -TimeoutSeconds $TimeoutSeconds
    try { $verifiedBackup = [string]$stateVerification.Stdout | ConvertFrom-Json }
    catch { throw "State-preparation backup verification did not return valid JSON." }
    if (
        $verifiedBackup.status -ne "VERIFY" -or
        [string]$verifiedBackup.backup_id -ne [string]$parsed.backup_id -or
        [string]$verifiedBackup.bundle_path -ne $bundle -or
        [string]$verifiedBackup.manifest_sha256 -ne [string]$parsed.backup_manifest_sha256 -or
        [string]$verifiedBackup.backup_db_sha256 -ne [string]$parsed.backup_db_sha256 -or
        [string]$verifiedBackup.source_release_sha -ne $CandidateSha -or
        [int]$verifiedBackup.schema_version -ne 30 -or
        [string]$verifiedBackup.quick_check -ne "ok" -or
        $verifiedBackup.write_performed -ne $false -or
        $verifiedBackup.automatic_overwrite -ne $false
    ) { throw "State-preparation backup bundle does not match its COMPLETE receipt." }
    return [pscustomobject]@{
        receipt = $parsed
        receipt_sha256 = [string]$parsed.receipt_sha256
        receipt_file_sha256 = Get-DawnstrikeSha256File $receiptPath
        after_db_sha256 = [string]$parsed.after_db_sha256
        after_wal_sha256 = [string]$parsed.after_wal_sha256
        after_shm_sha256 = [string]$parsed.after_shm_sha256
        after_logical_snapshot_sha256 = [string]$parsed.after_logical_snapshot_sha256
        inventory_sha256 = [string]$parsed.inventory_sha256
        backup_id = [string]$parsed.backup_id
        backup_bundle_path = $bundle
        backup_db_sha256 = [string]$parsed.backup_db_sha256
        backup_manifest_sha256 = [string]$parsed.backup_manifest_sha256
        backup_manifest_file_sha256 = [string]$parsed.backup_manifest_file_sha256
    }
}

function Assert-DawnstrikeCaptureRebindChain {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$ActivationReceipt,
        [Parameter(Mandatory = $true)][object]$Auxiliary,
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$CandidateSha,
        [Parameter(Mandatory = $true)][string]$CandidateTree,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    if (-not $Auxiliary.present -or $Auxiliary.state -ne "Ready") {
        throw "A post-rebind capture chain requires the exact Ready auxiliary task."
    }
    $captureReceiptRoot = Join-Path $StateRoot "receipts\capture-task"
    Assert-DawnstrikeNoReparseComponents $captureReceiptRoot "Capture-task receipt root"
    $paths = @(Get-ChildItem -LiteralPath $captureReceiptRoot -Filter ("capture-task-rebind-" + $CandidateSha + ".json") -File -ErrorAction SilentlyContinue)
    foreach ($path in $paths) {
        Assert-DawnstrikeNoReparseComponents $path.FullName "Capture-task receipt"
    }
    if ($paths.Count -ne 1) { throw "Exactly one COMPLETE capture rebind receipt is required for a Ready auxiliary task." }
    $captureContract = Join-Path $CandidateRoot "scripts\capture_task_contract.py"
    $result = Invoke-DawnstrikeActivationProcess $PythonPath @(
        $captureContract, "verify-receipt", "--receipt", $paths[0].FullName,
        "--candidate-sha", $CandidateSha, "--candidate-tree", $CandidateTree
    ) $CandidateRoot "Capture rebind chain verification" $TimeoutSeconds
    try { $capture = [string]$result.Stdout | ConvertFrom-Json }
    catch { throw "Capture rebind chain verification did not return valid JSON." }
    $activationPath = Join-Path $StateRoot ("receipts\runtime-activation\runtime-activation-" + [string]$ActivationReceipt.activation_id + ".json")
    Assert-DawnstrikeNoReparseComponents $activationPath "Capture rebind activation receipt"
    if (-not (Test-Path -LiteralPath $activationPath -PathType Leaf)) {
        throw "Capture rebind activation receipt is missing."
    }
    $activationItem = Get-Item -LiteralPath $activationPath -Force -ErrorAction Stop
    if (
        [string]$capture.activation_id -ne [string]$ActivationReceipt.activation_id -or
        [string]$capture.activation_receipt_name -ne $activationItem.Name -or
        [string]$capture.activation_receipt_sha256 -ne (Get-DawnstrikeSha256File $activationItem) -or
        [string]$capture.xml_after_sha256 -ne [string]$Auxiliary.xml_sha256 -or
        [string]$capture.action_after_sha256 -ne [string]$Auxiliary.action_contract_sha256 -or
        [string]$capture.definition_after_sha256 -ne [string]$Auxiliary.definition_contract_sha256 -or
        (Get-DawnstrikeAuxiliarySectionHash ([string]$Auxiliary.xml) "Principal") -ne [string]$capture.principal_sha256 -or
        (Get-DawnstrikeAuxiliarySectionHash ([string]$Auxiliary.xml) "Triggers") -ne [string]$capture.trigger_sha256 -or
        (Get-DawnstrikeAuxiliarySectionHash ([string]$Auxiliary.xml) "Settings") -ne [string]$capture.settings_sha256
    ) { throw "Ready auxiliary task is not bound to the exact activation receipt chain." }
    if ([string]$capture.changed_field -ne "candidate_sha_and_input_bindings") {
        throw "Ready auxiliary task receipt does not attest the permitted input-binding transformation."
    }
    foreach ($binding in @(
        @("symbols-manifest-sha256", [string]$capture.symbols_manifest_sha256),
        @("entitlement-receipt-sha256", [string]$capture.entitlement_receipt_sha256),
        @("source-config-sha256", [string]$capture.source_config_sha256)
    )) {
        $bindingPattern = '(?i)(?<![A-Za-z0-9_-])--' + [regex]::Escape($binding[0]) + '(?:=|\s+)(?:"' + [regex]::Escape($binding[1]) + '"|' + [regex]::Escape($binding[1]) + ')(?![A-Za-z0-9])'
        if (@([regex]::Matches([string]$Auxiliary.xml, $bindingPattern)).Count -ne 1) {
            throw "Ready auxiliary task does not bind the supplied $($binding[0]) receipt hash."
        }
    }
    return $capture
}

function Enter-DawnstrikeRuntimeActivationLockCore {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$StateRoot)

    $lockRoot = Join-Path $StateRoot "locks"
    Assert-DawnstrikeNoReparseComponents $lockRoot "Runtime activation lock root"
    New-Item -ItemType Directory -Path $lockRoot -Force | Out-Null
    Assert-DawnstrikeNoReparseComponents $lockRoot "Runtime activation lock root"
    $path = Join-Path $lockRoot "dawnstrike-runtime-activation.lock"
    Assert-DawnstrikeNoReparseComponents $path "Runtime activation lock"
    if (Test-Path -LiteralPath $path) {
        throw "A runtime activation lock already exists and requires review."
    }
    if (Get-Command Get-DawnstrikeLockSnapshot -ErrorAction SilentlyContinue) {
        $dailyBefore = @(
            Get-ChildItem -LiteralPath $lockRoot -Filter "dawnstrike-daily-*.lock" -File -Force |
                ForEach-Object {
                    $null = Get-DawnstrikeLockSnapshot -Path $_.FullName -Label "Counterpart daily run lock"
                    $_
                }
        )
        if ($dailyBefore.Count -gt 0) {
            throw "A daily run lock exists; runtime activation is not permitted."
        }
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
    Assert-DawnstrikeNoReparseComponents $path "Runtime activation lock"
    $snapshot = if (Get-Command Get-DawnstrikeLockSnapshot -ErrorAction SilentlyContinue) {
        Get-DawnstrikeLockSnapshot -Path $path -Label "Runtime activation lock"
    }
    else { $null }
    if (
        $null -ne $snapshot -and
        (-not $snapshot.present -or $snapshot.lock_token -ne $token)
    ) { throw "Runtime activation lock could not be read back with its own token." }
    if ($null -ne $snapshot) {
        $dailyAfter = @(
            Get-ChildItem -LiteralPath $lockRoot -Filter "dawnstrike-daily-*.lock" -File -Force |
                ForEach-Object {
                    $null = Get-DawnstrikeLockSnapshot -Path $_.FullName -Label "Counterpart daily run lock"
                    $_
                }
        )
        if ($dailyAfter.Count -gt 0) {
            $owned = [pscustomobject]@{
                acquired = $true
                path = $path
                token = $token
                bytes_sha256 = $snapshot.bytes_sha256
            }
            $current = Get-DawnstrikeLockSnapshot -Path $path -Label "Runtime activation lock"
            if (
                $current.present -and
                $current.lock_token -eq $token -and
                $current.bytes_sha256 -eq $snapshot.bytes_sha256
            ) {
                Remove-Item -LiteralPath $path -Force -ErrorAction Stop
                $removed = Get-DawnstrikeLockSnapshot -Path $path -Label "Runtime activation lock" -AllowMissing
                if ($removed.present) { throw "Runtime activation lock could not be relinquished after a conflict." }
            }
            throw "A daily run lock appeared during runtime activation lock acquisition."
        }
    }
    return [pscustomobject]@{
        path = $path
        token = $token
        bytes_sha256 = if ($null -ne $snapshot) { [string]$snapshot.bytes_sha256 } else { $null }
        acquired = $true
    }
}

function Enter-DawnstrikeRuntimeActivationLock {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$StateRoot)

    if (-not (Get-Command Enter-DawnstrikeLockOperationMutex -ErrorAction SilentlyContinue)) {
        return Enter-DawnstrikeRuntimeActivationLockCore @PSBoundParameters
    }
    $mutex = Enter-DawnstrikeLockOperationMutex
    try {
        return Enter-DawnstrikeRuntimeActivationLockCore @PSBoundParameters
    }
    finally {
        Exit-DawnstrikeLockOperationMutex $mutex
    }
}

function Exit-DawnstrikeRuntimeActivationLockCore {
    [CmdletBinding()]
    param([AllowNull()][object]$Lock)

    if ($null -eq $Lock -or -not (Test-Path -LiteralPath $Lock.path -PathType Leaf)) {
        return
    }
    try { Assert-DawnstrikeNoReparseComponents $Lock.path "Runtime activation lock" }
    catch { return }
    try {
        if (Get-Command Get-DawnstrikeLockSnapshot -ErrorAction SilentlyContinue) {
            $snapshot = Get-DawnstrikeLockSnapshot -Path $Lock.path -Label "Runtime activation lock"
            if (
                $snapshot.present -and
                [string]$snapshot.lock_token -eq [string]$Lock.token -and
                (
                    [string]::IsNullOrWhiteSpace([string]$Lock.bytes_sha256) -or
                    [string]$snapshot.bytes_sha256 -eq [string]$Lock.bytes_sha256
                )
            ) {
                Remove-Item -LiteralPath $Lock.path -Force
                $after = Get-DawnstrikeLockSnapshot -Path $Lock.path -Label "Runtime activation lock" -AllowMissing
                if ($after.present) { return }
            }
        }
        else {
            $payload = Get-Content -LiteralPath $Lock.path -Raw | ConvertFrom-Json
            if ([string]$payload.lock_token -eq [string]$Lock.token) {
                Remove-Item -LiteralPath $Lock.path -Force
            }
        }
    }
    catch {
        # Never delete a lock whose ownership cannot be proven.
    }
}

function Exit-DawnstrikeRuntimeActivationLock {
    [CmdletBinding()]
    param([AllowNull()][object]$Lock)

    if (-not (Get-Command Enter-DawnstrikeLockOperationMutex -ErrorAction SilentlyContinue)) {
        return Exit-DawnstrikeRuntimeActivationLockCore @PSBoundParameters
    }
    $mutex = Enter-DawnstrikeLockOperationMutex
    try {
        Exit-DawnstrikeRuntimeActivationLockCore @PSBoundParameters
    }
    finally {
        Exit-DawnstrikeLockOperationMutex $mutex
    }
}

function Assert-DawnstrikeNoDailyLocks {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$StateRoot)

    $lockRoot = Join-Path $StateRoot "locks"
    if (-not (Test-Path -LiteralPath $lockRoot -PathType Container)) { return }
    Assert-DawnstrikeNoReparseComponents $lockRoot "Daily lock root"
    $dailyLocks = @(
        Get-ChildItem -LiteralPath $lockRoot -Filter "dawnstrike-daily-*.lock" -File -Force |
            ForEach-Object {
                if (Get-Command Get-DawnstrikeLockSnapshot -ErrorAction SilentlyContinue) {
                    $null = Get-DawnstrikeLockSnapshot -Path $_.FullName -Label "Daily run lock"
                }
                $_
            }
    )
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

# Override the legacy per-script implementation before any operation runs.
. (Join-Path $PSScriptRoot "runtime_activation_lock.ps1")

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
        [pscredential]$RunAsCredential,
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
    $stateDeclaration = Get-DawnstrikeStatePreparationDeclaration `
        -CandidateRoot $candidate `
        -GitPath $gitPath `
        -CandidateSha $candidateContract.head `
        -CandidateTree $candidateContract.tree `
        -PythonPath $pythonPath `
        -TimeoutSeconds $ProcessTimeoutSeconds
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

    $statePreparation = $null
    if ($stateDeclaration.required) {
        $statePreparation = Get-DawnstrikeStatePreparationProof `
            -CandidateRoot $candidate `
            -StateRoot $state `
            -BackupRoot $backupRoot `
            -CandidateSha $ExpectedSha `
            -CandidateTree $candidateContract.tree `
            -PythonPath $pythonPath `
            -TimeoutSeconds $ProcessTimeoutSeconds
    }
    $null = Assert-DawnstrikeCandidateIdentityAndDeclaration `
        -GitPath $gitPath `
        -CandidateRoot $candidate `
        -CandidateSha $ExpectedSha `
        -CandidateTree $candidateContract.tree `
        -Declaration $stateDeclaration `
        -TimeoutSeconds $ProcessTimeoutSeconds

    $runtimeContract = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds
    if ($runtimeContract.head -eq $ExpectedSha) {
        $receiptRoot = Join-Path $state "receipts\runtime-activation"
        Assert-DawnstrikeNoReparseComponents $receiptRoot "Activation receipt root"
        $existing = @(
            Get-ChildItem -LiteralPath $receiptRoot -Filter "runtime-activation-*.json" -File -ErrorAction SilentlyContinue |
                ForEach-Object {
                    Assert-DawnstrikeNoReparseComponents $_.FullName "Activation receipt"
                    $_
                } | Sort-Object LastWriteTimeUtc -Descending
        )
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
                    if ($stateDeclaration.required) {
                        $existingAuxiliary = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
                        if ([bool]$receipt.auxiliary_capture_present -ne [bool]$existingAuxiliary.present) {
                            throw "Existing activation receipt does not match auxiliary capture presence."
                        }
                        if ($existingAuxiliary.present) {
                            if ($existingAuxiliary.state -eq "Disabled") {
                                if (
                                    $existingAuxiliary.definition_contract_sha256 -ne [string]$receipt.auxiliary_capture_definition_contract_sha256 -or
                                    $existingAuxiliary.action_contract_sha256 -ne [string]$receipt.auxiliary_capture_action_contract_sha256
                                ) { throw "Existing activation receipt does not match the disabled auxiliary task." }
                            }
                            elseif ($existingAuxiliary.state -eq "Ready") {
                                $null = Assert-DawnstrikeCaptureRebindChain `
                                    -ActivationReceipt $receipt -Auxiliary $existingAuxiliary `
                                    -CandidateRoot $candidate -StateRoot $state -CandidateSha $ExpectedSha `
                                    -CandidateTree $candidateContract.tree -PythonPath $pythonPath `
                                    -TimeoutSeconds $ProcessTimeoutSeconds
                            }
                            else { throw "Existing auxiliary capture task is in an ambiguous state." }
                        }
                        if (
                            [string]$receipt.state_preparation_receipt_sha256 -ne [string]$statePreparation.receipt_sha256 -or
                            [string]$receipt.state_preparation_after_db_sha256 -ne [string]$statePreparation.after_db_sha256 -or
                            [string]$receipt.state_preparation_inventory_sha256 -ne [string]$statePreparation.inventory_sha256
                        ) { throw "Existing activation receipt does not match live state preparation." }
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
    $null = Assert-DawnstrikeCandidateIdentityAndDeclaration `
        -GitPath $gitPath `
        -CandidateRoot $candidate `
        -CandidateSha $ExpectedSha `
        -CandidateTree $candidateContract.tree `
        -Declaration $stateDeclaration `
        -TimeoutSeconds $ProcessTimeoutSeconds
    Assert-DawnstrikeNoDailyLocks $state
    $taskBefore = Get-DawnstrikeTaskContract $runtime $state
    # Inventory the auxiliary independently of the candidate declaration.  A
    # present task without an explicit sidecar contract is an ungoverned task,
    # never an implicit legacy-compatible absence.
    $auxiliaryBefore = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
    if ($auxiliaryBefore.present -and -not $stateDeclaration.required) {
        throw "Auxiliary capture task is present but the candidate does not declare its governed sidecar contract."
    }

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
            auxiliary_capture_present = [bool]$auxiliaryBefore.present
            auxiliary_capture_state_before = if ($auxiliaryBefore.present) { [string]$auxiliaryBefore.state } else { "ABSENT" }
            auxiliary_capture_state_after = if ($auxiliaryBefore.present) { [string]$auxiliaryBefore.state } else { "ABSENT" }
            auxiliary_capture_action = if ($auxiliaryBefore.present) { "PREPARED_FOR_QUIESCE" } else { "ABSENT_ALLOWED" }
            research_only = $true
            broker_execution_enabled = $false
        }
    }

    $activationSeed = "$ExpectedSha`:$($runtimeContract.head)`:$MarketDate`:$($evidence.ci_evidence_sha256)`:$($evidence.sol_evidence_sha256)"
    $activationId = (Get-DawnstrikeSha256Text $activationSeed).Substring(0, 24)
    $stage = "$runtime.stage-$activationId"
    $null = Assert-DawnstrikeCandidateIdentityAndDeclaration `
        -GitPath $gitPath `
        -CandidateRoot $candidate `
        -CandidateSha $ExpectedSha `
        -CandidateTree $candidateContract.tree `
        -Declaration $stateDeclaration `
        -TimeoutSeconds $ProcessTimeoutSeconds
    $rollbackRoot = Join-Path $state "runtime-rollbacks\$activationId"
    $rollbackCheckout = Join-Path $rollbackRoot "previous-runtime"
    $rollbackBundle = Join-Path $rollbackRoot "previous-runtime.bundle"
    $receiptRoot = Join-Path $state "receipts\runtime-activation"
    $schedulerBackupName = "runtime-activation-$activationId"
    $schedulerBackupPath = Join-Path $state "scheduler-backups\$schedulerBackupName"
    $preparedReceipt = Join-Path $receiptRoot "runtime-activation-$activationId.prepared.json"
    $completeReceipt = Join-Path $receiptRoot "runtime-activation-$activationId.json"
    Assert-DawnstrikeNoReparseComponents $receiptRoot "Activation receipt root"
    Assert-DawnstrikeNoReparseComponents $preparedReceipt "Prepared activation receipt"
    Assert-DawnstrikeNoReparseComponents $completeReceipt "Complete activation receipt"
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
        if ($stateDeclaration.required) {
            $currentAuxiliary = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
            if ([bool]$existing.auxiliary_capture_present -ne [bool]$currentAuxiliary.present) {
                throw "Existing activation receipt does not match auxiliary capture presence."
            }
            if ($currentAuxiliary.present) {
                if ($currentAuxiliary.state -eq "Disabled") {
                    if (
                        $currentAuxiliary.definition_contract_sha256 -ne [string]$existing.auxiliary_capture_definition_contract_sha256 -or
                        $currentAuxiliary.action_contract_sha256 -ne [string]$existing.auxiliary_capture_action_contract_sha256
                    ) { throw "Existing activation receipt does not match the disabled auxiliary task." }
                }
                elseif ($currentAuxiliary.state -eq "Ready") {
                    $null = Assert-DawnstrikeCaptureRebindChain `
                        -ActivationReceipt $existing -Auxiliary $currentAuxiliary `
                        -CandidateRoot $candidate -StateRoot $state -CandidateSha $ExpectedSha `
                        -CandidateTree $current.tree -PythonPath $pythonPath `
                        -TimeoutSeconds $ProcessTimeoutSeconds
                }
                else { throw "Existing auxiliary capture task is in an ambiguous state." }
            }
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
        $auxiliaryDisabled = $false
        $preserveLocks = $false
        try {
            $lockOrigin = Convert-DawnstrikeCanonicalOriginIdentity $origin
            $lockPythonSha = Get-DawnstrikeRuntimeLockHash $pythonPath
            $activationLock = Enter-DawnstrikeGovernedRuntimeLock -StateRoot $state -Operation runtime_activation `
                -CandidateSha $ExpectedSha -CandidateTree ([string]$candidateContract.tree) `
                -OriginIdentity $lockOrigin -PythonPath $pythonPath -PythonSha256 $lockPythonSha
            Assert-DawnstrikeNoDailyLocks $state
            $dailyLock = Enter-DawnstrikeDailyRunLock -StateRoot $state -MarketDate $MarketDate -Owner "runtime_activation"
            if (-not $dailyLock.acquired) {
                throw "Runtime activation could not acquire the daily run lock."
            }
            Confirm-DawnstrikeActivationDailyLockHandshake `
                -StateRoot $state -ActivationLock $activationLock -DailyLock $dailyLock | Out-Null
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
                -TaskContract $taskLocked `
                -AuxiliaryCapture $auxiliaryBefore
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
            if ($auxiliaryBefore.present) {
                $auxiliaryDisabled = $true
                $auxiliaryBoundary = Disable-DawnstrikeAuxiliaryCaptureTask $runtime $state
                if ($auxiliaryBoundary.xml_sha256 -ne $auxiliaryBefore.xml_sha256) {
                    throw "Auxiliary capture task XML changed while entering the disabled boundary."
                }
            }
            if ($stateDeclaration.required) {
                # Re-read every database sidecar hash only after both locks and
                # all six task quiescence are proven.  This closes the WAL
                # and online-snapshot TOCTOU between preflight and swap.
                $statePreparationLocked = Get-DawnstrikeStatePreparationProof `
                    -CandidateRoot $candidate `
                    -StateRoot $state `
                    -BackupRoot $backupRoot `
                    -CandidateSha $ExpectedSha `
                    -CandidateTree $candidateContract.tree `
                    -PythonPath $pythonPath `
                    -TimeoutSeconds $ProcessTimeoutSeconds
                if (
                    $statePreparationLocked.receipt_sha256 -ne $statePreparation.receipt_sha256 -or
                    $statePreparationLocked.after_db_sha256 -ne $statePreparation.after_db_sha256 -or
                    $statePreparationLocked.after_wal_sha256 -ne $statePreparation.after_wal_sha256 -or
                    $statePreparationLocked.after_shm_sha256 -ne $statePreparation.after_shm_sha256 -or
                    $statePreparationLocked.after_logical_snapshot_sha256 -ne $statePreparation.after_logical_snapshot_sha256 -or
                    $statePreparationLocked.inventory_sha256 -ne $statePreparation.inventory_sha256
                ) { throw "Durable state changed after task quiescence; WAL or inventory drift is untrusted." }
            }
            $stateLocked = Invoke-DawnstrikeContractCli $pythonPath $candidate @("inspect-state", "--db-path", $dbPath) "Locked durable state validation" $ProcessTimeoutSeconds
            if ($stateLocked.main_file_sha256 -ne $stateInfo.main_file_sha256) {
                throw "Durable state changed during activation preflight."
            }

            $backupId = "runtime-activation-$activationId"
            $backupTool = Join-Path $candidate "scripts\state_disaster_recovery.py"
            Assert-DawnstrikeNoReparseComponents $backupTool "Durable-state backup tool"
            if (-not (Test-Path -LiteralPath $backupTool -PathType Leaf)) {
                throw "Durable-state backup tool is missing."
            }
            $backupArguments = @(
                $backupTool,
                "backup", "--source-db", $dbPath, "--backup-root", $backupRoot,
                "--state-root", $state, "--retention", [string]$BackupRetention,
                "--source-sha", $runtimeContract.head, "--backup-id", $backupId
            )
            if ($stateDeclaration.required) {
                if ($null -eq $statePreparationLocked) {
                    throw "Locked state-preparation proof is required before the activation backup."
                }
                foreach ($lockedHash in @(
                    $statePreparationLocked.after_db_sha256,
                    $statePreparationLocked.after_wal_sha256,
                    $statePreparationLocked.after_shm_sha256,
                    $statePreparationLocked.after_logical_snapshot_sha256
                )) {
                    if ([string]$lockedHash -notmatch '^[0-9a-f]{64}$') {
                        throw "Locked state-preparation snapshot hash is invalid."
                    }
                }
                $backupArguments += @(
                    "--expected-db-sha256", [string]$statePreparationLocked.after_db_sha256,
                    "--expected-wal-sha256", [string]$statePreparationLocked.after_wal_sha256,
                    "--expected-shm-sha256", [string]$statePreparationLocked.after_shm_sha256,
                    "--expected-logical-snapshot-sha256",
                    [string]$statePreparationLocked.after_logical_snapshot_sha256
                )
            }
            $backup = Invoke-DawnstrikeActivationProcess `
                -FilePath $pythonPath `
                -ArgumentList $backupArguments `
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
            $backupBundlePath = $null
            $backupManifestSha256 = $null
            $backupLogicalSnapshotSha256 = $null
            $backupSourceLogicalSnapshotSha256 = $null
            if ($stateDeclaration.required) {
                if (
                    [string]$backupResult.source_live_main_file_sha256 -ne
                        [string]$statePreparationLocked.after_db_sha256 -or
                    [string]$backupResult.backup_db_sha256 -notmatch '^[0-9a-f]{64}$' -or
                    [string]$backupResult.source_logical_snapshot_sha256 -ne
                        [string]$statePreparationLocked.after_logical_snapshot_sha256 -or
                    [string]$backupResult.backup_logical_snapshot_sha256 -ne
                        [string]$statePreparationLocked.after_logical_snapshot_sha256 -or
                    [string]$backupResult.manifest_sha256 -notmatch '^[0-9a-f]{64}$' -or
                    [string]$backupResult.bundle_path -eq ""
                ) {
                    throw "SQLite backup did not return the exact locked snapshot lineage."
                }
                $backupBundlePath = Resolve-DawnstrikeActivationRoot `
                    ([string]$backupResult.bundle_path) `
                    "Durable-state backup bundle"
                $backupRootResolved = Resolve-DawnstrikeActivationRoot $backupRoot "BackupRoot"
                if (
                    -not $backupBundlePath.StartsWith(
                        $backupRootResolved.TrimEnd('\') + '\',
                        [System.StringComparison]::OrdinalIgnoreCase
                    ) -or
                    [System.IO.Path]::GetFileName($backupBundlePath) -ne $backupId
                ) {
                    throw "Durable-state backup bundle is outside the expected backup root."
                }
                foreach ($backupFileName in @("shadow_real.sqlite", "manifest.json", "receipt.json")) {
                    $backupFile = Join-Path $backupBundlePath $backupFileName
                    Assert-DawnstrikeNoReparseComponents $backupFile "Durable-state backup $backupFileName"
                    if (-not (Test-Path -LiteralPath $backupFile -PathType Leaf)) {
                        throw "Durable-state backup is missing $backupFileName."
                    }
                }
                $backupVerification = Invoke-DawnstrikeActivationProcess `
                    -FilePath $pythonPath `
                    -ArgumentList @(
                        $backupTool, "restore-verify", "--bundle", $backupBundlePath,
                        "--target-db", $dbPath, "--backup-root", $backupRoot,
                        "--state-root", $state
                    ) `
                    -WorkingDirectory $candidate `
                    -Label "Pre-activation durable-state backup lineage verification" `
                    -TimeoutSeconds $ProcessTimeoutSeconds
                try { $backupVerificationResult = [string]$backupVerification.Stdout | ConvertFrom-Json }
                catch { throw "Durable-state backup lineage verification did not return valid JSON." }
                if (
                    [string]$backupVerificationResult.status -ne "VERIFY" -or
                    [string]$backupVerificationResult.backup_id -ne $backupId -or
                    [string]$backupVerificationResult.bundle_path -ne $backupBundlePath -or
                    [string]$backupVerificationResult.manifest_sha256 -ne [string]$backupResult.manifest_sha256 -or
                    [string]$backupVerificationResult.backup_db_sha256 -ne [string]$backupResult.backup_db_sha256 -or
                    [string]$backupVerificationResult.backup_logical_snapshot_sha256 -ne [string]$backupResult.backup_logical_snapshot_sha256 -or
                    [string]$backupVerificationResult.source_logical_snapshot_sha256 -ne [string]$backupResult.source_logical_snapshot_sha256 -or
                    [string]$backupVerificationResult.source_release_sha -ne [string]$runtimeContract.head -or
                    [int]$backupVerificationResult.schema_version -ne [int]$stateInfo.schema_version -or
                    [string]$backupVerificationResult.quick_check -ne "ok" -or
                    $backupVerificationResult.write_performed -ne $false -or
                    $backupVerificationResult.automatic_overwrite -ne $false
                ) {
                    throw "Durable-state backup manifest or receipt lineage does not match the locked proof."
                }
                $backupManifestSha256 = [string]$backupResult.manifest_sha256
                $backupLogicalSnapshotSha256 = [string]$backupResult.backup_logical_snapshot_sha256
                $backupSourceLogicalSnapshotSha256 = [string]$backupResult.source_logical_snapshot_sha256
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
            if ($stateDeclaration.required) {
                $auxiliaryBackupManifest = Get-Content `
                    -LiteralPath (Join-Path $taskBackup.backup_path "manifest.json") `
                    -Raw -Encoding UTF8 | ConvertFrom-Json
                $receiptPayload.state_preparation_required = $true
                $receiptPayload.state_preparation_contract = [string]$stateDeclaration.sidecar_contract
                $receiptPayload.state_preparation_receipt_sha256 = [string]$statePreparation.receipt_sha256
                $receiptPayload.state_preparation_after_db_sha256 = [string]$statePreparation.after_db_sha256
                $receiptPayload.state_preparation_after_wal_sha256 = [string]$statePreparation.after_wal_sha256
                $receiptPayload.state_preparation_after_shm_sha256 = [string]$statePreparation.after_shm_sha256
                $receiptPayload.state_preparation_after_logical_snapshot_sha256 = [string]$statePreparation.after_logical_snapshot_sha256
                $receiptPayload.state_preparation_inventory_sha256 = [string]$statePreparation.inventory_sha256
                $receiptPayload.state_preparation_backup_id = [string]$statePreparation.backup_id
                $receiptPayload.state_preparation_backup_bundle_path = [string]$statePreparation.backup_bundle_path
                $receiptPayload.state_preparation_backup_db_sha256 = [string]$statePreparation.backup_db_sha256
                $receiptPayload.state_preparation_backup_manifest_sha256 = [string]$statePreparation.backup_manifest_sha256
                $receiptPayload.state_preparation_backup_manifest_file_sha256 = [string]$statePreparation.backup_manifest_file_sha256
                $receiptPayload.state_backup_bundle_path = [string]$backupBundlePath
                $receiptPayload.state_backup_manifest_sha256 = [string]$backupManifestSha256
                $receiptPayload.state_backup_logical_snapshot_sha256 = [string]$backupLogicalSnapshotSha256
                $receiptPayload.state_backup_source_logical_snapshot_sha256 = [string]$backupSourceLogicalSnapshotSha256
                $receiptPayload.auxiliary_capture_present = [bool]$auxiliaryBefore.present
                $receiptPayload.auxiliary_capture_state_before = if ($auxiliaryBefore.present) { [string]$auxiliaryBefore.state } else { "ABSENT" }
                $receiptPayload.auxiliary_capture_state_after = if ($auxiliaryBefore.present) { "Disabled" } else { "ABSENT" }
                $receiptPayload.auxiliary_capture_action = if ($auxiliaryBefore.present) { "DISABLED_UNTIL_EXACT_SHA_REBIND" } else { "ABSENT_ALLOWED" }
                $receiptPayload.auxiliary_capture_xml_sha256 = [string]$auxiliaryBefore.xml_sha256
                $receiptPayload.auxiliary_capture_xml_file_sha256 = if ($auxiliaryBefore.present) {
                    [string]$auxiliaryBackupManifest.auxiliary_capture.xml_file_sha256
                }
                else {
                    Get-DawnstrikeSha256Text ""
                }
                $receiptPayload.auxiliary_capture_definition_contract_sha256 = [string]$auxiliaryBefore.definition_contract_sha256
                $receiptPayload.auxiliary_capture_action_contract_sha256 = [string]$auxiliaryBefore.action_contract_sha256
                $receiptPayload.auxiliary_capture_backup_name = if ($auxiliaryBefore.present) { [string]$taskBackup.backup_name } else { "NONE" }
                $receiptPayload.auxiliary_capture_backup_manifest_sha256 = [string]$taskBackup.manifest_sha256
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

            # The candidate checkout remains the source of executable tools
            # until the exact staged checkout is installed. Reassert its
            # commit/tree and declaration binding at the last pre-swap
            # boundary so a delete/restore cannot alter activation semantics.
            $null = Assert-DawnstrikeCandidateIdentityAndDeclaration `
                -GitPath $gitPath `
                -CandidateRoot $candidate `
                -CandidateSha $ExpectedSha `
                -CandidateTree $candidateContract.tree `
                -Declaration $stateDeclaration `
                -TimeoutSeconds $ProcessTimeoutSeconds

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
            $auxiliaryAfterDisabled = if ($stateDeclaration.required) {
                Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
            }
            else {
                $auxiliaryBefore
            }
            if ($auxiliaryBefore.present) {
                if (
                    -not $auxiliaryAfterDisabled.present -or
                    $auxiliaryAfterDisabled.state -ne "Disabled" -or
                    $auxiliaryAfterDisabled.definition_contract_sha256 -ne $auxiliaryBefore.definition_contract_sha256 -or
                    $auxiliaryAfterDisabled.action_contract_sha256 -ne $auxiliaryBefore.action_contract_sha256
                ) { throw "Auxiliary capture task changed across the runtime swap." }
            }
            elseif ($auxiliaryAfterDisabled.present) {
                throw "An auxiliary capture task appeared during the runtime swap."
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
            $auxiliaryAfter = if ($stateDeclaration.required) {
                Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
            }
            else {
                $auxiliaryBefore
            }
            if ($auxiliaryBefore.present) {
                if ($auxiliaryAfter.state -ne "Disabled" -or $auxiliaryAfter.definition_contract_sha256 -ne $auxiliaryBefore.definition_contract_sha256) {
                    throw "Auxiliary capture task must remain Disabled until exact-SHA rebind."
                }
            }
            elseif ($auxiliaryAfter.present) {
                throw "An auxiliary capture task appeared after activation."
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
                    if ($stateDeclaration.required -and $auxiliaryBefore.present) {
                        $null = Disable-DawnstrikeAuxiliaryCaptureTask $runtime $state
                        $auxiliaryDisabled = $true
                    }
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
                        if ($stateDeclaration.required) {
                            $null = Restore-DawnstrikeAuxiliaryCaptureTask `
                                -Expected $auxiliaryBefore `
                                -RuntimeRoot $runtime `
                                -StateRoot $state `
                                -RunAsCredential $RunAsCredential
                            $auxiliaryDisabled = $false
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
                throw "Runtime activation failed and automatic restore could not be completed; canonical tasks are proven Disabled and the prepared receipt/rollback tool are required. Original failure: $($failure.Exception.Message)"
                }
            }
            throw $failure
        }
        finally {
            if (-not $preserveLocks) {
                if ($null -ne $dailyLock) { Exit-DawnstrikeDailyRunLock -Lock $dailyLock }
                Exit-DawnstrikeGovernedRuntimeLock $activationLock
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
        -RunAsCredential $RunAsCredential `
        -PreflightOnly:$PreflightOnly
    $result | ConvertTo-Json -Depth 12
}
