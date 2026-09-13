$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
# Append, never overwrite: start-all.ps1 may already include edge-collector\src.
$env:PYTHONPATH = "$root\middleware\src;$root\simulated-devices\src;$root\edge-collector\src;$env:PYTHONPATH"
New-Item -ItemType Directory -Force -Path "$root\logs" | Out-Null
$existing = Get-NetTCPConnection -LocalPort 1883 -State Listen -ErrorAction SilentlyContinue
if ($existing) { Write-Host "MQTT Broker is already listening on 127.0.0.1:1883 (PID $($existing[0].OwningProcess))"; exit 0 }
# Broker selection: drop a portable mosquitto.exe into work\tools\mosquitto to
# run the production broker instead of the in-repo teaching implementation.
# All clients speak standard MQTT 3.1.1, so the swap is transparent.
$mosquittoExe = Join-Path $root 'work\tools\mosquitto\mosquitto.exe'
if (Test-Path $mosquittoExe) {
  New-Item -ItemType Directory -Force -Path "$root\work\mosquitto" | Out-Null
  "listener 1883 127.0.0.1`r`nallow_anonymous true`r`npersistence false" | Set-Content "$root\work\mosquitto\mosquitto.conf" -Encoding Ascii
  $proc = Start-Process $mosquittoExe -ArgumentList '-c',"`"$root\work\mosquitto\mosquitto.conf`"" -WorkingDirectory "$root\work\mosquitto" -RedirectStandardOutput "$root\logs\mqtt.log" -RedirectStandardError "$root\logs\mqtt-error.log" -WindowStyle Hidden -PassThru
} else {
  $proc = Start-Process python -ArgumentList '-m','iot_middleware.broker' -WorkingDirectory $root -RedirectStandardOutput "$root\logs\mqtt.log" -RedirectStandardError "$root\logs\mqtt-error.log" -WindowStyle Hidden -PassThru
}
New-Item -ItemType Directory -Force -Path "$root\work\pids" | Out-Null
$proc.Id | Set-Content "$root\work\pids\mqtt.pid"
for ($i = 0; $i -lt 20; $i++) {
  Start-Sleep -Milliseconds 250
  if (Get-NetTCPConnection -LocalPort 1883 -State Listen -ErrorAction SilentlyContinue) { Write-Host 'MQTT Broker is listening on 127.0.0.1:1883'; exit 0 }
}
throw 'MQTT Broker did not start on port 1883'
