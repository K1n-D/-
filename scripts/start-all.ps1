$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = "$root\middleware\src;$root\simulated-devices\src"
New-Item -ItemType Directory -Force -Path "$root\logs" | Out-Null
New-Item -ItemType Directory -Force -Path "$root\work\pids" | Out-Null
& "$PSScriptRoot\start-mysql.ps1"
& "$PSScriptRoot\start-mqtt.ps1"
Start-Sleep -Milliseconds 500

function Test-ListeningPort([int]$Port) {
  return [bool](Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Test-RunningCommand([string]$Pattern) {
  return [bool](Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match $Pattern })
}

# Record the spawned PID so stop-all.ps1 can stop exactly this process later
# instead of pattern-matching unrelated python processes.
function Save-Pid([string]$Name, $Process) {
  if ($Process -and $Process.Id) { $Process.Id | Set-Content "$root\work\pids\$Name.pid" }
}

if (-not (Test-ListeningPort 8080)) {
  $p = Start-Process python -ArgumentList 'server.py' -WorkingDirectory "$root\frontend-backend\backend" -RedirectStandardOutput "$root\logs\backend.log" -RedirectStandardError "$root\logs\backend-error.log" -WindowStyle Minimized -PassThru
  Save-Pid 'backend' $p
}
else {
  Write-Host 'Backend is already listening on 127.0.0.1:8080.'
}

if (-not (Test-ListeningPort 8090) -and -not (Test-RunningCommand 'iot_middleware\.main')) {
  $p = Start-Process python -ArgumentList '-m','iot_middleware.main' -WorkingDirectory "$root" -RedirectStandardOutput "$root\logs\middleware.log" -RedirectStandardError "$root\logs\middleware-error.log" -WindowStyle Minimized -PassThru
  Save-Pid 'middleware' $p
}
else {
  Write-Host 'Middleware is already running.'
}

Start-Sleep -Seconds 1
if (-not (Test-RunningCommand 'sim_devices\.main')) {
  $p = Start-Process python -ArgumentList '-m','sim_devices.main','--all','--scenario','normal' -WorkingDirectory "$root" -RedirectStandardOutput "$root\logs\devices.log" -RedirectStandardError "$root\logs\devices-error.log" -WindowStyle Minimized -PassThru
  Save-Pid 'devices' $p
}
else {
  Write-Host 'Simulated devices are already running.'
}

if (Test-Path "$root\frontend-backend\frontend\node_modules\.bin\vite.cmd") {
  if (-not (Test-ListeningPort 5173)) {
    $p = Start-Process npm.cmd -ArgumentList 'run','dev','--','--host','127.0.0.1','--port','5173' -WorkingDirectory "$root\frontend-backend\frontend" -RedirectStandardOutput "$root\logs\frontend.log" -RedirectStandardError "$root\logs\frontend-error.log" -WindowStyle Minimized -PassThru
    Save-Pid 'frontend' $p
  }
  else {
    Write-Host 'Frontend is already listening on 127.0.0.1:5173.'
  }
} else {
  if (-not (Test-ListeningPort 5173)) {
    $p = Start-Process python -ArgumentList '-m','http.server','5173','--directory',"$root\frontend-backend\frontend" -WorkingDirectory "$root" -RedirectStandardOutput "$root\logs\frontend.log" -RedirectStandardError "$root\logs\frontend-error.log" -WindowStyle Minimized -PassThru
    Save-Pid 'frontend' $p
  }
  else {
    Write-Host 'Frontend is already listening on 127.0.0.1:5173.'
  }
}
if (-not (Test-ListeningPort 5174)) {
  $p = Start-Process python -ArgumentList '-m','http.server','5174','--directory',"$root\simulated-devices\frontend" -WorkingDirectory "$root" -RedirectStandardOutput "$root\logs\simulator-frontend.log" -RedirectStandardError "$root\logs\simulator-frontend-error.log" -WindowStyle Minimized -PassThru
  Save-Pid 'simulator-frontend' $p
}
else {
  Write-Host 'Simulator frontend is already listening on 127.0.0.1:5174.'
}
Write-Host 'Started: dashboard http://127.0.0.1:5173, simulator frontend http://127.0.0.1:5174, backend http://127.0.0.1:8080, middleware health http://127.0.0.1:8090/health, device control http://127.0.0.1:8091/api/devices'
