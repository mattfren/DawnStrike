# Shared, strict activation-lock implementation. Callers set operation and
# immutable source identity before acquisition; credentials are never accepted.
$script:DawnstrikeApprovedPythonPath='C:\Users\MattFields\AppData\Local\Programs\Python\Python313\python.exe'
$script:DawnstrikeApprovedPythonSha256='ef8f51028ac5329641985112f8efb1c2d4c47c86b8011ddf7e6fae21e2b4e5a1'
$script:DawnstrikeApprovedPythonSubject='CN=Python Software Foundation, O=Python Software Foundation, L=Beaverton, S=Oregon, C=US'
$script:DawnstrikeApprovedPythonThumbprint='9BA3C2E210C7E8296C5056515BFC0B0BBA78AC48'

function Get-DawnstrikeApprovedLockInterpreter {
    Assert-DawnstrikeSharedLockNoReparse $script:DawnstrikeApprovedPythonPath 'Approved lock-contract interpreter'
    if(-not (Test-Path -LiteralPath $script:DawnstrikeApprovedPythonPath -PathType Leaf)){throw 'Approved lock-contract interpreter is missing.'}
    if((Get-DawnstrikeRuntimeLockHash $script:DawnstrikeApprovedPythonPath)-ne $script:DawnstrikeApprovedPythonSha256){throw 'Approved lock-contract interpreter hash changed.'}
    try{$certificate=[Security.Cryptography.X509Certificates.X509Certificate2]::new([Security.Cryptography.X509Certificates.X509Certificate]::CreateFromSignedFile($script:DawnstrikeApprovedPythonPath))}
    catch{throw 'Approved lock-contract interpreter has no readable Authenticode signer.'}
    if($certificate.Subject -ne $script:DawnstrikeApprovedPythonSubject -or $certificate.Thumbprint -ne $script:DawnstrikeApprovedPythonThumbprint){throw 'Approved lock-contract interpreter signer is invalid.'}
    return [pscustomobject]@{path=$script:DawnstrikeApprovedPythonPath;sha256=$script:DawnstrikeApprovedPythonSha256}
}

