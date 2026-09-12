$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = "$root\middleware\src;$root\simulated-devices\src"
New-Item -ItemType Directory -Force -Path "$root\logs" | Out-Null
$existing = Get-NetTCPConnection -LocalPort 1883 -State Listen -ErrorAction SilentlyContinue
if ($existing) { Write-Host "MQTT Broker is already listening on 127.0.0.1:1883 (PID $($existing[0].OwningProcess))"; exit 0 }
$proc = Start-Process python -ArgumentList '-m','iot_middleware.broker' -WorkingDirectory $root -RedirectStandardOutput "$root\logs\mqtt.log" -RedirectStandardError "$root\logs\mqtt-error.log" -WindowStyle Hidden -PassThru
New-Item -ItemType Directory -Force -Path "$root\work\pids" | Out-Null
$proc.Id | Set-Content "$root\work\pids\mqtt.pid"
for ($i = 0; $i -lt 20; $i++) {
  Start-Sleep -Milliseconds 250
  if (Get-NetTCPConnection -LocalPort 1883 -State Listen -ErrorAction SilentlyContinue) { Write-Host 'MQTT Broker is listening on 127.0.0.1:1883'; exit 0 }
}
throw 'MQTT Broker did not start on port 1883'
