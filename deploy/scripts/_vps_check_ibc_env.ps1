$ErrorActionPreference = "Continue"
Write-Host "--- IBC env var probe ---"
Write-Host "user: $env:USERNAME"
Write-Host "host: $env:COMPUTERNAME"
Write-Host ""

$names = @("ETA_IBC_LOGIN_ID", "ETA_IBC_PASSWORD", "IBKR_PASSWORD", "TWS_PASSWORD", "IBKR_USERNAME")
foreach ($n in $names) {
    $mach = [System.Environment]::GetEnvironmentVariable($n, "Machine")
    $user = [System.Environment]::GetEnvironmentVariable($n, "User")
    $proc = [System.Environment]::GetEnvironmentVariable($n, "Process")
    if ($mach) { $machDesc = "<" + $mach.Length + " chars>" } else { $machDesc = "unset" }
    if ($user) { $userDesc = "<" + $user.Length + " chars>" } else { $userDesc = "unset" }
    if ($proc) { $procDesc = "<" + $proc.Length + " chars>" } else { $procDesc = "unset" }
    Write-Host ("{0,-22} machine={1,-12} user={2,-12} process={3}" -f $n, $machDesc, $userDesc, $procDesc)
}
Write-Host ""

Write-Host "--- IBC config files (could contain creds) ---"
$ibcRoot = "C:\IBC"
if (Test-Path $ibcRoot) {
    Get-ChildItem $ibcRoot -Filter "*.ini" -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host "  $($_.FullName) ($($_.Length) bytes)"
    }
} else {
    Write-Host "  C:\IBC not present"
}

$jtsRoot = "C:\Jts"
if (Test-Path $jtsRoot) {
    Get-ChildItem $jtsRoot -Filter "jts.ini" -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host "  $($_.FullName) ($($_.Length) bytes)"
    }
}
Write-Host ""

Write-Host "--- All scheduled task ETA-IBGateway* command lines ---"
foreach ($name in @("ETA-IBGateway", "ETA-IBGateway-DailyRestart")) {
    $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $t) { continue }
    Write-Host "TASK: $name"
    foreach ($a in $t.Actions) {
        Write-Host "  Args: $($a.Arguments)"
    }
}
Write-Host ""

Write-Host "--- Password file candidates ---"
$candidates = @(
    "C:\EvolutionaryTradingAlgo\var\eta_engine\state\ibkr_pw.txt",
    "C:\EvolutionaryTradingAlgo\eta_engine\secrets\ibkr_pw.txt",
    "C:\Users\trader\ibkr_pw.txt",
    "C:\Jts\ibkr_pw.txt",
    "C:\IBC\ibkr_pw.txt"
)
foreach ($c in $candidates) {
    Write-Host ("  {0} : exists={1}" -f $c, (Test-Path $c))
}
