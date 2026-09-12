$root = Split-Path -Parent $PSScriptRoot
$currentPid = $PID
Write-Host 'Disconnect test: stop the simulator for 35 seconds, then restart it.'
Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $currentPid -and $_.CommandLine -match 'sim_devices.main' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 35
$env:PYTHONPATH = "$root\middleware\src;$root\simulated-devices\src"
Start-Process python -ArgumentList '-m','sim_devices.main','--scenario','normal' -WorkingDirectory "$root" -WindowStyle Minimized
Write-Host 'Inspect logs/middleware.log and the dashboard for OFFLINE -> ONLINE.'
