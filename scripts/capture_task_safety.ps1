function Get-DawnstrikeCapturePrincipalSid {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) { throw "Capture task principal is blank." }
    try {
        if ($Value -match '^S-\d-\d+(?:-\d+)+$') {
            return ([System.Security.Principal.SecurityIdentifier]::new($Value)).Value.ToUpperInvariant()
        }
        return ([System.Security.Principal.NTAccount]::new($Value)).Translate(
            [System.Security.Principal.SecurityIdentifier]
        ).Value.ToUpperInvariant()
    }
    catch { throw "Capture task principal cannot be resolved to one canonical SID." }
}

function Get-DawnstrikeCanonicalOrigin {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Origin)

    if ([string]::IsNullOrWhiteSpace($Origin) -or $Origin -match '[\x00-\x1f\x7f]') {
        throw "Dawnstrike origin is blank or contains control characters."
    }
    $trimmed = $Origin.Trim()
    if ($trimmed -ne $Origin -or $trimmed.Contains('?') -or $trimmed.Contains('#')) {
        throw "Dawnstrike origin contains ambiguous query, fragment, or whitespace."
    }
    if ($trimmed -ieq 'https://github.com/mattfren/DawnStrike.git') {
        return 'https://github.com/mattfren/DawnStrike.git'
    }
    if ($trimmed -ieq 'git@github.com:mattfren/DawnStrike.git') {
        return 'git@github.com:mattfren/DawnStrike.git'
    }
    if ($trimmed -match '^[A-Za-z][A-Za-z0-9+.-]*://' -or $trimmed -match '@' -or $trimmed -match ':') {
        throw "Dawnstrike origin scheme, user information, or repository identity is not approved."
    }
    throw "Dawnstrike origin is not an approved canonical repository."
}

