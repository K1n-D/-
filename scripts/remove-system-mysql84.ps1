$ErrorActionPreference = 'Stop'

$serviceName = 'MySQL84'
$installDir = 'C:\Program Files\MySQL\MySQL Server 8.4'
$dataDir = 'C:\ProgramData\MySQL\MySQL Server 8.4'

$service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
if ($service) {
  if ($service.Status -ne 'Stopped') {
    Stop-Service -Name $serviceName -Force
    $service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(20))
  }
  & sc.exe delete $serviceName | Out-Host
  if ($LASTEXITCODE -ne 0) { throw "Unable to delete Windows service $serviceName" }
}

Get-Process mysqld -ErrorAction SilentlyContinue | Where-Object {
  $_.Path -like "$installDir*"
} | Stop-Process -Force -ErrorAction SilentlyContinue

foreach ($path in @($installDir, $dataDir)) {
  if (Test-Path -LiteralPath $path) {
    Remove-Item -LiteralPath $path -Recurse -Force
    Write-Host "Removed $path"
  }
}

Write-Host 'System MySQL84 removal completed.'
