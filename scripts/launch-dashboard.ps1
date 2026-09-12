param(
  [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dashboardUrl = 'http://127.0.0.1:5173'
$requiredPorts = @(3306, 1883, 8080, 8090, 5173)

function Test-ListeningPort([int]$Port) {
  return [bool](Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

$missingPorts = @($requiredPorts | Where-Object { -not (Test-ListeningPort $_) })
if ($missingPorts.Count -gt 0) {
  Write-Host "Starting Industrial IoT services. Missing ports: $($missingPorts -join ', ')"
  & "$PSScriptRoot\start-all.ps1"
  if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
    throw "Service startup failed with exit code $LASTEXITCODE"
  }
}
else {
  Write-Host 'Industrial IoT services are already running.'
}

$ready = $false
for ($attempt = 1; $attempt -le 30; $attempt++) {
  try {
    $response = Invoke-WebRequest -Uri $dashboardUrl -UseBasicParsing -TimeoutSec 2
    if ($response.StatusCode -eq 200) {
      $ready = $true
      break
    }
  }
  catch {
    Start-Sleep -Seconds 1
  }
}

if (-not $ready) {
  Write-Warning "Dashboard is not responding yet. Try opening $dashboardUrl manually."
}
elseif (-not $NoBrowser) {
  Start-Process $dashboardUrl
  Write-Host "Dashboard opened: $dashboardUrl"
}

Write-Host "Project root: $root"
