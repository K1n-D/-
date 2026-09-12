$currentPid = $PID
Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $currentPid -and $_.CommandLine -match 'iot_middleware.broker|iot_middleware.main|sim_devices.main|http.server.*(5173|5174)|server.py|vite.*5173|npm.*run.*dev' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
$mysqlService = Get-Service -Name MySQL84 -ErrorAction SilentlyContinue
if ($mysqlService) {
  if ($mysqlService.Status -ne 'Stopped') { Stop-Service -Name MySQL84 -Force -ErrorAction SilentlyContinue }
} else {
  $mysqlConnection = Get-NetTCPConnection -LocalPort 3306 -State Listen -ErrorAction SilentlyContinue
  if ($mysqlConnection) { Stop-Process -Id $mysqlConnection[0].OwningProcess -Force -ErrorAction SilentlyContinue }
}
Write-Host 'Stopped local Industrial IoT processes.'
