$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$sourceHome = Join-Path $root 'work\tools\mysql\mysql-8.4.6-winx64'
$sourceData = Join-Path $root 'work\mysql-runtime'
$installHome = 'C:\Program Files\MySQL\MySQL Server 8.4'
$dataHome = 'C:\ProgramData\MySQL\MySQL Server 8.4\Data'
$configHome = 'C:\ProgramData\MySQL\MySQL Server 8.4'
$configPath = Join-Path $configHome 'my.ini'
$serviceName = 'MySQL84'

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw 'This installer must run elevated as Administrator.'
}
if (-not (Test-Path (Join-Path $sourceHome 'bin\mysqld.exe'))) { throw "Source MySQL not found: $sourceHome" }
if (-not (Test-Path (Join-Path $sourceData 'mysql'))) { throw "Source data directory not found: $sourceData" }

$old = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
if ($old -and $old.Status -ne 'Stopped') { Stop-Service -Name $serviceName -Force; $old.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30)) }
Get-Process mysqld -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

if (Test-Path $installHome) { Remove-Item -LiteralPath $installHome -Recurse -Force }
if (Test-Path $configHome) { Remove-Item -LiteralPath $configHome -Recurse -Force }
New-Item -ItemType Directory -Force -Path $installHome,$dataHome,$configHome | Out-Null
Copy-Item -Path (Join-Path $sourceHome '*') -Destination $installHome -Recurse -Force
Copy-Item -Path (Join-Path $sourceData '*') -Destination $dataHome -Recurse -Force

$ini = @"
[mysqld]
basedir=C:/Program Files/MySQL/MySQL Server 8.4
datadir=C:/ProgramData/MySQL/MySQL Server 8.4/Data
port=3306
bind-address=127.0.0.1
mysqlx=0
character-set-server=utf8mb4
collation-server=utf8mb4_unicode_ci
mysql_native_password=ON
lc-messages-dir=C:/Program Files/MySQL/MySQL Server 8.4/share

[client]
port=3306
default-character-set=utf8mb4
"@
Set-Content -LiteralPath $configPath -Value $ini -Encoding ASCII

$mysqld = Join-Path $installHome 'bin\mysqld.exe'
if ($old) { & sc.exe delete $serviceName | Out-Host; Start-Sleep -Seconds 2 }
& $mysqld --install $serviceName --defaults-file=$configPath | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Failed to register $serviceName (exit code $LASTEXITCODE)" }
Set-Service -Name $serviceName -StartupType Automatic
Start-Service -Name $serviceName
$service = Get-Service -Name $serviceName
$service.WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
Write-Host "Installed MySQL 8.4.6 as Windows service $serviceName on 127.0.0.1:3306"
