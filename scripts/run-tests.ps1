$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = "$root\middleware\src;$root\simulated-devices\src"
Write-Host '== Python unit tests =='
python -m unittest discover -s "$root\middleware\tests" -p 'test_*.py' -v
python -m unittest discover -s "$root\simulated-devices\tests" -p 'test_*.py' -v
Write-Host '== Python syntax =='
python -m compileall -q "$root\middleware\src" "$root\simulated-devices\src" "$root\frontend-backend\backend"
Write-Host '== MQTT heartbeat probe =='
@'
import time
from iot_middleware.local_mqtt import Client
seen=[]
c=Client('test-runner',keepalive=2,on_control=lambda x: seen.append(x)); c.connect(); c.start_keepalive(); time.sleep(3.5); c.disconnect()
assert seen.count('PINGRESP') >= 1, seen
print('PINGRESP_COUNT=', seen.count('PINGRESP'))
'@ | python -
Write-Host '== MQTT 3.1.1 Paho interoperability =='
& "$PSScriptRoot\test-mqtt-interop.ps1"
Write-Host '== MySQL connectivity and schema =='
$mysql = Join-Path $root 'work\tools\mysql\mysql-8.4.6-winx64\bin\mysql.exe'
if (Test-Path $mysql) {
  & "$PSScriptRoot\start-mysql.ps1"
  & $mysql --protocol=tcp --host=127.0.0.1 --port=3306 -uroot -proot1234 -e "SELECT VERSION() AS version, @@port AS port;"
  & $mysql --protocol=tcp --host=127.0.0.1 --port=3306 -uroot -proot1234 -D iot_monitor -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='iot_monitor';" | ForEach-Object { if ([int]$_ -lt 5) { throw 'iot_monitor schema is incomplete' } else { Write-Host "iot_monitor tables=$_" } }
} else { Write-Warning 'Portable MySQL client not found; database check skipped.' }
$mvn = Join-Path $root 'work\tools\maven-real\apache-maven-3.9.9\bin\mvn.cmd'
if (Test-Path $mvn) {
  Write-Host '== Java Maven tests =='
  Push-Location "$root\frontend-backend\backend"
  & $mvn test -q
  Pop-Location
}
Write-Host 'All available tests passed.' -ForegroundColor Green
