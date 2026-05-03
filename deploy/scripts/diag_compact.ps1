$e="SilentlyContinue";$p=0;$f=0;$w=0
function S($l,$o){if($o){Write-Host "  [PASS] $l" -f Green;$global:p++}
elseif($null-eq$o){Write-Host "  [WARN] $l" -f Yellow;$global:w++}
else{Write-Host "  [FAIL] $l" -f Red;$global:f++}}

# 1. PROCESSES
Write-Host "`n=== PROCESSES ===" -f Cyan
$py=@(Get-Process python* -ea $e).Count;S "Python: $py" ($py -gt 2)
$jv=@(Get-Process java* -ea $e).Count;S "Java(IBKR): $jv" ($jv -gt 0)
$cf=@(Get-Process cloudflared* -ea $e).Count;S "Cloudflared: $cf" ($cf -gt 0)
$cd=@(Get-Process caddy* -ea $e).Count;S "Caddy: $cd" ($cd -gt 0)

# 2. TASKS
Write-Host "`n=== SCHEDULED TASKS ===" -f Cyan
$ts=@("ETA-Dashboard","ETA-Jarvis-Live","ETA-Avengers-Fleet","ETA-Dashboard-Live",
"ETA-Executor-DashboardAssemble","ETA-Steward-ShadowTick","ETA-Steward-HealthWatchdog",
"ETA-Reasoner-TwinVerdict","ETA-Hermes-Jarvis-Flush","ApexIbkrGatewayWatchdog",
"ETA-BTC-Fleet","ETA-MNQ-Supervisor","ETA-HealthCheck")
$r=0;$st=0;$m=@()
foreach($t in $ts){$i=schtasks /query /tn $t /fo csv 2>$null|ConvertFrom-Csv -ea $e
if(-not$i){$m+=$t}elseif($i.Status-eq"Ready"){$r++;$st++}elseif($i.Status-eq"Running"){$r++}else{$st++}}
S "Tasks: $r running/$st ready/$($m.Count) missing" ($m.Count -eq 0)
if($m){$m|%{Write-Host "         MISSING: $_" -f Red}}

# 3. SERVICES
Write-Host "`n=== SERVICES ===" -f Cyan
$sv=@("FirmCore","FirmWatchdog","FirmCommandCenter","FirmCommandCenterTunnel","HermesJarvisTelegram")
foreach($s in $sv){$x=Get-Service $s -ea $e;S "$s ($($x.Status))" ($x -and $x.Status -eq "Running")}

# 4. PORTS
Write-Host "`n=== PORTS ===" -f Cyan
foreach($port in @(5000,8000,8420)){
$n=netstat -ano 2>$null|Select-String ":$port .*LISTENING";S "Port $port" ($n -ne $null)}

# 5. IBKR
Write-Host "`n=== IBKR GATEWAY ===" -f Cyan
try{$r=Invoke-RestMethod -Uri "https://127.0.0.1:5000/v1/api/portfolio/accounts" -SkipCertificateCheck -TimeoutSec 10
S "IBKR auth: $($r.Count) account(s)" $true;Write-Host "         $r" -f Gray}catch{S "IBKR gateway (port 5000)" $false}

# 6. DEEPSEEK
Write-Host "`n=== DEEPSEEK KEY ===" -f Cyan
$envPath="C:\EvolutionaryTradingAlgo\eta_engine\.env"
if(Test-Path $envPath){$k=Select-String -Path $envPath -Pattern "DEEPSEEK_API_KEY=(\S{10})"|%{$_.Matches.Groups[1].Value}
S "DeepSeek key: $k..." ($k -ne $null)}else{S ".env not found" $false}

# 7. DASHBOARD API
Write-Host "`n=== DASHBOARD API ===" -f Cyan
try{$a=Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/bot-fleet" -TimeoutSec 10
$c=($a|Get-Member -MemberType NoteProperty).Count
$err=0;foreach($pr in $a.PSObject.Properties){if($pr.Value.status -eq "error"){$err++}}
S "Bot fleet: $c bots, $err errors" ($err -eq 0)}catch{S "Dashboard API (port 8000)" $false}

# 8. HEARTBEAT
Write-Host "`n=== HEARTBEAT ===" -f Cyan
$hp="C:\EvolutionaryTradingAlgo\eta_engine\data\runtime_supervisor_health.json"
if(Test-Path $hp){$hb=Get-Content $hp -Raw|ConvertFrom-Json
$age=[math]::Round(((Get-Date)-[datetime]$hb.last_heartbeat).TotalMinutes,1)
S "Last beat: ${age}m ago" ($age -lt 30)}else{S "Health file missing" $false}

# 9. JARVIS MODE
Write-Host "`n=== JARVIS MODE ===" -f Cyan
$m=Select-String -Path $envPath -Pattern "ETA_MODE=(.+)" 2>$null|%{$_.Matches.Groups[1].Value}
if($m){S "Mode: $m" $true}else{S "ETA_MODE not set" $false}

# 10. DISK
Write-Host "`n=== DISK ===" -f Cyan
$d=Get-PSDrive C;$free=[math]::Round($d.Free/1GB,1);S "Free: ${free}GB" ($free -gt 5)

# 11. LOG WATCH
Write-Host "`n=== RECENT LOGS ===" -f Cyan
$ld="C:\EvolutionaryTradingAlgo\eta_engine\var\logs"
foreach($l in (Get-ChildItem $ld -Filter "*.log" -ea $e|Sort LastWriteTime -Desc|Select -First 3)){
$age=[int]((Get-Date)-$l.LastWriteTime).TotalMinutes;Write-Host "  $($l.Name) (${age}m)" -f $(if($age -lt 60){"Green"}else{"Yellow"})}
if(-not(Test-Path $ld)){S "Log dir missing" $false}

# SUMMARY
Write-Host "`n=== SUMMARY: PASS=$p WARN=$w FAIL=$f ===" -f Cyan
if($f -eq 0 -and $w -le 2){Write-Host "VERDICT: HEALTHY" -f Green}
elseif($f -eq 0){Write-Host "VERDICT: DEGRADED" -f Yellow}
else{Write-Host "VERDICT: UNHEALTHY" -f Red}
