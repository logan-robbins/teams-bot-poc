param(
    [string]$CertSubjectHosts = "alfred-disney-bot.eastus.cloudapp.azure.com",
    [string]$CertFriendlyNamePattern = "alfred-disney-cert*,alfred-bot-cert*"
)

$ErrorActionPreference = "Stop"

$subjectPatterns = @($CertSubjectHosts -split "," | ForEach-Object { "CN=" + [regex]::Escape($_.Trim()) } | Where-Object { $_ -ne "CN=" })
$friendlyPatterns = @($CertFriendlyNamePattern -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })

$cert = Get-ChildItem Cert:\LocalMachine\My |
    Where-Object {
        $candidate = $_
        ($subjectPatterns | Where-Object { $candidate.Subject -match $_ }).Count -gt 0 -or
        ($friendlyPatterns | Where-Object { $candidate.FriendlyName -like $_ }).Count -gt 0
    } |
    Sort-Object NotAfter -Descending |
    Select-Object -First 1 Subject, Thumbprint, NotBefore, NotAfter, FriendlyName, HasPrivateKey

if (-not $cert) {
    throw "No bot certificate found in Cert:\LocalMachine\My (looked for Subject in [$CertSubjectHosts], FriendlyName in [$CertFriendlyNamePattern])."
}

$cert | ConvertTo-Json -Depth 3
