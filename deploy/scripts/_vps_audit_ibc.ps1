$ErrorActionPreference = "Continue"
Write-Host "--- VPS audit ---"
Write-Host "hostname: $env:COMPUTERNAME"
Write-Host "user: $env:USERNAME"
Write-Host "date: $(Get-Date -Format o)"
Write-Host ""
Write-Host "--- IBC install state ---"
$ibcState = "C:\EvolutionaryTradingAlgo\var\eta_engine\state\ibc_install.json"
if (Test-Path $ibcState) {
    $j = Get-Content -Raw $ibcState | ConvertFrom-Json
    Write-Host "  installed: $($j.installed)"
    Write-Host "  current_install_dir: $($j.current_install_dir)"
    Write-Host "  start_ibc_path: $($j.start_ibc_path)"
} else {
    Write-Host "  NOT INSTALLED (no state file)"
}
Write-Host ""
Write-Host "--- IBKR credentials JSON ---"
$credPath = "C:\EvolutionaryTradingAlgo\eta_engine\secrets\ibkr_credentials.json"
Write-Host "  exists: $(Test-Path $credPath)"
Write-Host ""
Write-Host "--- Password file ---"
$pwFile = "C:\EvolutionaryTradingAlgo\var\eta_engine\state\ibkr_pw.txt"
Write-Host "  exists: $(Test-Path $pwFile)"
if (Test-Path $pwFile) {
    Write-Host "  size: $((Get-Item $pwFile).Length) bytes"
    Write-Host "  ACL:"
    (Get-Acl $pwFile).Access | ForEach-Object { Write-Host "    $($_.IdentityReference) $($_.AccessControlType) $($_.FileSystemRights)" }
}
Write-Host ""
Write-Host "--- Existing ETA-* scheduled tasks ---"
Get-ScheduledTask -TaskName ETA-* -ErrorAction SilentlyContinue | Select-Object TaskName, State | Format-Table -AutoSize | Out-String | Write-Host
Write-Host ""
Write-Host "--- IB Gateway install dirs ---"
if (Test-Path "C:\Jts\ibgateway") {
    Get-ChildItem -Directory "C:\Jts\ibgateway" | ForEach-Object { Write-Host "  $($_.Name)" }
} else {
    Write-Host "  NO Jts\ibgateway"
}
Write-Host ""
Write-Host "--- TWS Gateway port probe ---"
foreach ($port in 4002, 7497, 4001) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $task = $client.ConnectAsync("127.0.0.1", $port)
        if ($task.Wait(1500) -and $client.Connected) {
            Write-Host "  $port : OPEN"
        } else {
            Write-Host "  $port : closed"
        }
        $client.Close()
    } catch { Write-Host "  $port : closed ($_)" }
}
Write-Host ""
Write-Host "--- ibgateway / java processes ---"
Get-Process -Name ibgateway, java -ErrorAction SilentlyContinue | Select-Object Name, Id, StartTime | Format-Table -AutoSize | Out-String | Write-Host
