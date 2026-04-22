$ErrorActionPreference = "Stop"

$choco = "C:\ProgramData\chocolatey\bin\choco.exe"
if (-not (Test-Path $choco)) {
    throw "Chocolatey not found at $choco"
}

& $choco install win-acme -y --no-progress

$commands = @(
    (Get-Command wacs.exe -ErrorAction SilentlyContinue),
    (Get-Command wacs -ErrorAction SilentlyContinue)
) | Where-Object { $null -ne $_ } | Select-Object -First 1

if ($null -eq $commands) {
    throw "win-acme installed but wacs command was not found on PATH."
}

[pscustomobject]@{
    WacsPath = $commands.Source
} | ConvertTo-Json
