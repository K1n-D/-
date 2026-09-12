$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $PSScriptRoot
$currentPid = $PID
$pidDir = Join-Path $root 'work\pids'
$pattern = 'iot_middleware\.broker|iot_middleware\.main|sim_devices\.main|edge_collector\.main|edge_collector\.slave|http\.server.*(5173|5174)|server\.py|vite.*5173|npm.*run.*dev'

# 1) Stop exactly the processes start-all.ps1 recorded, so a generic command
#    line such as "server.py" can never match an unrelated python process.
#    The recorded PID is still validated against the pattern above to guard
#    against PID reuse since the pid file was written.
if (Test-Path $pidDir) {
  Get-ChildItem $pidDir -Filter '*.pid' | ForEach-Object {
    $recorded = [int](Get-Content $_.FullName | Select-Object -First 1)
    if ($recorded -and $recorded -ne $currentPid) {
      $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $recorded"
      if ($proc -and $proc.CommandLine -match $pattern) {
        Stop-Process -Id $recorded -Force
        Write-Host "Stopped $($_.BaseName) (PID $recorded)."
      }
    }
    Remove-Item $_.FullName -Force
  }
}

# 2) Fallback for strays the pid files cannot cover: node servers spawned by
#    the npm wrapper (a different PID than npm itself) and processes started
#    before pid files were introduced.
Get-CimInstance Win32_Process | Where-Object {
  $_.ProcessId -ne $currentPid -and $_.CommandLine -match $pattern
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

# 3) MySQL lifecycle is handled by service/port, not by the pattern above.
$mysqlService = Get-Service -Name MySQL84
if ($mysqlService) {
  if ($mysqlService.Status -ne 'Stopped') { Stop-Service -Name MySQL84 -Force }
} else {
  $mysqlConnection = Get-NetTCPConnection -LocalPort 3306 -State Listen
  if ($mysqlConnection) { Stop-Process -Id $mysqlConnection[0].OwningProcess -Force }
}
Write-Host 'Stopped local Industrial IoT processes.'
