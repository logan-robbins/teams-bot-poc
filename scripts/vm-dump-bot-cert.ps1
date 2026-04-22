$ErrorActionPreference = "Stop"

$cert = Get-ChildItem Cert:\LocalMachine\My |
    Where-Object {
        $_.Subject -match "CN=teamsbot\.qmachina\.com" -or
        $_.FriendlyName -like "qmachina-teamsbot-media*"
    } |
    Sort-Object NotAfter -Descending |
    Select-Object -First 1 Subject, Thumbprint, NotBefore, NotAfter, FriendlyName, HasPrivateKey

if (-not $cert) {
    throw "No bot certificate found in Cert:\LocalMachine\My."
}

$cert | ConvertTo-Json -Depth 3
