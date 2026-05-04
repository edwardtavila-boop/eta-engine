Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {$_.CommandLine -match 'jarvis_live'} | ForEach-Object {Stop-Process -Id $_.ProcessId -Force}
Write-Host "Jarvis process killed"
Start-Sleep 5
schtasks /run /tn ETA-Jarvis-Live
Write-Host "Task restarted"
