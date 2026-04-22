$ErrorActionPreference = "Stop"

$wacs = "C:\tools\win-acme\wacs.exe"
if (-not (Test-Path $wacs)) {
    throw "win-acme not found at $wacs"
}

& $wacs `
    --source manual `
    --host "teamsbot.qmachina.com,media.qmachina.com" `
    --validation selfhosting `
    --validationmode http-01 `
    --store certificatestore `
    --certificatestore My `
    --acl-read "azureuser" `
    --friendlyname "qmachina-teamsbot-media" `
    --emailaddress "logan@qmachina.com" `
    --accepttos `
    --closeonfinish

$certs = Get-ChildItem Cert:\LocalMachine\My |
    Where-Object { $_.Subject -match "teamsbot\\.qmachina\\.com|media\\.qmachina\\.com" } |
    Sort-Object NotAfter -Descending |
    Select-Object -First 5 Subject, Thumbprint, NotAfter

$certs | ConvertTo-Json
