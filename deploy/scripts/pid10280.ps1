Get-CimInstance Win32_Process -Filter "ProcessId=10280" | Select ProcessId,ProcessName,CommandLine
