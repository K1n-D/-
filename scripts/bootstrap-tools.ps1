param([switch]$Resume)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$toolRoot = Join-Path $root 'work\tools'
New-Item -ItemType Directory -Force -Path $toolRoot | Out-Null

function Get-Tool($name, $url, $archive, $extractTo) {
  $target = Join-Path $toolRoot $archive
  if ($Resume -and (Test-Path $target)) {
    Write-Host "Resuming $name ..."; curl.exe -L --fail -C - -o $target $url
  } elseif (-not (Test-Path $target)) {
    Write-Host "Downloading $name ..."; curl.exe -L --fail --retry 2 -o $target $url
  } else { Write-Host "$name archive already exists" }
  if ($extractTo) {
    $dir = Join-Path $toolRoot $extractTo
    if (-not (Test-Path $dir)) { Expand-Archive -Force $target $dir }
  }
}

Get-Tool 'Temurin JDK 17' 'https://api.adoptium.net/v3/binary/latest/17/ga/windows/x64/jdk/hotspot/normal/eclipse' 'jdk17.zip' 'jdk17'
Get-Tool 'Apache Maven 3.9.10' 'https://archive.apache.org/dist/maven/maven-3/3.9.10/binaries/apache-maven-3.9.10-bin.zip' 'maven.zip' 'maven'
Get-Tool 'MySQL 8.4.6' 'https://dev.mysql.com/get/Downloads/MySQL-8.4/mysql-8.4.6-winx64.zip' 'mysql.zip' 'mysql'
Write-Host 'Mosquitto is an installer rather than an archive; download it separately when network allows:'
Write-Host 'https://mosquitto.org/files/binary/win64/mosquitto-2.0.22-install-windows-x64.exe'
Write-Host 'Tool bootstrap finished. Use work/tools/*/bin executables without modifying system PATH.'
