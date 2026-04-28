$ErrorActionPreference = "Stop"

$choco = "C:\ProgramData\chocolatey\bin\choco.exe"
if (-not (Test-Path $choco)) {
    throw "Chocolatey not found at $choco"
}

& $choco install win-acme -y --no-progress --execution-timeout=600

$env:Path = "C:\tools\win-acme;C:\ProgramData\chocolatey\bin;" +
    [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
    [System.Environment]::GetEnvironmentVariable("Path", "User")

$command = @(
    (Get-Command wacs.exe -ErrorAction SilentlyContinue),
    (Get-Command wacs -ErrorAction SilentlyContinue),
    (Get-Item "C:\tools\win-acme\wacs.exe" -ErrorAction SilentlyContinue),
    (Get-ChildItem "C:\ProgramData\chocolatey\lib\win-acme" -Recurse -Filter "wacs.exe" -ErrorAction SilentlyContinue | Select-Object -First 1)
) | Where-Object { $null -ne $_ } | Select-Object -First 1

if ($null -eq $command) {
    throw "win-acme installed but wacs command was not found on PATH."
}

[pscustomobject]@{
    WacsPath = if ($command.Source) { $command.Source } else { $command.FullName }
} | ConvertTo-Json
