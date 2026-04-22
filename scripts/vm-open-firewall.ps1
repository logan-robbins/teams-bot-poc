$ErrorActionPreference = "Stop"

foreach ($port in 80, 443, 8445) {
    $name = "Allow TeamsBot Port $port"
    if (-not (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule `
            -DisplayName $name `
            -Direction Inbound `
            -Action Allow `
            -Protocol TCP `
            -LocalPort $port `
            -Profile Any | Out-Null
    }
}

Get-NetFirewallRule |
    Where-Object DisplayName -like "Allow TeamsBot Port *" |
    Get-NetFirewallPortFilter |
    Select-Object Protocol, LocalPort |
    Sort-Object LocalPort |
    ConvertTo-Json -Depth 3
