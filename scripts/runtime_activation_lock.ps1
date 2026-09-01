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
    $script:DawnstrikeLockMutexAbandoned = $false
    try { $owned=$mutex.WaitOne([TimeSpan]::FromSeconds(30)) }
    catch [Threading.AbandonedMutexException] { $owned=$true; $script:DawnstrikeLockMutexAbandoned=$true }
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
        if($script:DawnstrikeLockMutexAbandoned){$script:DawnstrikeLockMutexAbandoned=$false;throw 'Runtime activation lock mutex was abandoned; only exact stale-lock recovery is permitted.'}
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
    param([string]$StateRoot,[string]$ExpectedToken,[string]$ExpectedFileSha256,[string]$ExpectedOperation,[string]$CandidateSha,[string]$CandidateTree,[string]$OriginIdentity,[string]$PythonPath,[string]$PythonSha256,[ValidateSet("capture_task_rebind","runtime_activation","runtime_rollback","recovery")][string]$RecoveryOperation="recovery")
    $state = Assert-DawnstrikeRuntimeLockStateRoot $StateRoot
    $path = Join-Path $state "locks\dawnstrike-runtime-activation.lock"
    $mutex = Enter-DawnstrikeRuntimeLockMutex
    try {
        $script:DawnstrikeLockMutexAbandoned=$false
        $stale = Get-DawnstrikeStrictRuntimeLock $path $PythonPath $PythonSha256
        if ($stale.payload.lock_token -ne $ExpectedToken -or $stale.raw_file_sha256 -ne $ExpectedFileSha256 -or $stale.payload.operation -ne $ExpectedOperation -or $stale.payload.candidate_sha -ne $CandidateSha -or $stale.payload.candidate_tree -ne $CandidateTree -or $stale.payload.origin_identity -ne $OriginIdentity) { throw "Stale runtime lock does not match the PREPARED contract." }
        if (-not (Test-DawnstrikeRuntimeLockOwnerDead $stale.payload)) { throw "Runtime activation lock owner is still active." }
        $archive = Join-Path (Split-Path $path -Parent) ("recovered-stale-" + $ExpectedFileSha256 + ".lock")
        Assert-DawnstrikeSharedLockNoReparse $archive 'Stale lock archive'
        if (Test-Path -LiteralPath $archive) { throw "Stale lock archive already exists; adoption is ambiguous." }
        $token=[guid]::NewGuid().ToString("N")
        $json=(New-DawnstrikeRuntimeLockPayload $RecoveryOperation $CandidateSha $CandidateTree $OriginIdentity $token)|ConvertTo-Json -Compress
        $temp=Join-Path (Split-Path $path -Parent) (".lock-recovery-"+[guid]::NewGuid().ToString('N')+".tmp")
        Assert-DawnstrikeSharedLockNoReparse $temp 'Recovery lock temporary file'
        $bytes=[Text.UTF8Encoding]::new($false).GetBytes($json)
        $stream=[IO.File]::Open($temp,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        try{$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
        try { [IO.File]::Replace($temp,$path,$archive,$true) } finally { if(Test-Path -LiteralPath $temp){Remove-Item -LiteralPath $temp -Force} }
        if ((Get-DawnstrikeRuntimeLockHash $archive) -ne $ExpectedFileSha256) { throw "Stale lock archive changed during atomic adoption." }
        $current=Get-DawnstrikeStrictRuntimeLock $path $PythonPath $PythonSha256
        return [pscustomobject]@{path=$path;token=$token;bytes_sha256=$current.raw_file_sha256;operation=$RecoveryOperation;python_path=$PythonPath;python_sha256=$PythonSha256;stale_archive=$archive;acquired=$true}
    } finally { Exit-DawnstrikeRuntimeLockMutex $mutex }
}

function Get-DawnstrikeStrictRuntimeOperationJournal {
    param([string]$Path,[string]$PythonPath,[string]$PythonSha256)
    $contract=Join-Path $PSScriptRoot 'runtime_operation_journal.py'
    Assert-DawnstrikeSharedLockNoReparse $contract 'Runtime operation journal contract'
    Assert-DawnstrikeSharedLockNoReparse $Path 'Runtime operation journal'
    if($PythonPath-ne $script:DawnstrikeApprovedPythonPath-or $PythonSha256-ne $script:DawnstrikeApprovedPythonSha256){throw 'Journal interpreter is not approved.'}
    $item=Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if($item.PSIsContainer-or($item.Attributes-band [IO.FileAttributes]::ReparsePoint)-or$item.Length-gt 65536){throw 'Runtime operation journal leaf is unsafe.'}
    $state=(Split-Path (Split-Path (Split-Path $Path -Parent) -Parent) -Parent)
    $output=& $PythonPath '-I' '-B' $contract 'verify' $Path '--state-root' $state 2>$null
    if($LASTEXITCODE-ne 0){throw 'Runtime operation journal is malformed or unsafe.'}
    try{return ([string]($output-join''))|ConvertFrom-Json}catch{throw 'Runtime operation journal validator returned invalid output.'}
}

function Set-DawnstrikeRuntimeOperationJournalAdoption {
    param([object]$Journal,[string]$JournalPath,[string]$State,[string]$CurrentToken,
        [string]$CurrentHash,[string]$OldToken,[string]$OldHash,[string]$NextToken,
        [string]$NextHash,[string]$ArchiveRelative,[string]$NextRelative,
        [string]$PythonPath,[string]$PythonSha256)
    $contract=Join-Path $PSScriptRoot 'runtime_operation_journal.py'
    $payload=[ordered]@{}
    foreach($property in $Journal.payload.PSObject.Properties){
        if($property.Name-ne'journal_self_sha256'){$payload[$property.Name]=$property.Value}
    }
    $payload.adoption_state=$State;$payload.lock_token=$CurrentToken
    $payload.lock_file_sha256=$CurrentHash;$payload.old_lock_token=$OldToken
    $payload.old_lock_file_sha256=$OldHash;$payload.next_lock_token=$NextToken
    $payload.next_lock_file_sha256=$NextHash
    $payload.old_lock_archive_relative_path=$ArchiveRelative
    $payload.next_lock_relative_path=$NextRelative
    $payload.prior_journal_file_sha256=[string]$Journal.raw_file_sha256
    $payload.recorded_at_utc=[DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ')
    $input=Join-Path (Split-Path $JournalPath -Parent) ('.journal-input-'+[guid]::NewGuid().ToString('N')+'.json')
    Assert-DawnstrikeSharedLockNoReparse $input 'Runtime operation journal input'
    $bytes=[Text.UTF8Encoding]::new($false).GetBytes(($payload|ConvertTo-Json -Depth 8 -Compress))
    $stream=[IO.File]::Open($input,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try{$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
    try{
        $state=(Split-Path (Split-Path (Split-Path $JournalPath -Parent) -Parent) -Parent)
        $output=& $PythonPath '-I' '-B' $contract 'seal' $input $JournalPath '--state-root' $state 2>$null
        if($LASTEXITCODE-ne 0){throw 'Runtime operation journal adoption seal failed.'}
        try{return ([string]($output-join''))|ConvertFrom-Json}catch{throw 'Journal seal returned invalid output.'}
    }finally{if(Test-Path -LiteralPath $input){Remove-Item -LiteralPath $input -Force}}
}

function Adopt-DawnstrikeGovernedRuntimeLockWithJournal {
    [CmdletBinding()]
    param([string]$StateRoot,[string]$JournalPath,[string]$CandidateSha,
        [string]$CandidateTree,[string]$OriginIdentity,[string]$PythonPath,
        [string]$PythonSha256,[ValidateSet('after_prepared','after_replace')][string]$TestCrashPoint='')
    if($TestCrashPoint-and$env:DAWNSTRIKE_TEST_LOCK_JOURNAL-ne'1'){throw 'Journal crash injection is test-only.'}
    $state=Assert-DawnstrikeRuntimeLockStateRoot $StateRoot
    $statePrefix=$state.TrimEnd('\')+'\'
    $journalFull=[IO.Path]::GetFullPath($JournalPath)
    if(-not$journalFull.StartsWith($statePrefix,[StringComparison]::OrdinalIgnoreCase)){throw 'Journal must be inside StateRoot.'}
    $path=Join-Path $state 'locks\dawnstrike-runtime-activation.lock'
    $mutex=Enter-DawnstrikeRuntimeLockMutex
    try{
        while($true){
        $journal=Get-DawnstrikeStrictRuntimeOperationJournal $journalFull $PythonPath $PythonSha256
        $payload=$journal.payload
        if($payload.candidate_sha-ne$CandidateSha-or$payload.candidate_tree-ne$CandidateTree-or$payload.origin_identity-ne$OriginIdentity){throw 'Journal source identity does not match recovery.'}
        $current=Get-DawnstrikeStrictRuntimeLock $path $PythonPath $PythonSha256
        $ownerStart=(Get-Process -Id $PID).StartTime.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ')
        $ownedByCurrent=([int]$current.payload.process_id-eq[int]$PID-and[string]$current.payload.process_started_at_utc-eq$ownerStart)
        if(-not$ownedByCurrent-and-not(Test-DawnstrikeRuntimeLockOwnerDead $current.payload)){throw 'Runtime activation lock owner is still active.'}
        $lockRoot=Split-Path $path -Parent
        if($payload.adoption_state-eq'NONE'-or$payload.adoption_state-eq'ADOPTED'){
            if($payload.lock_token-ne$current.payload.lock_token-or$payload.lock_file_sha256-ne$current.raw_file_sha256){throw 'Journal does not bind the stale lock.'}
            if($ownedByCurrent){return [pscustomobject]@{path=$path;token=[string]$current.payload.lock_token;bytes_sha256=[string]$current.raw_file_sha256;operation=[string]$current.payload.operation;python_path=$PythonPath;python_sha256=$PythonSha256;acquired=$true;journal_path=$journalFull;journal_sha256=[string]$journal.raw_file_sha256}}
            $oldToken=[string]$current.payload.lock_token;$oldHash=[string]$current.raw_file_sha256
            $nextToken=[guid]::NewGuid().ToString('N')
            $nextJson=(New-DawnstrikeRuntimeLockPayload ([string]$current.payload.operation) $CandidateSha $CandidateTree $OriginIdentity $nextToken)|ConvertTo-Json -Compress
            $nextBytes=[Text.UTF8Encoding]::new($false).GetBytes($nextJson)
            $nextHash=Get-DawnstrikeSharedLockSha256Text $nextJson
            $nextName='.next-runtime-lock-'+$nextHash+'.tmp';$nextPath=Join-Path $lockRoot $nextName
            $nextStream=[IO.File]::Open($nextPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
            try{$nextStream.Write($nextBytes,0,$nextBytes.Length);$nextStream.Flush($true)}finally{$nextStream.Dispose()}
            $archiveName='recovered-stale-'+$oldHash+'.lock'
            $journal=Set-DawnstrikeRuntimeOperationJournalAdoption $journal $journalFull 'ADOPTION_PREPARED' $oldToken $oldHash $oldToken $oldHash $nextToken $nextHash ('locks/'+$archiveName) ('locks/'+$nextName) $PythonPath $PythonSha256
            if($TestCrashPoint-eq'after_prepared'){exit 137}
            $payload=$journal.payload
        }elseif($payload.adoption_state-ne'ADOPTION_PREPARED'-and$payload.adoption_state-ne'ADOPTED'){throw 'Journal adoption state is not recoverable.'}
        $oldPath=Join-Path $state ([string]$payload.old_lock_archive_relative_path).Replace('/','\')
        $nextPath=if($payload.next_lock_relative_path-ne'NONE'){Join-Path $state ([string]$payload.next_lock_relative_path).Replace('/','\')}else{$null}
        if($payload.adoption_state-eq'ADOPTION_PREPARED'){
            if($current.raw_file_sha256-eq$payload.old_lock_file_sha256){
                if(-not(Test-Path -LiteralPath $nextPath -PathType Leaf)-or(Get-DawnstrikeRuntimeLockHash $nextPath)-ne$payload.next_lock_file_sha256){throw 'Prepared next-lock bytes are missing or changed.'}
                [IO.File]::Replace($nextPath,$path,$oldPath,$true)
            }elseif($current.raw_file_sha256-ne$payload.next_lock_file_sha256-or-not(Test-Path -LiteralPath $oldPath -PathType Leaf)-or(Get-DawnstrikeRuntimeLockHash $oldPath)-ne$payload.old_lock_file_sha256){throw 'Prepared adoption lock/archive pair is invalid.'}
            if($TestCrashPoint-eq'after_replace'){exit 137}
            $current=Get-DawnstrikeStrictRuntimeLock $path $PythonPath $PythonSha256
            $journal=Set-DawnstrikeRuntimeOperationJournalAdoption $journal $journalFull 'ADOPTED' ([string]$current.payload.lock_token) ([string]$current.raw_file_sha256) ([string]$payload.old_lock_token) ([string]$payload.old_lock_file_sha256) ([string]$payload.next_lock_token) ([string]$payload.next_lock_file_sha256) ([string]$payload.old_lock_archive_relative_path) 'NONE' $PythonPath $PythonSha256
        }
        $current=Get-DawnstrikeStrictRuntimeLock $path $PythonPath $PythonSha256
        # A recovered precomputed lock can belong to a recovery process that
        # crashed after replacement. Loop under the same mutex until the lock
        # payload is owned by this exact PID/start pair.
        }
    }finally{Exit-DawnstrikeRuntimeLockMutex $mutex}
}

function Set-DawnstrikeRuntimeOperationJournalPhase {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$StateRoot,
        [Parameter(Mandatory=$true)][string]$JournalPath,
        [Parameter(Mandatory=$true)][object]$Lock,
        [ValidateSet('runtime_activation','runtime_rollback','capture_task_rebind','capture_task_hardening')][string]$Operation,
        [ValidateSet('INIT','PRE_QUIESCE','PRE_SWAP','POST_SWAP','PRE_ENABLE','POST_ENABLE','PRE_TASK_UPDATE','POST_TASK_UPDATE','COMPLETE')][string]$Phase,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateSha,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateTree,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$CurrentSha,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$CurrentTree,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$PreviousSha,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$PreviousTree,
        [Parameter(Mandatory=$true)][string]$OriginIdentity,
        [Parameter(Mandatory=$true)][string]$PreparedReceiptRelativePath,
        [ValidatePattern('^[0-9a-f]{64}$')][string]$PreparedReceiptSha256,
        [Parameter(Mandatory=$true)][string]$CompleteReceiptRelativePath,
        [ValidatePattern('^[0-9a-f]{64}$')][string]$CompleteReceiptSha256,
        [ValidatePattern('^[0-9a-f]{64}$')][string]$BackupContractSha256,
        [ValidatePattern('^[0-9a-f]{64}$')][string]$TaskContractSha256,
        [ValidatePattern('^[0-9a-f]{64}$')][string]$RuntimeStageContractSha256,
        [Parameter(Mandatory=$true)][string]$PythonPath,
        [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{64}$')][string]$PythonSha256
    )
    $state=Assert-DawnstrikeRuntimeLockStateRoot $StateRoot
    $journalFull=[IO.Path]::GetFullPath($JournalPath)
    $journalRoot=[IO.Path]::GetFullPath((Join-Path $state 'receipts\runtime-operation')).TrimEnd('\')+'\'
    if(-not$journalFull.StartsWith($journalRoot,[StringComparison]::OrdinalIgnoreCase)){throw 'Runtime operation journal must be inside its governed receipt root.'}
    Assert-DawnstrikeSharedLockNoReparse $journalFull 'Runtime operation journal'
    $current=Get-DawnstrikeStrictRuntimeLock $Lock.path $PythonPath $PythonSha256
    $processStart=(Get-Process -Id $PID).StartTime.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ')
    if($current.payload.lock_token-ne$Lock.token-or$current.raw_file_sha256-ne$Lock.bytes_sha256-or[int]$current.payload.process_id-ne[int]$PID-or[string]$current.payload.process_started_at_utc-ne$processStart){throw 'Journal transition requires the exact live lock owned by this process.'}
    if($current.payload.operation-ne$Operation-or$current.payload.candidate_sha-ne$CandidateSha-or$current.payload.candidate_tree-ne$CandidateTree-or$current.payload.origin_identity-ne$OriginIdentity){throw 'Journal transition lock identity does not match the operation.'}
    $phases=@{
        runtime_activation=@('INIT','PRE_QUIESCE','PRE_SWAP','POST_SWAP','COMPLETE')
        runtime_rollback=@('INIT','PRE_SWAP','POST_SWAP','COMPLETE')
        capture_task_rebind=@('INIT','PRE_ENABLE','POST_ENABLE','COMPLETE')
        capture_task_hardening=@('INIT','PRE_TASK_UPDATE','POST_TASK_UPDATE','COMPLETE')
    }
    $sequence=[array]::IndexOf([object[]]$phases[$Operation],$Phase)
    if($sequence-lt 0){throw 'Journal phase is invalid for the operation.'}
    $empty=Get-DawnstrikeSharedLockSha256Text ''
    $priorHash=$empty
    $initOwnerProcessId=[int]$current.payload.process_id
    $initOwnerStartedAtUtc=[string]$current.payload.process_started_at_utc
    if($Phase-ne'INIT'){
        $prior=Get-DawnstrikeStrictRuntimeOperationJournal $journalFull $PythonPath $PythonSha256
        $priorHash=[string]$prior.raw_file_sha256
        $initOwnerProcessId=[int]$prior.payload.init_owner_process_id
        $initOwnerStartedAtUtc=[string]$prior.payload.init_owner_started_at_utc
    }elseif(Test-Path -LiteralPath $journalFull){throw 'INIT journal already exists.'}
    $payload=[ordered]@{
        schema_version='dawnstrike.runtime_operation_journal.v1';operation=$Operation;phase=$Phase;sequence=$sequence
        candidate_sha=$CandidateSha;candidate_tree=$CandidateTree;current_sha=$CurrentSha;current_tree=$CurrentTree
        previous_sha=$PreviousSha;previous_tree=$PreviousTree;origin_identity=$OriginIdentity
        origin_identity_sha256=Get-DawnstrikeSharedLockSha256Text $OriginIdentity
        state_root_sha256=Get-DawnstrikeSharedLockSha256Text $state.ToLowerInvariant()
        lock_token=[string]$current.payload.lock_token;lock_file_sha256=[string]$current.raw_file_sha256
        prior_journal_file_sha256=$priorHash
        prepared_receipt_relative_path=$PreparedReceiptRelativePath;prepared_receipt_sha256=$PreparedReceiptSha256
        complete_receipt_relative_path=$CompleteReceiptRelativePath;complete_receipt_sha256=$CompleteReceiptSha256
        backup_contract_sha256=$BackupContractSha256;task_contract_sha256=$TaskContractSha256
        runtime_stage_contract_sha256=$RuntimeStageContractSha256;recorded_at_utc=[DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ')
        research_only=$true;broker_execution_enabled=$false;adoption_state='NONE'
        old_lock_token=[string]$current.payload.lock_token;old_lock_file_sha256=[string]$current.raw_file_sha256
        next_lock_token=[string]$current.payload.lock_token;next_lock_file_sha256=[string]$current.raw_file_sha256
        old_lock_archive_relative_path='NONE';next_lock_relative_path='NONE'
        init_owner_process_id=$initOwnerProcessId;init_owner_started_at_utc=$initOwnerStartedAtUtc
    }
    New-Item -ItemType Directory -Path (Split-Path $journalFull -Parent) -Force|Out-Null
    $input=Join-Path (Split-Path $journalFull -Parent) ('.journal-transition-'+[guid]::NewGuid().ToString('N')+'.json')
    $bytes=[Text.UTF8Encoding]::new($false).GetBytes(($payload|ConvertTo-Json -Depth 8 -Compress))
    $stream=[IO.File]::Open($input,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try{$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
    try{
        $contract=Join-Path $PSScriptRoot 'runtime_operation_journal.py'
        $arguments=@('-I','-B',$contract,'transition',$input,$journalFull,'--state-root',$state)
        if($Phase-ne'INIT'){$arguments+=@('--previous',$journalFull)}
        $output=& $PythonPath @arguments 2>$null
        if($LASTEXITCODE-ne 0){throw 'Runtime operation journal phase transition failed.'}
        try{return ([string]($output-join''))|ConvertFrom-Json}catch{throw 'Journal transition returned invalid output.'}
    }finally{if(Test-Path -LiteralPath $input){Remove-Item -LiteralPath $input -Force}}
}

function Enter-DawnstrikeGovernedRuntimeLockWithJournal {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$StateRoot,
        [Parameter(Mandatory=$true)][string]$JournalPath,
        [ValidateSet('runtime_activation','runtime_rollback','capture_task_rebind','capture_task_hardening')][string]$Operation,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateSha,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateTree,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$CurrentSha,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$CurrentTree,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$PreviousSha,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$PreviousTree,
        [Parameter(Mandatory=$true)][string]$OriginIdentity,
        [Parameter(Mandatory=$true)][string]$PreparedReceiptRelativePath,
        [Parameter(Mandatory=$true)][string]$CompleteReceiptRelativePath,
        [ValidatePattern('^[0-9a-f]{64}$')][string]$TaskContractSha256,
        [Parameter(Mandatory=$true)][string]$PythonPath,
        [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{64}$')][string]$PythonSha256,
        [ValidateSet('after_init','after_lock')][string]$TestCrashPoint='',
        [switch]$TestInjectDailyLockRace
    )
    if($TestCrashPoint-and$env:DAWNSTRIKE_TEST_LOCK_JOURNAL-ne'1'){throw 'Journal acquisition crash injection is test-only.'}
    $state=Assert-DawnstrikeRuntimeLockStateRoot $StateRoot
    $journalFull=[IO.Path]::GetFullPath($JournalPath)
    $journalRoot=[IO.Path]::GetFullPath((Join-Path $state 'receipts\runtime-operation')).TrimEnd('\')+'\'
    if(-not$journalFull.StartsWith($journalRoot,[StringComparison]::OrdinalIgnoreCase)){throw 'Runtime operation journal must be inside its governed receipt root.'}
    if($TaskContractSha256-eq(Get-DawnstrikeSharedLockSha256Text '')){throw 'Journal acquisition requires a nonempty task contract hash.'}
    Assert-DawnstrikeSharedLockNoReparse $journalFull 'Runtime operation journal'
    $lockRoot=Join-Path $state 'locks';New-Item -ItemType Directory -Path $lockRoot -Force|Out-Null
    Assert-DawnstrikeSharedLockNoReparse $lockRoot 'Runtime activation lock root'
    $lockRootItem=Get-Item -LiteralPath $lockRoot -Force
    if($lockRootItem.Attributes-band[IO.FileAttributes]::ReparsePoint){throw 'Runtime activation lock root is unsafe.'}
    $lockPath=Join-Path $lockRoot 'dawnstrike-runtime-activation.lock'
    $mutex=Enter-DawnstrikeRuntimeLockMutex
    try{
        $abandoned=[bool]$script:DawnstrikeLockMutexAbandoned
        $script:DawnstrikeLockMutexAbandoned=$false
        $dailyBefore=@(Get-ChildItem -LiteralPath $lockRoot -Filter 'dawnstrike-daily-*.lock' -File -Force -ErrorAction SilentlyContinue)
        if($dailyBefore.Count){throw 'A daily run lock exists; runtime lock initialization is not permitted.'}
        $hasJournal=Test-Path -LiteralPath $journalFull -PathType Leaf
        $hasLock=Test-Path -LiteralPath $lockPath -PathType Leaf
        if($hasJournal){
            $journal=Get-DawnstrikeStrictRuntimeOperationJournal $journalFull $PythonPath $PythonSha256
            if([string]$journal.payload.phase-ne'INIT'-or[string]$journal.payload.operation-ne$Operation-or[string]$journal.payload.candidate_sha-ne$CandidateSha-or[string]$journal.payload.candidate_tree-ne$CandidateTree-or[string]$journal.payload.current_sha-ne$CurrentSha-or[string]$journal.payload.current_tree-ne$CurrentTree-or[string]$journal.payload.previous_sha-ne$PreviousSha-or[string]$journal.payload.previous_tree-ne$PreviousTree-or[string]$journal.payload.origin_identity-ne$OriginIdentity-or[string]$journal.payload.prepared_receipt_relative_path-ne$PreparedReceiptRelativePath-or[string]$journal.payload.complete_receipt_relative_path-ne$CompleteReceiptRelativePath-or[string]$journal.payload.task_contract_sha256-ne$TaskContractSha256){throw 'Existing INIT journal identity is invalid.'}
            if(-not$hasLock){
                $initOwner=[pscustomobject]@{process_id=[int]$journal.payload.init_owner_process_id;process_started_at_utc=[string]$journal.payload.init_owner_started_at_utc}
                if(-not$abandoned-and-not(Test-DawnstrikeRuntimeLockOwnerDead $initOwner)){throw 'Orphan INIT journal owner is still active.'}
                $before=[string]$journal.raw_file_sha256
                if((Get-DawnstrikeStrictRuntimeOperationJournal $journalFull $PythonPath $PythonSha256).raw_file_sha256-ne$before){throw 'Orphan INIT journal changed during recovery.'}
                Remove-Item -LiteralPath $journalFull -Force
                if(Test-Path -LiteralPath $journalFull){throw 'Orphan INIT journal cleanup failed.'}
                $hasJournal=$false
            }else{
                $lock=Get-DawnstrikeStrictRuntimeLock $lockPath $PythonPath $PythonSha256
                if($journal.payload.lock_token-ne$lock.payload.lock_token-or$journal.payload.lock_file_sha256-ne$lock.raw_file_sha256){throw 'INIT journal and lock do not match.'}
                if(-not(Test-DawnstrikeRuntimeLockOwnerDead $lock.payload)){throw 'Runtime activation lock owner is still active.'}
                return Adopt-DawnstrikeGovernedRuntimeLockWithJournal -StateRoot $state -JournalPath $journalFull -CandidateSha $CandidateSha -CandidateTree $CandidateTree -OriginIdentity $OriginIdentity -PythonPath $PythonPath -PythonSha256 $PythonSha256
            }
        }elseif($hasLock){throw 'Runtime lock exists without its exact INIT journal.'}
        $token=[guid]::NewGuid().ToString('N')
        $lockJson=(New-DawnstrikeRuntimeLockPayload $Operation $CandidateSha $CandidateTree $OriginIdentity $token)|ConvertTo-Json -Compress
        $lockBytes=[Text.UTF8Encoding]::new($false).GetBytes($lockJson)
        $lockHash=Get-DawnstrikeSharedLockSha256Text $lockJson
        $empty=Get-DawnstrikeSharedLockSha256Text ''
        $payload=[ordered]@{
            schema_version='dawnstrike.runtime_operation_journal.v1';operation=$Operation;phase='INIT';sequence=0
            candidate_sha=$CandidateSha;candidate_tree=$CandidateTree;current_sha=$CurrentSha;current_tree=$CurrentTree
            previous_sha=$PreviousSha;previous_tree=$PreviousTree;origin_identity=$OriginIdentity
            origin_identity_sha256=Get-DawnstrikeSharedLockSha256Text $OriginIdentity
            state_root_sha256=Get-DawnstrikeSharedLockSha256Text $state.ToLowerInvariant()
            lock_token=$token;lock_file_sha256=$lockHash;prior_journal_file_sha256=$empty
            prepared_receipt_relative_path=$PreparedReceiptRelativePath;prepared_receipt_sha256=$empty
            complete_receipt_relative_path=$CompleteReceiptRelativePath;complete_receipt_sha256=$empty
            backup_contract_sha256=$empty;task_contract_sha256=$TaskContractSha256;runtime_stage_contract_sha256=$empty
            recorded_at_utc=[DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ');research_only=$true;broker_execution_enabled=$false
            adoption_state='NONE';old_lock_token=$token;old_lock_file_sha256=$lockHash;next_lock_token=$token;next_lock_file_sha256=$lockHash
            old_lock_archive_relative_path='NONE';next_lock_relative_path='NONE'
            init_owner_process_id=[int]$PID;init_owner_started_at_utc=(Get-Process -Id $PID).StartTime.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ')
        }
        New-Item -ItemType Directory -Path (Split-Path $journalFull -Parent) -Force|Out-Null
        $input=Join-Path (Split-Path $journalFull -Parent) ('.journal-init-'+[guid]::NewGuid().ToString('N')+'.json')
        $inputBytes=[Text.UTF8Encoding]::new($false).GetBytes(($payload|ConvertTo-Json -Depth 8 -Compress))
        $inputStream=[IO.File]::Open($input,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        try{$inputStream.Write($inputBytes,0,$inputBytes.Length);$inputStream.Flush($true)}finally{$inputStream.Dispose()}
        try{
            $contract=Join-Path $PSScriptRoot 'runtime_operation_journal.py'
            $output=& $PythonPath '-I' '-B' $contract 'transition' $input $journalFull '--state-root' $state 2>$null
            if($LASTEXITCODE -ne 0){throw 'INIT journal sealing failed.'}
        }finally{if(Test-Path $input){Remove-Item $input -Force}}
        if($TestCrashPoint-eq'after_init'){Stop-Process -Id $PID -Force}
        $stream=[IO.File]::Open($lockPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        try{$stream.Write($lockBytes,0,$lockBytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
        $strict=Get-DawnstrikeStrictRuntimeLock $lockPath $PythonPath $PythonSha256
        if($strict.raw_file_sha256-ne$lockHash-or$strict.payload.lock_token-ne$token){throw 'Created lock does not match INIT journal.'}
        if($TestCrashPoint-eq'after_lock'){Stop-Process -Id $PID -Force}
        if($TestInjectDailyLockRace){
            if($env:DAWNSTRIKE_TEST_LOCK_JOURNAL-ne'1'){throw 'Daily-lock race injection is test-only.'}
            [IO.File]::WriteAllText((Join-Path $lockRoot 'dawnstrike-daily-injected.lock'),'test')
        }
        $dailyAfter=@(Get-ChildItem -LiteralPath $lockRoot -Filter 'dawnstrike-daily-*.lock' -File -Force -ErrorAction SilentlyContinue)
        if($dailyAfter.Count){
            $ownedLock=Get-DawnstrikeStrictRuntimeLock $lockPath $PythonPath $PythonSha256
            $ownedJournal=Get-DawnstrikeStrictRuntimeOperationJournal $journalFull $PythonPath $PythonSha256
            if($ownedLock.payload.lock_token-ne$token-or$ownedLock.raw_file_sha256-ne$lockHash-or$ownedJournal.payload.lock_token-ne$token-or$ownedJournal.payload.lock_file_sha256-ne$lockHash){throw 'Initialized lock or journal changed during daily-lock race; evidence retained.'}
            Remove-Item -LiteralPath $lockPath -Force
            Remove-Item -LiteralPath $journalFull -Force
            if((Test-Path $lockPath)-or(Test-Path $journalFull)){throw 'Owned initialization evidence could not be cleaned after daily-lock race.'}
            throw 'A daily run lock appeared during runtime lock initialization.'
        }
        return [pscustomobject]@{path=$lockPath;token=$token;bytes_sha256=$lockHash;operation=$Operation;python_path=$PythonPath;python_sha256=$PythonSha256;acquired=$true;journal_path=$journalFull}
    }finally{Exit-DawnstrikeRuntimeLockMutex $mutex}
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