function Assert-DawnstrikeSharedLockNoReparse([string]$Path,[string]$Label){
    $full=[IO.Path]::GetFullPath($Path);$cursor=[IO.FileInfo]::new($full)
    if(-not $cursor.Exists){$cursor=[IO.DirectoryInfo]::new((Split-Path $full -Parent))}
    while($null-ne $cursor){
        if($cursor.Exists-and($cursor.Attributes-band [IO.FileAttributes]::ReparsePoint)){throw "$Label contains a reparse point."}
        $cursor=if($cursor-is [IO.FileInfo]){$cursor.Directory}else{$cursor.Parent}
    }
}
function Get-DawnstrikeSharedLockSha256Text([string]$Text) {
    $sha=[Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text)))).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Get-DawnstrikeRuntimeLockHash([string]$Path) {
    $sha=[Security.Cryptography.SHA256]::Create();$stream=$null
    try{$stream=[IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read);return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-','').ToLowerInvariant()}
    finally{if($null-ne $stream){$stream.Dispose()};$sha.Dispose()}
}

function Enter-DawnstrikeRuntimeLockMutex {
    $created = $false
    $mutex = [Threading.Mutex]::new($false, "Global\DawnstrikeRuntimeActivationLockV2", [ref]$created)
    try { $owned=$mutex.WaitOne([TimeSpan]::FromSeconds(30)) }
    catch [Threading.AbandonedMutexException] { $mutex.Dispose(); throw "Runtime activation lock mutex was abandoned; recovery evidence is required." }
    if (-not $owned) { $mutex.Dispose(); throw "Runtime activation lock mutex timed out." }
    return $mutex
}

function Exit-DawnstrikeRuntimeLockMutex([Threading.Mutex]$Mutex) {
    try { $Mutex.ReleaseMutex() } finally { $Mutex.Dispose() }
}

function Assert-DawnstrikeRuntimeLockStateRoot([string]$StateRoot) {
    if (-not [IO.Path]::IsPathRooted($StateRoot)) { throw "StateRoot must be drive-qualified." }
    $full = [IO.Path]::GetFullPath($StateRoot).TrimEnd('\')
    $root = [IO.Path]::GetPathRoot($full).TrimEnd('\')
    if ([string]::Equals($full, $root, [StringComparison]::OrdinalIgnoreCase)) { throw "StateRoot cannot be a drive root." }
    if (Test-Path -LiteralPath $full -PathType Leaf) { throw "StateRoot cannot be a file." }
    $cursor = [IO.DirectoryInfo]::new($full)
    while ($null -ne $cursor) {
        if ($cursor.Exists -and ($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "StateRoot contains a reparse point." }
        $cursor = $cursor.Parent
    }
    return $full
}

function Get-DawnstrikeStrictRuntimeLock([string]$Path,[string]$PythonPath,[string]$PythonSha256) {
    $contract = Join-Path $PSScriptRoot "runtime_activation_lock_contract.py"
    Assert-DawnstrikeSharedLockNoReparse $contract 'Runtime activation lock contract'
    Assert-DawnstrikeSharedLockNoReparse $Path 'Runtime activation lock'
    if (-not [IO.Path]::IsPathRooted($PythonPath) -or -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw "Approved lock-contract interpreter is invalid." }
    $item=Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if(($item.Attributes-band [IO.FileAttributes]::ReparsePoint)-or $item.Length -gt 16384){throw 'Runtime activation lock leaf is unsafe.'}
    if($PythonPath -ne $script:DawnstrikeApprovedPythonPath -or $PythonSha256 -ne $script:DawnstrikeApprovedPythonSha256){throw 'Lock-contract interpreter is not the approved exact identity.'}
    if ((Get-DawnstrikeRuntimeLockHash $PythonPath) -ne $PythonSha256) { throw "Approved lock-contract interpreter hash changed." }
    $arguments = @('-I','-B',$contract,$Path)
    $output = & $PythonPath @arguments 2>$null
    if ($LASTEXITCODE -ne 0) { throw "Runtime activation lock is malformed or unsafe." }
    try { return ([string]($output -join "")) | ConvertFrom-Json }
    catch { throw "Runtime activation lock validator returned invalid output." }
}

function Test-DawnstrikeRuntimeLockOwnerDead([object]$Payload) {
    $process = Get-Process -Id ([int]$Payload.process_id) -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $true }
    $actual = $process.StartTime.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ")
    if ($actual -eq [string]$Payload.process_started_at_utc) { return $false }
    # A different OS process start proves this PID was reused and the recorded
    # owner is dead. Inability to read the start time raises and fails closed.
    return $true
}

function Convert-DawnstrikeCanonicalOriginIdentity([string]$Origin) {
    $value=$Origin.Trim()
    if ($value -match '^(?:https://|ssh://git@)github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$' -or $value -match '^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$') {
        $identity=("github.com/{0}/{1}" -f $Matches[1].ToLowerInvariant(),$Matches[2].ToLowerInvariant())
        if($identity -ne 'github.com/mattfren/dawnstrike'){throw 'Origin is not the governed Dawnstrike repository.'}
        return $identity
    }
    throw "Origin URL cannot be reduced to an approved canonical identity."
}

function New-DawnstrikeRuntimeLockPayload([string]$Operation,[string]$CandidateSha,[string]$CandidateTree,[string]$OriginIdentity,[string]$Token) {
    $originHash = Get-DawnstrikeSharedLockSha256Text $OriginIdentity
    return [ordered]@{
        schema_version = "dawnstrike.runtime_activation_lock.v2"
        operation = $Operation
        candidate_sha = $CandidateSha
        candidate_tree = $CandidateTree
        origin_identity = $OriginIdentity
        origin_identity_sha256 = $originHash
        process_id = [int]$PID
        process_started_at_utc = (Get-Process -Id $PID).StartTime.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ")
        acquired_at_utc = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ")
        lock_token = $Token
        research_only = $true
        broker_execution_enabled = $false
    }
}

function Enter-DawnstrikeGovernedRuntimeLock {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$StateRoot,
        [ValidateSet("capture_task_hardening","capture_task_rebind","runtime_activation","runtime_rollback","recovery")][string]$Operation = "runtime_activation",
        [ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateSha = $script:DawnstrikeLockCandidateSha,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateTree = $script:DawnstrikeLockCandidateTree,
        [Parameter(Mandatory=$true)][string]$OriginIdentity,
        [Parameter(Mandatory=$true)][string]$PythonPath,
        [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{64}$')][string]$PythonSha256
    )
    if ([string]::IsNullOrWhiteSpace($CandidateSha) -or [string]::IsNullOrWhiteSpace($CandidateTree) -or [string]::IsNullOrWhiteSpace($OriginIdentity)) { throw "Runtime activation lock source identity is incomplete." }
    $state = Assert-DawnstrikeRuntimeLockStateRoot $StateRoot
    $lockRoot = Join-Path $state "locks"
    New-Item -ItemType Directory -Path $lockRoot -Force | Out-Null
    $null = Assert-DawnstrikeRuntimeLockStateRoot $state
    $lockRootItem=Get-Item -LiteralPath $lockRoot -Force
    if($lockRootItem.Attributes-band [IO.FileAttributes]::ReparsePoint){throw 'Runtime activation lock root is unsafe.'}
    $path = Join-Path $lockRoot "dawnstrike-runtime-activation.lock"
    $mutex = Enter-DawnstrikeRuntimeLockMutex
    try {
        $daily=@(Get-ChildItem -LiteralPath $lockRoot -Filter 'dawnstrike-daily-*.lock' -File -Force -ErrorAction SilentlyContinue)
        if($daily.Count){throw "A daily run lock exists; runtime activation is not permitted."}
        if (Test-Path -LiteralPath $path) { $null = Get-DawnstrikeStrictRuntimeLock $path $PythonPath $PythonSha256; throw "A runtime activation lock already exists and requires governed recovery." }
        $token = [guid]::NewGuid().ToString("N")
        $json = (New-DawnstrikeRuntimeLockPayload $Operation $CandidateSha $CandidateTree $OriginIdentity $token) | ConvertTo-Json -Compress
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($json)
        $stream = [IO.File]::Open($path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        try { $stream.Write($bytes,0,$bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
        $strict = Get-DawnstrikeStrictRuntimeLock $path $PythonPath $PythonSha256
        if ($strict.payload.lock_token -ne $token) { throw "Runtime activation lock read-back token mismatch." }
        $dailyAfter=@(Get-ChildItem -LiteralPath $lockRoot -Filter 'dawnstrike-daily-*.lock' -File -Force -ErrorAction SilentlyContinue)
        if($dailyAfter.Count){
            $owned=Get-DawnstrikeStrictRuntimeLock $path $PythonPath $PythonSha256
            if($owned.payload.lock_token-ne $token-or $owned.raw_file_sha256-ne $strict.raw_file_sha256){throw 'Activation lock changed during daily-lock race; lock retained.'}
            Remove-Item -LiteralPath $path -Force
            if(Test-Path -LiteralPath $path){throw 'Owned activation lock could not be relinquished after daily-lock race.'}
            throw 'A daily run lock appeared during activation lock acquisition.'
        }
        return [pscustomobject]@{ path=$path; token=$token; bytes_sha256=[string]$strict.raw_file_sha256; operation=$Operation; python_path=$PythonPath; python_sha256=$PythonSha256; acquired=$true }
    } finally { Exit-DawnstrikeRuntimeLockMutex $mutex }
}

function Adopt-DawnstrikeGovernedRuntimeLock {
    [CmdletBinding()]
    param([string]$StateRoot,[string]$ExpectedToken,[string]$ExpectedFileSha256,[string]$ExpectedOperation,[string]$CandidateSha,[string]$CandidateTree,[string]$OriginIdentity,[string]$PythonPath,[string]$PythonSha256)
    $state = Assert-DawnstrikeRuntimeLockStateRoot $StateRoot
    $path = Join-Path $state "locks\dawnstrike-runtime-activation.lock"
    $mutex = Enter-DawnstrikeRuntimeLockMutex
    try {
        $stale = Get-DawnstrikeStrictRuntimeLock $path $PythonPath $PythonSha256
        if ($stale.payload.lock_token -ne $ExpectedToken -or $stale.raw_file_sha256 -ne $ExpectedFileSha256 -or $stale.payload.operation -ne $ExpectedOperation -or $stale.payload.candidate_sha -ne $CandidateSha -or $stale.payload.candidate_tree -ne $CandidateTree -or $stale.payload.origin_identity -ne $OriginIdentity) { throw "Stale runtime lock does not match the PREPARED contract." }
        if (-not (Test-DawnstrikeRuntimeLockOwnerDead $stale.payload)) { throw "Runtime activation lock owner is still active." }
        $archive = Join-Path (Split-Path $path -Parent) ("recovered-stale-" + $ExpectedFileSha256 + ".lock")
        Assert-DawnstrikeSharedLockNoReparse $archive 'Stale lock archive'
        if (Test-Path -LiteralPath $archive) { throw "Stale lock archive already exists; adoption is ambiguous." }
        $token=[guid]::NewGuid().ToString("N")
        $json=(New-DawnstrikeRuntimeLockPayload "recovery" $CandidateSha $CandidateTree $OriginIdentity $token)|ConvertTo-Json -Compress
        $temp=Join-Path (Split-Path $path -Parent) (".lock-recovery-"+[guid]::NewGuid().ToString('N')+".tmp")
        Assert-DawnstrikeSharedLockNoReparse $temp 'Recovery lock temporary file'
        $bytes=[Text.UTF8Encoding]::new($false).GetBytes($json)
        $stream=[IO.File]::Open($temp,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        try{$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
        try { [IO.File]::Replace($temp,$path,$archive,$true) } finally { if(Test-Path -LiteralPath $temp){Remove-Item -LiteralPath $temp -Force} }
        if ((Get-DawnstrikeRuntimeLockHash $archive) -ne $ExpectedFileSha256) { throw "Stale lock archive changed during atomic adoption." }
        $current=Get-DawnstrikeStrictRuntimeLock $path $PythonPath $PythonSha256
        return [pscustomobject]@{path=$path;token=$token;bytes_sha256=$current.raw_file_sha256;operation="recovery";python_path=$PythonPath;python_sha256=$PythonSha256;stale_archive=$archive;acquired=$true}
    } finally { Exit-DawnstrikeRuntimeLockMutex $mutex }
}

function Exit-DawnstrikeGovernedRuntimeLock {
    param([AllowNull()][object]$Lock)
    if ($null -eq $Lock) { return }
    $mutex=Enter-DawnstrikeRuntimeLockMutex
    try {
        $current=Get-DawnstrikeStrictRuntimeLock $Lock.path $Lock.python_path $Lock.python_sha256
        if ($current.payload.lock_token -ne $Lock.token -or $current.raw_file_sha256 -ne $Lock.bytes_sha256) { throw "Runtime activation lock ownership changed; lock retained." }
        Remove-Item -LiteralPath $Lock.path -Force
        if (Test-Path -LiteralPath $Lock.path) { throw "Runtime activation lock release was not proven." }
    } finally { Exit-DawnstrikeRuntimeLockMutex $mutex }
}
