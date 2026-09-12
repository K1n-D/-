param(
  [int]$Port = 3306
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$asciiRoot = 'C:\iot-monitor'
$mysqlHome = Join-Path $asciiRoot 'work\tools\mysql\mysql-8.4.6-winx64'
$dataDir = Join-Path $asciiRoot 'work\mysql-runtime'
$iniPath = Join-Path $asciiRoot 'work\mysql.ini'
$logDir = Join-Path $root 'logs'

$installedService = Get-Service -Name MySQL84 -ErrorAction SilentlyContinue
if ($installedService) {
  if ($installedService.Status -ne 'Running') {
    Start-Service -Name MySQL84
    $installedService.WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
  }
  Write-Host 'MySQL84 Windows service is running on 127.0.0.1:3306.'
  exit 0
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# MySQL 8 on Windows can fail to resolve plugin paths under non-ASCII folders.
# Keep the configuration paths ASCII while pointing the junction at this project.
if (-not (Test-Path $asciiRoot)) {
  New-Item -ItemType Junction -Path $asciiRoot -Target $root | Out-Null
}
else {
  $link = Get-Item $asciiRoot -ErrorAction Stop
  if ($link.LinkType -ne 'Junction' -or (($link.Target | Select-Object -First 1) -ne $root)) {
    throw "$asciiRoot exists but is not the project junction. Remove it or point it to $root."
  }
}

if (-not (Test-Path "$mysqlHome\bin\mysqld.exe")) { throw "MySQL binary not found: $mysqlHome\bin\mysqld.exe" }
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existing) { Write-Host "MySQL is already listening on 127.0.0.1:$Port (PID $($existing[0].OwningProcess))"; exit 0 }

$mysqlArgs = @("--defaults-file=$iniPath")

if (-not (Test-Path "$dataDir\mysql")) {
  Write-Host 'Initializing project-local MySQL data directory ...'
  & "$mysqlHome\bin\mysqld.exe" @mysqlArgs --initialize-insecure --console
  if ($LASTEXITCODE -ne 0) { throw "MySQL initialization failed with exit code $LASTEXITCODE" }
}

$stdout = Join-Path $logDir 'mysql.log'
$stderr = Join-Path $logDir 'mysql-error.log'
Start-Process -FilePath "$mysqlHome\bin\mysqld.exe" -ArgumentList ($mysqlArgs + '--console') -WorkingDirectory "$mysqlHome\bin" -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden | Out-Null
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Milliseconds 500
  if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "MySQL 8.4.6 is listening on 127.0.0.1:$Port"
    exit 0
  }
}
if (Test-Path $stderr) { Get-Content $stderr -Tail 80 }
if (Test-Path $stdout) { Get-Content $stdout -Tail 80 }
throw "MySQL did not start on port $Port"
