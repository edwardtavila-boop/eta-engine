[Environment]::SetEnvironmentVariable("ETA_SUPERVISOR_MODE", "paper_live", "Machine")
[Environment]::SetEnvironmentVariable("ETA_SUPERVISOR_FEED", "composite", "Machine")
[Environment]::SetEnvironmentVariable("ETA_PAPER_LIVE_ORDER_ROUTE", "direct_ibkr", "Machine")
[Environment]::SetEnvironmentVariable("ETA_PAPER_LIVE_ALLOWED_SYMBOLS", "MNQ,MNQ1", "Machine")
[Environment]::SetEnvironmentVariable("ETA_SUPERVISOR_STARTING_CASH", "50000", "Machine")
Write-Host "Set ETA_SUPERVISOR_MODE=paper_live (Machine)"
Write-Host "Set ETA_SUPERVISOR_FEED=composite (Machine)"
Write-Host "Set ETA_PAPER_LIVE_ORDER_ROUTE=direct_ibkr (Machine)"
Write-Host "Set ETA_PAPER_LIVE_ALLOWED_SYMBOLS=MNQ,MNQ1 (Machine)"
Write-Host "Set ETA_SUPERVISOR_STARTING_CASH=50000 (Machine)"
