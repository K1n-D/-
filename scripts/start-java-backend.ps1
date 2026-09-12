$root = Split-Path -Parent $PSScriptRoot
$java = Join-Path $root 'work\tools\jdk17\jdk-17.0.20.1+1\bin\java.exe'
$jar = Join-Path $root 'frontend-backend\backend\target\industrial-iot-backend-0.1.0.jar'
if (-not (Test-Path $java)) { $java = 'java' }
if (-not (Test-Path $jar)) { throw "Build the backend first with Maven." }
Write-Host 'Starting Spring Boot backend on port 8081 (Java service).'
& $java -jar $jar --server.port=8081 --server.address=127.0.0.1