function Assert-DawnstrikeCaptureRegularPath {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Label)
    $full = [System.IO.Path]::GetFullPath($Path)
    $item = Get-Item -LiteralPath $full -Force -ErrorAction Stop
    if ($item.PSIsContainer -or ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label must be a regular non-reparse file."
    }
    $cursor = $item.Directory
    while ($null -ne $cursor) {
        if (($cursor.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label contains a reparse-point path component."
        }
        if ([string]::Equals($cursor.FullName.TrimEnd('\'), $cursor.Root.FullName.TrimEnd('\'), [System.StringComparison]::OrdinalIgnoreCase)) { break }
        $cursor = $cursor.Parent
    }
    return $full
}

function Get-DawnstrikeCaptureQuotedTokens {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Arguments)

    $matches = @([regex]::Matches($Arguments, '"(?<value>[^"\r\n]*)"'))
    if ($matches.Count -eq 0) { throw "Capture action arguments are not canonically quoted." }
    $canonical = ($matches | ForEach-Object { '"' + [string]$_.Groups['value'].Value + '"' }) -join ' '
    if ($canonical -ne $Arguments) { throw "Capture action arguments are not in the exact canonical token form." }
    return @($matches | ForEach-Object { [string]$_.Groups['value'].Value })
}

function Get-DawnstrikeCaptureFileSha256 {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = $null
    try {
        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
        return ([System.BitConverter]::ToString($sha.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
        $sha.Dispose()
    }
}

function Assert-DawnstrikeCaptureCanonicalXml {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][System.Xml.XmlDocument]$Document,
        [switch]$AllowLegacySettings
    )

    # Task Scheduler XML is a small, closed vocabulary for this governed task.
    # Keep the map explicit: accepting an unknown element/attribute here would
    # allow a migration input to smuggle in an alternate execution surface.
    $namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    $allowed = @{
        Task = @("RegistrationInfo", "Principals", "Settings", "Triggers", "Actions")
        RegistrationInfo = @("Description", "URI")
        Description = @()
        URI = @()
        Principals = @("Principal")
        Principal = @("UserId", "LogonType", "RunLevel")
        UserId = @()
        LogonType = @()
        RunLevel = @()
        Settings = @("Enabled", "StartWhenAvailable", "WakeToRun", "DisallowStartIfOnBatteries", "StopIfGoingOnBatteries", "ExecutionTimeLimit", "MultipleInstancesPolicy", "RestartOnFailure", "UseUnifiedSchedulingEngine", "IdleSettings")
        Enabled = @()
        StartWhenAvailable = @()
        WakeToRun = @()
        DisallowStartIfOnBatteries = @()
        StopIfGoingOnBatteries = @()
        ExecutionTimeLimit = @()
        MultipleInstancesPolicy = @()
        RestartOnFailure = @("Interval", "Count")
        UseUnifiedSchedulingEngine = @()
        IdleSettings = @("Duration", "WaitTimeout", "StopOnIdleEnd", "RestartOnIdle")
        Duration = @()
        WaitTimeout = @()
        StopOnIdleEnd = @()
        RestartOnIdle = @()
        Interval = @()
        Count = @()
        Triggers = @("CalendarTrigger")
        CalendarTrigger = @("StartBoundary", "Enabled", "ScheduleByWeek")
        StartBoundary = @()
        ScheduleByWeek = @("WeeksInterval", "DaysOfWeek")
        WeeksInterval = @()
        DaysOfWeek = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
        Monday = @()
        Tuesday = @()
        Wednesday = @()
        Thursday = @()
        Friday = @()
        Actions = @("Exec")
        Exec = @("Command", "Arguments", "WorkingDirectory")
        Command = @()
        Arguments = @()
        WorkingDirectory = @()
    }
    if ($null -eq $Document.DocumentElement -or $Document.DocumentElement.LocalName -ne "Task" -or $Document.DocumentElement.NamespaceURI -ne $namespace) {
        throw "Capture task root or namespace is invalid."
    }
    foreach ($node in @($Document.SelectNodes("//*"))) {
        if ($node.NamespaceURI -ne $namespace -or -not $allowed.ContainsKey([string]$node.LocalName)) {
            throw "Capture task contains an unknown element or namespace: $($node.LocalName)."
        }
        $expectedChildren = @($allowed[[string]$node.LocalName])
        if ($node.LocalName -eq "Settings" -and $AllowLegacySettings) {
            $expectedChildren = @("DisallowStartIfOnBatteries", "StopIfGoingOnBatteries", "ExecutionTimeLimit", "MultipleInstancesPolicy", "StartWhenAvailable", "IdleSettings", "UseUnifiedSchedulingEngine")
        }
        $actualChildren = @($node.ChildNodes | Where-Object { $_.NodeType -eq [System.Xml.XmlNodeType]::Element } | ForEach-Object { [string]$_.LocalName })
        foreach ($child in $actualChildren) {
            if ($child -notin $expectedChildren) { throw "Capture task contains an unknown child element: $child." }
        }
        # Optional children remain optional, but their relative order is
        # canonical.  This prevents an attacker from hiding a second action,
        # trigger, or setting behind an otherwise valid element sequence.
        $lastIndex = -1
        foreach ($child in $actualChildren) {
            $childIndex = [array]::IndexOf($expectedChildren, $child)
            if ($childIndex -le $lastIndex) { throw "Capture task child order or cardinality is invalid under $($node.LocalName)." }
            $lastIndex = $childIndex
        }
        foreach ($attribute in @($node.Attributes)) {
            if ($attribute.Name -eq "xmlns") {
                if ($node -ne $Document.DocumentElement -or [string]$attribute.Value -ne $namespace) { throw "Capture task contains an unexpected namespace declaration." }
                continue
            }
            if ($attribute.Prefix -eq "xmlns") { throw "Capture task contains an unexpected namespace declaration." }
            $valid = ($node.LocalName -eq "Task" -and $attribute.LocalName -eq "version") -or
                ($node.LocalName -eq "Principal" -and $attribute.LocalName -eq "id") -or
                ($node.LocalName -eq "Actions" -and $attribute.LocalName -eq "Context")
            if (-not $valid -or $attribute.NamespaceURI -notin @("", $namespace)) {
                throw "Capture task contains an unknown attribute: $($attribute.Name)."
            }
        }
        $requiredChildren = @{
            Task = @("RegistrationInfo", "Principals", "Settings", "Triggers", "Actions")
            RegistrationInfo = @("Description", "URI")
            Principals = @("Principal")
            Principal = @("UserId", "LogonType")
            Settings = @()
            RestartOnFailure = @("Interval", "Count")
            Triggers = @("CalendarTrigger")
            CalendarTrigger = @("StartBoundary", "ScheduleByWeek")
            ScheduleByWeek = @("WeeksInterval", "DaysOfWeek")
            DaysOfWeek = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
            Actions = @("Exec")
            Exec = @("Command", "Arguments", "WorkingDirectory")
        }
        if ($requiredChildren.ContainsKey([string]$node.LocalName)) {
            foreach ($requiredChild in @($requiredChildren[[string]$node.LocalName])) {
                if (@($node.ChildNodes | Where-Object { $_.NodeType -eq [System.Xml.XmlNodeType]::Element -and $_.LocalName -eq $requiredChild }).Count -ne 1) {
                    throw "Capture task must contain exactly one $requiredChild under $($node.LocalName)."
                }
            }
        }
        $exactAttributes = @{
            Task = @("version")
            Principal = @("id")
            Actions = @("Context")
        }
        if ($exactAttributes.ContainsKey([string]$node.LocalName)) {
            $actualAttributes = @($node.Attributes | Where-Object { $_.Name -ne "xmlns" } | ForEach-Object { [string]$_.LocalName })
            $expectedAttributes = @($exactAttributes[[string]$node.LocalName])
            if (($actualAttributes -join ',') -ne ($expectedAttributes -join ',')) {
                throw "Capture task attributes are not exact under $($node.LocalName)."
            }
        }
        foreach ($child in @($node.ChildNodes)) {
            if ($child.NodeType -in @([System.Xml.XmlNodeType]::Comment, [System.Xml.XmlNodeType]::ProcessingInstruction, [System.Xml.XmlNodeType]::CDATA)) {
                throw "Capture task cannot contain comments, processing instructions, or CDATA."
            }
            if ($child.NodeType -eq [System.Xml.XmlNodeType]::Text -and -not [string]::IsNullOrWhiteSpace([string]$child.Value)) {
                if ($node.LocalName -notin @("Description", "URI", "UserId", "LogonType", "RunLevel", "Enabled", "StartWhenAvailable", "WakeToRun", "DisallowStartIfOnBatteries", "StopIfGoingOnBatteries", "ExecutionTimeLimit", "MultipleInstancesPolicy", "Interval", "Count", "StartBoundary", "WeeksInterval", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Command", "Arguments", "WorkingDirectory", "UseUnifiedSchedulingEngine", "Duration", "WaitTimeout", "StopOnIdleEnd", "RestartOnIdle")) {
                    throw "Capture task contains non-whitespace text outside a governed value."
                }
            }
        }
    }
    $top = @($Document.DocumentElement.ChildNodes | Where-Object { $_.NodeType -eq 'Element' } | ForEach-Object { $_.LocalName })
    if (($top -join ',') -ne 'RegistrationInfo,Principals,Settings,Triggers,Actions') { throw "Capture task top-level contract is invalid." }
}

function Assert-DawnstrikeCaptureTaskSafety {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Xml,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [string]$ExpectedPrincipal = "",
        [string]$ExpectedCandidateSha = "",
        [string]$ExpectedSymbolsManifest = "",
        [string]$ExpectedSymbolsManifestSha256 = "",
        [string]$ExpectedEntitlementReceipt = "",
        [string]$ExpectedEntitlementReceiptSha256 = "",
        [string]$ExpectedSourceConfig = "",
        [string]$ExpectedSourceConfigSha256 = "",
        [string]$ExpectedDbPath = "C:\r\dawnstrike-forward-db\staging.sqlite",
        [string]$ExpectedEvidenceRoot = "C:\r\dawnstrike-forward-evidence",
        [string]$ExpectedRunRoot = "C:\r\dawnstrike-forward-runs",
        [string]$ExpectedOutputRoot = "C:\r\dawnstrike-forward-output",
        [string]$ExpectedSessionRoot = "C:\r\dawnstrike-forward-sessions",
        [string]$ExpectedConfigRoot = "C:\r\dawnstrike-capture-config-20260830",
        [string]$ExpectedInterpreterPath = "",
        [string]$ExpectedInterpreterSha256 = "",
        [string]$ExpectedInterpreterSignerThumbprint = "9BA3C2E210C7E8296C5056515BFC0B0BBA78AC48",
        [ValidateSet("", "true", "false")][string]$ExpectedEnabled = "",
        [switch]$AllowLegacySettings,
        [switch]$AllowLegacyLauncher,
        [switch]$RequirePasswordPrincipal,
        [switch]$RequireRunner
    )

    if ($Xml.Length -gt 1048576) { throw "Capture task XML exceeds the bounded safety limit." }
    try {
        $document = [System.Xml.XmlDocument]::new()
        $document.PreserveWhitespace = $true
        $settings = [System.Xml.XmlReaderSettings]::new()
        $settings.DtdProcessing = [System.Xml.DtdProcessing]::Prohibit
        $settings.XmlResolver = $null
        $settings.MaxCharactersInDocument = 1048576
        $reader = [System.Xml.XmlReader]::Create([System.IO.StringReader]::new($Xml), $settings)
        try { $document.Load($reader) } finally { $reader.Dispose() }
    }
    catch { throw "Capture task safety validation requires valid XML." }
    Assert-DawnstrikeCaptureCanonicalXml -Document $document -AllowLegacySettings:$AllowLegacySettings
    $settingsForStage = @($document.SelectNodes("//*[local-name()='Settings']"))
    $idleSettings = if ($settingsForStage.Count -eq 1) { @($settingsForStage[0].ChildNodes | Where-Object { $_.LocalName -eq "IdleSettings" }) } else { @() }
    if ($idleSettings.Count -gt 0 -and -not $AllowLegacySettings) {
        throw "IdleSettings are permitted only on a validated migration input."
    }
    foreach ($idle in $idleSettings) {
        if ($idle.Attributes.Count -ne 0) { throw "Legacy IdleSettings cannot carry attributes." }
        $idleChildren = @($idle.ChildNodes | Where-Object { $_.NodeType -eq 'Element' } | ForEach-Object { $_.LocalName })
        if (($idleChildren -join ',') -ne 'Duration,WaitTimeout,StopOnIdleEnd,RestartOnIdle') {
            throw "Legacy IdleSettings cardinality or order is not the exact migration contract."
        }
        $idleValues = @($idle.ChildNodes | Where-Object { $_.NodeType -eq 'Element' } | ForEach-Object { [string]$_.InnerText })
        if (($idleValues -join ',') -ne 'PT10M,PT1H,true,false') {
            throw "Legacy IdleSettings values are not the exact migration contract."
        }
    }
    if ($AllowLegacySettings) {
        if ($settingsForStage.Count -ne 1) { throw "Legacy migration input Settings section is missing." }
        $legacyChildren = @($settingsForStage[0].ChildNodes | Where-Object { $_.NodeType -eq 'Element' } | ForEach-Object { $_.LocalName })
        if (($legacyChildren -join ',') -ne 'DisallowStartIfOnBatteries,StopIfGoingOnBatteries,ExecutionTimeLimit,MultipleInstancesPolicy,StartWhenAvailable,IdleSettings,UseUnifiedSchedulingEngine') {
            throw "Legacy migration input Settings are not the exact live contract."
        }
        $legacyValues = @($settingsForStage[0].ChildNodes | Where-Object { $_.NodeType -eq 'Element' } | ForEach-Object { [string]$_.InnerText })
        if (($legacyValues[0..4] -join ',') -ne 'true,true,PT3H,IgnoreNew,true' -or [string]$legacyValues[6] -ne 'true') {
            throw "Legacy migration input Settings values are not the exact live contract."
        }
    }
    elseif (-not $AllowLegacyLauncher) {
        if ($settingsForStage.Count -ne 1) { throw "Canonical Settings section is missing." }
        $canonicalChildren = @($settingsForStage[0].ChildNodes | Where-Object { $_.NodeType -eq 'Element' } | ForEach-Object { $_.LocalName })
        if (($canonicalChildren -join ',') -ne 'Enabled,StartWhenAvailable,WakeToRun,DisallowStartIfOnBatteries,StopIfGoingOnBatteries,ExecutionTimeLimit,MultipleInstancesPolicy,RestartOnFailure,UseUnifiedSchedulingEngine') {
            throw "Canonical Settings cardinality or order is invalid."
        }
        $canonicalValues = @($settingsForStage[0].ChildNodes | Where-Object { $_.NodeType -eq 'Element' } | ForEach-Object { [string]$_.InnerText })
        if ($canonicalValues[0] -notin @('true', 'false') -or $canonicalValues[1] -ne 'true' -or $canonicalValues[2] -ne 'true' -or $canonicalValues[3] -ne 'false' -or $canonicalValues[4] -ne 'false' -or $canonicalValues[5] -ne 'PT3H' -or $canonicalValues[6] -ne 'IgnoreNew' -or $canonicalValues[8] -ne 'true') {
            throw "Canonical Settings values are invalid."
        }
        $restartSettings = @($settingsForStage[0].ChildNodes | Where-Object { $_.LocalName -eq 'RestartOnFailure' })
        if ($restartSettings.Count -ne 1 -or @($restartSettings[0].ChildNodes | Where-Object { $_.LocalName -eq 'Interval' }).Count -ne 1 -or [string](@($restartSettings[0].ChildNodes | Where-Object { $_.LocalName -eq 'Interval' })[0].InnerText) -ne 'PT15M' -or @($restartSettings[0].ChildNodes | Where-Object { $_.LocalName -eq 'Count' }).Count -ne 1 -or [string](@($restartSettings[0].ChildNodes | Where-Object { $_.LocalName -eq 'Count' })[0].InnerText) -ne '3') {
            throw "Canonical RestartOnFailure values are invalid."
        }
        if ($ExpectedEnabled -and $canonicalValues[0] -ne $ExpectedEnabled) { throw "Canonical task enablement is not the expected stage."
        }
    }
    if ($document.DocumentElement.LocalName -ne "Task" -or $document.DocumentElement.NamespaceURI -ne "http://schemas.microsoft.com/windows/2004/02/mit/task") {
        throw "Capture task root or namespace is invalid."
    }
    if ([string]$document.DocumentElement.GetAttribute("version") -ne "1.3") { throw "Capture task version must be 1.3." }
    if (@($document.SelectNodes("//comment()|//processing-instruction()")).Count -ne 0) { throw "Capture task cannot contain comments or processing instructions." }
    $allNodes = @($document.SelectNodes("//*") | ForEach-Object { $_.ChildNodes } | ForEach-Object { $_ })
    if (@($allNodes | Where-Object { $_.NodeType -eq [System.Xml.XmlNodeType]::CDATA }).Count -ne 0) { throw "Capture task cannot contain CDATA." }
    $top = @($document.DocumentElement.ChildNodes | Where-Object { $_.NodeType -eq 'Element' } | ForEach-Object { $_.LocalName })
    if (($top -join ',') -ne 'RegistrationInfo,Principals,Settings,Triggers,Actions') { throw "Capture task top-level contract is invalid." }
    $registration = @($document.SelectNodes("//*[local-name()='RegistrationInfo']"))
    if ($registration.Count -ne 1) { throw "Capture task RegistrationInfo is invalid." }
    $registrationChildren = @($registration[0].ChildNodes | Where-Object { $_.NodeType -eq 'Element' })
    if (($registrationChildren.LocalName -join ',') -ne 'Description,URI' -or [string]$registrationChildren[0].InnerText -ne 'Dawnstrike delayed SIP research capture; no broker execution.' -or [string]$registrationChildren[1].InnerText -ne '\Dawnstrike Delayed SIP Capture') {
        throw "Capture task registration identity is invalid."
    }
    $principals = @($document.SelectNodes("//*[local-name()='Principals']"))
    if ($principals.Count -ne 1) { throw "Capture task must contain exactly one Principals section." }
    $principalNodes = @($principals[0].ChildNodes | Where-Object { $_.LocalName -eq "Principal" })
    if ($principalNodes.Count -ne 1) { throw "Capture task must contain exactly one Principal." }
    $principal = $principalNodes[0]
    if ([string]$principal.GetAttribute("id") -ne "Author" -or $principal.Attributes.Count -ne 1) {
        throw "Capture task Principal must have the sole canonical Author id."
    }
    foreach ($name in @("UserId", "LogonType")) {
        $nodes = @($principal.ChildNodes | Where-Object { $_.LocalName -eq $name })
        if ($nodes.Count -ne 1) { throw "Capture task Principal must contain exactly one $name." }
        Set-Variable -Name ("principal" + $name) -Value ([string]$nodes[0].InnerText)
    }
    $runLevelNodes = @($principal.ChildNodes | Where-Object { $_.LocalName -eq "RunLevel" })
    if ($runLevelNodes.Count -gt 1 -or ($RequirePasswordPrincipal -and $runLevelNodes.Count -ne 1)) {
        throw "Capture task Principal RunLevel cardinality is invalid."
    }
    $principalRunLevel = if ($runLevelNodes.Count -eq 0) { "LeastPrivilege" } else { [string]$runLevelNodes[0].InnerText }
    $principalChildren = @($principal.ChildNodes | Where-Object { $_.NodeType -eq 'Element' } | ForEach-Object { $_.LocalName })
    if (@($principalChildren | Where-Object { $_ -notin @("UserId", "LogonType", "RunLevel") }).Count -ne 0) {
        throw "Capture task Principal contains an unapproved child element."
    }
    if ($RequirePasswordPrincipal -and [string]$principalLogonType -ne "Password") { throw "Capture task LogonType must be Password." }
    if (-not $RequirePasswordPrincipal -and [string]$principalLogonType -notin @("Password", "InteractiveToken")) {
        throw "Capture task pre-hardening LogonType is not governed."
    }
    if ([string]$principalRunLevel -ne "LeastPrivilege") { throw "Capture task RunLevel must be LeastPrivilege." }
    if (@($principal.SelectNodes(".//*[local-name()='GroupId' or local-name()='RequiredPrivileges' or local-name()='Privilege']")).Count -ne 0) {
        throw "Capture task cannot use GroupId, RequiredPrivileges, or elevated privileges."
    }
    $triggers = @($document.SelectNodes("//*[local-name()='Triggers']"))
    if ($triggers.Count -ne 1) { throw "Capture task must contain exactly one Triggers section." }
    $triggerElements = @($triggers[0].ChildNodes | Where-Object { $_.NodeType -eq 'Element' })
    if ($triggerElements.Count -ne 1 -or $triggerElements[0].LocalName -ne "CalendarTrigger" -or $triggerElements[0].NamespaceURI -ne $document.DocumentElement.NamespaceURI) {
        throw "Capture task must contain exactly one namespace-correct CalendarTrigger."
    }
    $trigger = $triggerElements[0]
    $allowedTriggerChildren = @("StartBoundary", "Enabled", "ScheduleByWeek")
    if (@($trigger.ChildNodes | Where-Object { $_.NodeType -eq 'Element' -and $_.LocalName -notin $allowedTriggerChildren }).Count -ne 0) {
        throw "Capture task CalendarTrigger contains an unapproved child element."
    }
    $triggerEnabled = @($trigger.ChildNodes | Where-Object { $_.LocalName -eq "Enabled" })
    if ($triggerEnabled.Count -gt 1 -or ($triggerEnabled.Count -eq 1 -and [string]$triggerEnabled[0].InnerText -ne "true")) {
        throw "Capture task trigger must be enabled or use the enabled-by-default schema semantics."
    }
    $start = @($trigger.ChildNodes | Where-Object { $_.LocalName -eq "StartBoundary" })
    $schedule = @($trigger.ChildNodes | Where-Object { $_.LocalName -eq "ScheduleByWeek" })
    if ($start.Count -ne 1 -or $schedule.Count -ne 1 -or [string]$start[0].InnerText -notmatch '^\d{4}-\d{2}-\d{2}T15:20:00(?:[+-]\d{2}:\d{2})?$') {
        throw "Capture task trigger must start at canonical 15:20 local time."
    }
    if (@($trigger.ChildNodes | Where-Object { $_.NodeType -eq 'Element' -and $_.LocalName -in @("EndBoundary", "RandomDelay", "Repetition", "ExecutionTimeLimit") }).Count -ne 0) {
        throw "Capture task trigger contains an unapproved timing surface."
    }
    $weeks = @($schedule[0].ChildNodes | Where-Object { $_.LocalName -eq "WeeksInterval" })
    $days = @($schedule[0].ChildNodes | Where-Object { $_.LocalName -eq "DaysOfWeek" })
    if (@($schedule[0].ChildNodes | Where-Object { $_.NodeType -eq 'Element' -and $_.LocalName -notin @("WeeksInterval", "DaysOfWeek") }).Count -ne 0) {
        throw "Capture task weekly schedule contains an unapproved child element."
    }
    if ($weeks.Count -ne 1 -or [string]$weeks[0].InnerText -ne "1" -or $days.Count -ne 1) { throw "Capture task weekly schedule is invalid." }
    $dayNames = @($days[0].ChildNodes | Where-Object { $_.NodeType -eq 'Element' } | ForEach-Object { $_.LocalName })
    if (($dayNames -join ',') -ne 'Monday,Tuesday,Wednesday,Thursday,Friday') { throw "Capture task weekdays are not canonical Mon-Fri." }
    $principalSid = Get-DawnstrikeCapturePrincipalSid ([string]$principalUserId)
    if ($ExpectedPrincipal) {
        $expectedSid = Get-DawnstrikeCapturePrincipalSid $ExpectedPrincipal
        if ($principalSid -ne $expectedSid) { throw "Capture task principal SID does not match the local credential." }
    }

    $actions = @($document.SelectNodes("//*[local-name()='Actions']"))
    if ($actions.Count -ne 1) { throw "Capture task must contain exactly one Actions section." }
    if ([string]$actions[0].GetAttribute("Context") -ne "Author" -or $actions[0].Attributes.Count -ne 1) {
        throw "Capture task Actions must reference only the canonical Author principal."
    }
    $execNodes = @($actions[0].ChildNodes | Where-Object { $_.LocalName -eq "Exec" })
    if ($execNodes.Count -ne 1 -or @($actions[0].ChildNodes | Where-Object { $_.NodeType -eq 'Element' }).Count -ne 1) {
        throw "Capture task must contain exactly one Exec action."
    }
    if ($execNodes[0].NamespaceURI -ne $actions[0].NamespaceURI) {
        throw "Capture task Exec action is outside the task namespace."
    }
    $execChildren = @($execNodes[0].ChildNodes | Where-Object { $_.NodeType -eq 'Element' })
    if (@($execChildren | Where-Object { $_.LocalName -notin @("Command", "Arguments", "WorkingDirectory") -or $_.NamespaceURI -ne $execNodes[0].NamespaceURI }).Count -ne 0) {
        throw "Capture task Exec contains an unapproved child element or namespace."
    }
    $record = @{}
    foreach ($name in @("Command", "Arguments", "WorkingDirectory")) {
        $nodes = @($execNodes[0].ChildNodes | Where-Object { $_.LocalName -eq $name })
        if ($nodes.Count -ne 1) { throw "Capture task Exec must contain exactly one $name." }
        $record[$name] = [string]$nodes[0].InnerText
    }
    $legacyLauncher = [string]::Equals($record.Command, "py.exe", [System.StringComparison]::OrdinalIgnoreCase)
    if ($legacyLauncher -and -not $AllowLegacyLauncher) { throw "Legacy py.exe is forbidden after hardening." }
    if (-not $legacyLauncher) {
        if ([string]::IsNullOrWhiteSpace($ExpectedInterpreterPath) -or [string]::IsNullOrWhiteSpace($ExpectedInterpreterSha256)) {
            throw "Direct capture interpreter validation requires an exact approved path and SHA-256."
        }
        if (-not [System.IO.Path]::IsPathRooted($record.Command)) { throw "Capture interpreter path must be absolute." }
        $interpreter = Assert-DawnstrikeCaptureRegularPath $record.Command "Capture interpreter"
        if (-not [string]::Equals($interpreter, [System.IO.Path]::GetFullPath($ExpectedInterpreterPath), [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Capture interpreter path is not the exact approved executable."
        }
        $interpreterSha = Get-DawnstrikeCaptureFileSha256 $interpreter
        if ($interpreterSha -ne $ExpectedInterpreterSha256) { throw "Capture interpreter hash changed." }
        $signature = Get-AuthenticodeSignature -LiteralPath $interpreter -ErrorAction Stop
        if (
            [string]$signature.Status -ne "Valid" -or $null -eq $signature.SignerCertificate -or
            [string]$signature.SignerCertificate.Subject -ne "CN=Python Software Foundation, O=Python Software Foundation, L=Beaverton, S=Oregon, C=US" -or
            [string]$signature.SignerCertificate.Thumbprint -ne $ExpectedInterpreterSignerThumbprint
        ) { throw "Capture interpreter Authenticode identity is not approved." }
        $versionOutput = @(& $interpreter -I -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null)
        if ($LASTEXITCODE -ne 0 -or $versionOutput.Count -ne 1 -or [string]$versionOutput[0] -notmatch '^3\.13\.') { throw "Capture interpreter is not Python 3.13." }
    }
    $runtime = [System.IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\')
    $state = [System.IO.Path]::GetFullPath($StateRoot).TrimEnd('\')
    if (-not [string]::Equals([System.IO.Path]::GetFullPath($record.WorkingDirectory).TrimEnd('\'), $runtime, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Capture task WorkingDirectory must equal RuntimeRoot."
    }
    $tokens = @(Get-DawnstrikeCaptureQuotedTokens $record.Arguments)
    $optionOrder = @(
        "--candidate-sha", "--repo-root", "--db-path", "--evidence-root", "--run-root",
        "--output-root", "--session-root", "--symbols-manifest", "--symbols-manifest-sha256",
        "--entitlement-receipt", "--entitlement-receipt-sha256", "--source-config",
        "--source-config-sha256", "--env-file", "--max-pages", "--retries"
    )
    if ($tokens.Count -ne (3 + ($optionOrder.Count * 2) + 1)) { throw "Capture action token count is not exact." }
    if ($legacyLauncher) {
        if ($tokens[0] -ne "-3.13" -or $tokens[1] -ne "-u") { throw "Legacy capture launcher prefix is invalid." }
    }
    elseif ($tokens[0] -ne "-I" -or $tokens[1] -ne "-u") { throw "Capture action Python isolation prefix is invalid." }
    $runner = [System.IO.Path]::GetFullPath($tokens[2])
    $expectedRunner = [System.IO.Path]::GetFullPath((Join-Path $runtime "scripts\run_daily_intraday_capture.py"))
    if (-not [string]::Equals($runner, $expectedRunner, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Capture action runner is outside the exact RuntimeRoot contract."
    }
    if ($RequireRunner) {
        $null = Assert-DawnstrikeCaptureRegularPath $runner "Capture action runner"
    }
    $values = @{}
    $cursor = 3
    foreach ($option in $optionOrder) {
        if ($tokens[$cursor] -ne $option) { throw "Capture action option order or name is invalid at $option." }
        $values[$option] = [string]$tokens[$cursor + 1]
        if ([string]::IsNullOrWhiteSpace($values[$option])) { throw "Capture action $option is blank." }
        $cursor += 2
    }
    if ($tokens[$cursor] -ne "--execute") { throw "Capture action must end with the sole --execute flag." }
    if ($values["--candidate-sha"] -notmatch '^[0-9a-f]{40}$') { throw "Capture action candidate SHA is invalid." }
    if ($ExpectedCandidateSha -and $values["--candidate-sha"] -ne $ExpectedCandidateSha) { throw "Capture action candidate SHA is not the exact activated candidate." }
    if ($values["--max-pages"] -ne "100" -or $values["--retries"] -ne "3") { throw "Capture action retry/page policy is invalid." }
    if (-not [string]::Equals([System.IO.Path]::GetFullPath($values["--repo-root"]).TrimEnd('\'), $runtime, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Capture action repo root does not equal RuntimeRoot."
    }
    $expectedEnv = [System.IO.Path]::GetFullPath((Join-Path $state "secrets\runtime.env"))
    if (-not [string]::Equals([System.IO.Path]::GetFullPath($values["--env-file"]), $expectedEnv, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Capture action env file is outside the governed StateRoot location."
    }
    if ($RequireRunner) { $null = Assert-DawnstrikeCaptureRegularPath $expectedEnv "Capture action env file" }
    foreach ($expected in @(
        @("--db-path", $ExpectedDbPath), @("--evidence-root", $ExpectedEvidenceRoot),
        @("--run-root", $ExpectedRunRoot), @("--output-root", $ExpectedOutputRoot),
        @("--session-root", $ExpectedSessionRoot)
    )) {
        if (-not [string]::Equals([System.IO.Path]::GetFullPath([string]$values[$expected[0]]), [System.IO.Path]::GetFullPath([string]$expected[1]), [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Capture action $($expected[0]) is outside the exact production policy."
        }
    }
    $configRoot = [System.IO.Path]::GetFullPath($ExpectedConfigRoot).TrimEnd('\') + '\'
    foreach ($option in @("--symbols-manifest", "--entitlement-receipt", "--source-config")) {
        if (-not [System.IO.Path]::GetFullPath([string]$values[$option]).StartsWith($configRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Capture action $option is outside the governed capture config root."
        }
    }
    $externalOptions = @("--db-path", "--evidence-root", "--run-root", "--output-root", "--session-root", "--symbols-manifest", "--entitlement-receipt", "--source-config")
    $external = @()
    foreach ($option in $externalOptions) {
        if (-not [System.IO.Path]::IsPathRooted($values[$option])) { throw "Capture action $option must be absolute." }
        $full = [System.IO.Path]::GetFullPath($values[$option])
        foreach ($forbidden in @($runtime, $state)) {
            $prefix = $forbidden.TrimEnd('\') + '\'
            if ([string]::Equals($full.TrimEnd('\'), $forbidden, [System.StringComparison]::OrdinalIgnoreCase) -or $full.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Capture action $option is inside a forbidden governed root."
            }
        }
        $external += $full.TrimEnd('\')
    }
    for ($left = 0; $left -lt $external.Count; $left++) {
        for ($right = $left + 1; $right -lt $external.Count; $right++) {
            $leftPrefix = $external[$left] + '\'; $rightPrefix = $external[$right] + '\'
            if ([string]::Equals($external[$left], $external[$right], [System.StringComparison]::OrdinalIgnoreCase) -or $external[$left].StartsWith($rightPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or $external[$right].StartsWith($leftPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Capture action external roots must be distinct and non-nested."
            }
        }
    }
    foreach ($hashOption in @("--symbols-manifest-sha256", "--entitlement-receipt-sha256", "--source-config-sha256")) {
        if ($values[$hashOption] -notmatch '^[0-9a-f]{64}$') { throw "Capture action $hashOption is invalid." }
    }
    foreach ($binding in @(
        @("--symbols-manifest", $ExpectedSymbolsManifest), @("--symbols-manifest-sha256", $ExpectedSymbolsManifestSha256),
        @("--entitlement-receipt", $ExpectedEntitlementReceipt), @("--entitlement-receipt-sha256", $ExpectedEntitlementReceiptSha256),
        @("--source-config", $ExpectedSourceConfig), @("--source-config-sha256", $ExpectedSourceConfigSha256)
    )) {
        if ([string]::IsNullOrWhiteSpace([string]$binding[1])) { continue }
        $actual = [string]$values[$binding[0]]
        if ($binding[0].EndsWith("sha256")) {
            if ($actual -ne [string]$binding[1]) { throw "Capture action $($binding[0]) hash is not exact." }
        }
        elseif (-not [string]::Equals([System.IO.Path]::GetFullPath($actual), [System.IO.Path]::GetFullPath([string]$binding[1]), [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Capture action $($binding[0]) path is not exact."
        }
    }
    foreach ($inputBinding in @(
        @("--symbols-manifest", "--symbols-manifest-sha256", $ExpectedSymbolsManifestSha256),
        @("--entitlement-receipt", "--entitlement-receipt-sha256", $ExpectedEntitlementReceiptSha256),
        @("--source-config", "--source-config-sha256", $ExpectedSourceConfigSha256)
    )) {
        $inputPath = [System.IO.Path]::GetFullPath([string]$values[$inputBinding[0]])
        $null = Assert-DawnstrikeCaptureRegularPath $inputPath "Capture action input file"
        $actualHash = Get-DawnstrikeCaptureFileSha256 $inputPath
        if ($actualHash -ne [string]$values[$inputBinding[1]] -or (-not [string]::IsNullOrWhiteSpace([string]$inputBinding[2]) -and $actualHash -ne [string]$inputBinding[2])) {
            throw "Capture action input file hash does not match its exact binding."
        }
    }
    return [pscustomobject]@{
        principal_user_id = [string]$principalUserId
        principal_sid = $principalSid
        principal_count = 1
        logon_type = [string]$principalLogonType
        run_level = "LeastPrivilege"
        group_id_absent = $true
        required_privileges_absent = $true
        execute = [string]$record.Command
        python_prefix = if ($legacyLauncher) { "-3.13 -u" } else { "-I -u" }
        runner_path = $runner
        working_directory = $runtime
        candidate_sha = [string]$values["--candidate-sha"]
        forward_observed = $true
        execute_enabled = $true
        option_contract = "EXACT_CANONICAL_FORWARD_CAPTURE_V1"
    }
}
