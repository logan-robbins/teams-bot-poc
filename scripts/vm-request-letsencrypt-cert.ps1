param(
    [string]$RunAsUser = "azureuser",
    [string]$Hostnames = "alfred-disney-bot.eastus.cloudapp.azure.com",
    [string]$EmailAddress = "Logan.Robbins@disney.com",
    [string]$FriendlyName = "alfred-disney-cert"
)

$ErrorActionPreference = "Stop"

$env:Path = "C:\tools\win-acme;C:\ProgramData\chocolatey\bin;" +
    [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
    [System.Environment]::GetEnvironmentVariable("Path", "User")

$wacsCommand = @(
    (Get-Command wacs.exe -ErrorAction SilentlyContinue),
    (Get-Command wacs -ErrorAction SilentlyContinue),
    (Get-Item "C:\tools\win-acme\wacs.exe" -ErrorAction SilentlyContinue),
    (Get-ChildItem "C:\ProgramData\chocolatey\lib\win-acme" -Recurse -Filter "wacs.exe" -ErrorAction SilentlyContinue | Select-Object -First 1)
) | Where-Object { $null -ne $_ } | Select-Object -First 1

if ($null -eq $wacsCommand) {
    throw "win-acme not found. Run vm-install-win-acme.ps1 first."
}

$wacs = if ($wacsCommand.Source) { $wacsCommand.Source } else { $wacsCommand.FullName }

$uniqueHosts = ($Hostnames -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ } | Select-Object -Unique) -join ","
$primaryHost = ($uniqueHosts -split ",")[0]

& $wacs `
    --source manual `
    --host $uniqueHosts `
    --validation selfhosting `
    --validationmode http-01 `
    --store certificatestore `
    --certificatestore My `
    --acl-read $RunAsUser `
    --friendlyname $FriendlyName `
    --emailaddress $EmailAddress `
    --accepttos `
    --closeonfinish

$hostPattern = ($uniqueHosts -split "," | ForEach-Object { [regex]::Escape($_) }) -join "|"
$certs = Get-ChildItem Cert:\LocalMachine\My |
    Where-Object { $_.Subject -match $hostPattern } |
    Sort-Object NotAfter -Descending |
    Select-Object -First 5 Subject, Thumbprint, NotAfter

$certs | ConvertTo-Json
