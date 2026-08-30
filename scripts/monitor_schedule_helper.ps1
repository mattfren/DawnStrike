Set-StrictMode -Version Latest

function Get-DawnstrikeMonitorPayloadSha256 {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Payload,
        [Parameter(Mandatory = $true)][string]$HashProperty
    )

    $unsigned = [ordered]@{}
    if ($Payload -is [System.Collections.IDictionary]) {
        foreach ($key in $Payload.Keys) {
            if ([string]$key -ne $HashProperty) {
                $unsigned[[string]$key] = $Payload[$key]
            }
        }
    }
    else {
        foreach ($property in $Payload.PSObject.Properties) {
            if ($property.Name -ne $HashProperty) {
                $unsigned[$property.Name] = $property.Value
            }
        }
    }
    $json = $unsigned | ConvertTo-Json -Compress -Depth 8
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Get-DawnstrikeMonitorCycleStartUtc {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][DateTimeOffset]$NowUtc,
        [Parameter()][ValidateRange(1, 86400)][int]$IntervalSeconds = 300
    )

    $utc = $NowUtc.ToUniversalTime()
    $epoch = [DateTimeOffset]::new(1970, 1, 1, 0, 0, 0, [TimeSpan]::Zero)
    $elapsedSeconds = [math]::Floor(($utc - $epoch).TotalSeconds)
    $boundarySeconds = $elapsedSeconds - ($elapsedSeconds % $IntervalSeconds)
    return $epoch.AddSeconds($boundarySeconds)
}

function Get-DawnstrikeMonitorScheduleStartUtc {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$MarketDate)

    try {
        $central = [TimeZoneInfo]::FindSystemTimeZoneById("Central Standard Time")
        $localDate = [DateTime]::ParseExact(
            $MarketDate,
            "yyyy-MM-dd",
            [Globalization.CultureInfo]::InvariantCulture
        )
        $localStart = [DateTime]::SpecifyKind(
            $localDate.AddHours(8).AddMinutes(35),
            [DateTimeKind]::Unspecified
        )
        return [DateTimeOffset]([TimeZoneInfo]::ConvertTimeToUtc($localStart, $central))
    }
    catch {
        throw "Monitor schedule start could not be derived from the 08:35 America/Chicago contract for $MarketDate."
    }
}

function Get-DawnstrikeMonitorGapPlan {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$MarketDate,
        [Parameter(Mandatory = $true)][DateTimeOffset]$CycleStartUtc,
        [Parameter()][DateTimeOffset]$PreviousWatermarkUtc,
        [Parameter()][bool]$HasPreviousWatermark = $false,
        [Parameter()][bool]$IsMarketDay = $true,
        [Parameter()][ValidateRange(1, 86400)][int]$IntervalSeconds = 300
    )

    if (-not $IsMarketDay) {
        return [pscustomobject]@{
            status = "NOT_APPLICABLE"
            reason = "non_market_day"
            schedule_start_utc = $null
            gap_slots = @()
        }
    }
    try {
        $scheduleStart = Get-DawnstrikeMonitorScheduleStartUtc -MarketDate $MarketDate
    }
    catch {
        return [pscustomobject]@{
            status = "UNKNOWN_INITIAL_COVERAGE"
            reason = "schedule_derivation_failed"
            schedule_start_utc = $null
            gap_slots = @()
        }
    }
    $cycleStart = Get-DawnstrikeMonitorCycleStartUtc `
        -NowUtc $CycleStartUtc `
        -IntervalSeconds $IntervalSeconds
    if ($cycleStart -lt $scheduleStart) {
        return [pscustomobject]@{
            status = "BEFORE_SCHEDULE_START"
            reason = "before_08_35_central"
            schedule_start_utc = $scheduleStart
            gap_slots = @()
        }
    }
    if ($HasPreviousWatermark) {
        $previous = $PreviousWatermarkUtc.ToUniversalTime()
        $canonicalPrevious = Get-DawnstrikeMonitorCycleStartUtc `
            -NowUtc $previous `
            -IntervalSeconds $IntervalSeconds
        if ($previous -ne $canonicalPrevious) {
            return [pscustomobject]@{
                status = "INVALID_WATERMARK"
                reason = "watermark_not_on_interval_boundary"
                schedule_start_utc = $scheduleStart
                gap_slots = @()
            }
        }
        if ($previous -lt $scheduleStart) {
            return [pscustomobject]@{
                status = "INVALID_WATERMARK"
                reason = "watermark_before_schedule_start"
                schedule_start_utc = $scheduleStart
                gap_slots = @()
            }
        }
        if ($previous -gt $cycleStart) {
            return [pscustomobject]@{
                status = "INVALID_WATERMARK"
                reason = "watermark_after_cycle_start"
                schedule_start_utc = $scheduleStart
                gap_slots = @()
            }
        }
    }
    $expected = if ($HasPreviousWatermark) {
        $PreviousWatermarkUtc.ToUniversalTime().AddSeconds($IntervalSeconds)
    }
    else {
        $scheduleStart
    }
    if ($expected -lt $scheduleStart) { $expected = $scheduleStart }
    $slots = [System.Collections.Generic.List[DateTimeOffset]]::new()
    while ($expected -lt $cycleStart) {
        $slots.Add($expected)
        $expected = $expected.AddSeconds($IntervalSeconds)
    }
    return [pscustomobject]@{
        status = if ($slots.Count -gt 0) { "GAPS_FOUND" } else { "ON_TIME" }
        reason = if ($slots.Count -gt 0) { "monitor_interval_not_observed" } else { "none" }
        schedule_start_utc = $scheduleStart
        gap_slots = @($slots)
    }
}
