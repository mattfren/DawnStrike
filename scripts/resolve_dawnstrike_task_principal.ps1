function Resolve-DawnstrikeTaskPrincipal {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [pscredential]$Credential
    )

    $requestedPrincipal = [string]$Credential.UserName
    if ([string]::IsNullOrWhiteSpace($requestedPrincipal)) {
        throw "RunAsCredential.UserName is blank."
    }
    $requestedPrincipal = $requestedPrincipal.Trim()

    try {
        $requestedAccount = [System.Security.Principal.NTAccount]::new($requestedPrincipal)
        $requestedSid = $requestedAccount.Translate(
            [System.Security.Principal.SecurityIdentifier]
        )
        $canonicalAccount = $requestedSid.Translate(
            [System.Security.Principal.NTAccount]
        )
        return [string]$canonicalAccount.Value
    }
    catch [System.Security.Principal.IdentityNotMappedException] {
        $currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
        $currentPrincipal = [string]$currentIdentity.Name
        $currentShortName = ($currentPrincipal -split "\\")[-1]

        if (
            $requestedPrincipal -notmatch "\\" -and
            $requestedPrincipal -notmatch "@" -and
            $requestedPrincipal -ieq $currentShortName
        ) {
            $currentAccount = [System.Security.Principal.NTAccount]::new($currentPrincipal)
            $currentSid = $currentAccount.Translate(
                [System.Security.Principal.SecurityIdentifier]
            )
            $canonicalCurrentAccount = $currentSid.Translate(
                [System.Security.Principal.NTAccount]
            )
            Write-Verbose (
                "Resolved unqualified task principal '$requestedPrincipal' " +
                "to '$currentPrincipal'."
            )
            return [string]$canonicalCurrentAccount.Value
        }

        throw (
            "Task Scheduler principal '$requestedPrincipal' cannot be mapped to a Windows SID. " +
            "Use the canonical identity shown by whoami (for example, 'AzureAD\MattFields'), " +
            "then create the credential again with Get-Credential '<canonical identity>'."
        )
    }
}
