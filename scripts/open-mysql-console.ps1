$ErrorActionPreference = 'Stop'
$mysql = 'C:\Program Files\MySQL\MySQL Server 8.4\bin\mysql.exe'

if (-not (Test-Path -LiteralPath $mysql)) {
  Write-Host "MySQL client not found: $mysql" -ForegroundColor Red
  Read-Host 'Press Enter to close'
  exit 1
}

$service = Get-Service -Name MySQL84 -ErrorAction SilentlyContinue
if ($service -and $service.Status -ne 'Running') {
  try {
    Start-Service -Name MySQL84 -ErrorAction Stop
    $service.WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
  }
  catch {
    Write-Host 'Unable to start MySQL84 automatically. Please run this shortcut as Administrator.' -ForegroundColor Yellow
  }
}

Write-Host 'Connecting to MySQL 8.4.6 / iot_monitor on 127.0.0.1:3306 ...' -ForegroundColor Cyan
Write-Host 'User: iot_navicat   Password will be requested at the prompt.' -ForegroundColor DarkGray
& $mysql --protocol=tcp --host=127.0.0.1 --port=3306 --user=iot_navicat --password --database=iot_monitor
if ($LASTEXITCODE -ne 0) {
  Write-Host "MySQL exited with code $LASTEXITCODE" -ForegroundColor Red
  Read-Host 'Press Enter to close'
}
