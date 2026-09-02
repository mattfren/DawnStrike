Set-StrictMode -Version Latest

function ConvertTo-DawnstrikeExactMarketDate {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Value)

    $parsed = [DateTime]::MinValue
    if (-not [DateTime]::TryParseExact(
        $Value,
        'yyyy-MM-dd',
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::None,
        [ref]$parsed
    )) {
        throw 'Market date is not a real canonical calendar date.'
    }
    if ($parsed.ToString('yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture) -cne $Value) {
        throw 'Market date is not a real canonical calendar date.'
    }
    return $parsed.Date
}

function Assert-DawnstrikeUniverseBootstrapBoundarySnapshot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RequestedMarketDate,
        [Parameter(Mandatory = $true)][DateTime]$Now,
        [Parameter(Mandatory = $true)][object[]]$CanonicalTasks,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$CaptureTasks
    )

    $parsedDate = ConvertTo-DawnstrikeExactMarketDate -Value $RequestedMarketDate
    if ($parsedDate -ne $Now.Date) {
        throw 'Core-universe bootstrap is allowed only for the host current date.'
    }

    $expectedNames = @(
        'Dawnstrike AlphaOps Morning',
        'Dawnstrike AlphaOps Monitor 5m',
        'Dawnstrike AlphaOps EOD Full Report',
        'Dawnstrike AlphaOps V6 Weekly Training',
        'Dawnstrike 10of10 Daily Finalize'
    )
    if (@($CanonicalTasks).Count -ne $expectedNames.Count) {
        throw 'Core-universe bootstrap requires exactly five canonical task snapshots.'
    }
    $byName = @{}
    foreach ($task in @($CanonicalTasks)) {
        $name = [string]$task.name
        if ($name -cnotin $expectedNames -or $byName.ContainsKey($name)) {
            throw 'Core-universe bootstrap found an unknown or duplicate canonical task.'
        }
        if ([string]$task.state -cne 'Ready') {
            throw "Core-universe bootstrap requires a unique Ready canonical task: $name"
        }
        $byName[$name] = $task
    }
    foreach ($name in $expectedNames) {
        if (-not $byName.ContainsKey($name)) {
            throw "Core-universe bootstrap is missing canonical task: $name"
        }
    }

    $captures = @($CaptureTasks)
    if ($captures.Count -gt 1) {
        throw 'Core-universe bootstrap requires the optional delayed-SIP task to be absent or quiescent.'
    }
    if ($captures.Count -eq 1 -and [string]$captures[0].state -cnotin @('Ready', 'Disabled')) {
        throw 'Core-universe bootstrap requires the optional delayed-SIP task to be absent or quiescent.'
    }

    $morning = $byName['Dawnstrike AlphaOps Morning']
    $eod = $byName['Dawnstrike AlphaOps EOD Full Report']
    $finalizer = $byName['Dawnstrike 10of10 Daily Finalize']
    $morningNext = [DateTime]$morning.next_run_time
    $morningLast = [DateTime]$morning.last_run_time
    $eodLast = [DateTime]$eod.last_run_time
    $finalizerLast = [DateTime]$finalizer.last_run_time
    $beforeMorning = (
        $morningNext.Date -eq $Now.Date -and
        $Now -lt $morningNext -and
        $morningLast.Date -lt $Now.Date
    )
    $afterFinalizer = (
        $eodLast.Date -eq $Now.Date -and
        $finalizerLast.Date -eq $Now.Date -and
        $eodLast -le $Now -and
        $eodLast -le $finalizerLast -and
        $finalizerLast -le $Now
    )
    if (-not ($beforeMorning -or $afterFinalizer)) {
        throw 'Core-universe bootstrap requires the pre-Morning or post-finalizer quiescent window.'
    }
    return $true
}

function Assert-DawnstrikeExactProductionAliases {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object[]]$Actual,
        [Parameter(Mandatory = $true)][string[]]$Expected
    )

    $actualJson = ConvertTo-Json @($Actual | ForEach-Object { [string]$_ }) -Compress
    $expectedJson = ConvertTo-Json @($Expected) -Compress
    if ($actualJson -cne $expectedJson) {
        throw 'Vercel recovery result aliases do not match the governed production aliases.'
    }
}

function Assert-DawnstrikeVercelRecoveryResult {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Result,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
        [Parameter(Mandatory = $true)][string]$ExpectedMarketDate,
        [Parameter(Mandatory = $true)][string]$ExpectedProjectId,
        [Parameter(Mandatory = $true)][string]$ExpectedProjectName,
        [Parameter(Mandatory = $true)][string]$ExpectedProviderScope,
        [Parameter(Mandatory = $true)][string[]]$ExpectedAliases
    )

    $null = ConvertTo-DawnstrikeExactMarketDate -Value $ExpectedMarketDate
    if ($Result.research_only -ne $true -or $Result.broker_execution_enabled -ne $false) {
        throw 'Vercel publication recovery result crossed the research-only boundary.'
    }
    if (
        [string]$Result.project_id -cne $ExpectedProjectId -or
        [string]$Result.provider_scope -cne $ExpectedProviderScope
    ) {
        throw 'Vercel publication recovery result target identity is invalid.'
    }
    Assert-DawnstrikeExactProductionAliases `
        -Actual @($Result.production_aliases) -Expected $ExpectedAliases

    $schema = [string]$Result.schema_version
    $status = [string]$Result.status
    if ($schema -ceq 'dawnstrike.vercel_publication_recovery.v1') {
        if ([string]$Result.project_name -cne $ExpectedProjectName) {
            throw 'Vercel publication recovery result project name is invalid.'
        }
        $null = ConvertTo-DawnstrikeExactMarketDate -Value ([string]$Result.market_date)
        if (
            $status -notin @(
                'NO_NONTERMINAL_CURRENT_OPERATION',
                'ARCHIVED_COMPENSATED',
                'COMPENSATED'
            ) -or
            [string]$Result.market_date -cne $ExpectedMarketDate
        ) {
            throw 'Vercel publication recovery result identity is invalid.'
        }
        if ($status -in @('ARCHIVED_COMPENSATED', 'COMPENSATED') -and
            [string]$Result.archived_journal_sha256 -cnotmatch '^[0-9a-f]{64}$') {
            throw 'Vercel publication recovery archive identity is invalid.'
        }
    }
    elseif ($schema -ceq 'dawnstrike.daily_deployment.v1') {
        $null = ConvertTo-DawnstrikeExactMarketDate -Value ([string]$Result.market_date)
        $null = ConvertTo-DawnstrikeExactMarketDate -Value ([string]$Result.expected_market_date)
        if (
            $status -cne 'PRODUCTION_VERIFIED' -or
            [string]$Result.source_sha -cne $ExpectedSha -or
            [string]$Result.market_date -cne $ExpectedMarketDate -or
            [string]$Result.expected_market_date -cne $ExpectedMarketDate -or
            $Result.promoted -ne $true -or
            [int]$Result.readiness_http_status -ne 200
        ) {
            throw 'Vercel completed publication recovery result identity is invalid.'
        }
    }
    else {
        throw 'Vercel publication recovery returned an unsupported schema.'
    }
    return $true
}
