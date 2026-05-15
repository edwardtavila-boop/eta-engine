[Environment]::SetEnvironmentVariable("ETA_SUPERVISOR_MODE", "paper_live", "Machine")
[Environment]::SetEnvironmentVariable("ETA_SUPERVISOR_FEED", "composite", "Machine")
[Environment]::SetEnvironmentVariable("ETA_PAPER_LIVE_ORDER_ROUTE", "broker_router", "Machine")
[Environment]::SetEnvironmentVariable("ETA_PAPER_LIVE_ALLOWED_SYMBOLS", "MNQ,MNQ1,NQ,NQ1,ES,ES1,MES,MES1,RTY,RTY1,M2K,M2K1,MYM,MYM1,YM,YM1,GC,GC1,MGC,MGC1,CL,CL1,MCL,MCL1,NG,NG1,ZN,ZN1,6E,6E1,M6E,M6E1", "Machine")
[Environment]::SetEnvironmentVariable("ETA_SUPERVISOR_STARTING_CASH", "50000", "Machine")
[Environment]::SetEnvironmentVariable("ETA_PAPER_LIVE_KILLSWITCH_MODE", "advisory", "Machine")
Write-Host "Set ETA_SUPERVISOR_MODE=paper_live (Machine)"
Write-Host "Set ETA_SUPERVISOR_FEED=composite (Machine)"
Write-Host "Set ETA_PAPER_LIVE_ORDER_ROUTE=broker_router (Machine)"
Write-Host "Set ETA_PAPER_LIVE_ALLOWED_SYMBOLS=paper-live futures allowlist (Machine)"
Write-Host "Set ETA_SUPERVISOR_STARTING_CASH=50000 (Machine)"
Write-Host "Set ETA_PAPER_LIVE_KILLSWITCH_MODE=advisory (Machine)"
