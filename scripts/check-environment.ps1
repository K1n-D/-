$ErrorActionPreference = 'Continue'
Write-Host '=== Industrial IoT local environment ==='
foreach ($cmd in @('python','node','npm','java','mvn','mysql','mosquitto')) {
  $found = Get-Command $cmd -ErrorAction SilentlyContinue
  if ($found) { Write-Host "[OK] $cmd -> $($found.Source)" -ForegroundColor Green } else { Write-Host "[MISSING] $cmd" -ForegroundColor Yellow }
}
$root = Split-Path -Parent $PSScriptRoot
$portable = @{
  'portable-jdk' = "$root\work\tools\jdk17\jdk-17.0.20.1+1\bin\java.exe"
  'portable-maven' = "$root\work\tools\maven-real\apache-maven-3.9.9\bin\mvn.cmd"
  'portable-mysql' = "$root\work\tools\mysql\mysql-8.4.6-winx64\bin\mysql.exe"
}
foreach ($item in $portable.GetEnumerator()) {
  if (Test-Path $item.Value) { Write-Host "[OK] $($item.Key) -> $($item.Value)" -ForegroundColor Green } else { Write-Host "[MISSING] $($item.Key)" -ForegroundColor Yellow }
}
Write-Host 'Project services: MySQL 3306, MQTT 1883, backend 8080, middleware health 8090, frontend 5173.'
Write-Host 'Python fallback remains available when Java or external Mosquitto are missing.'
