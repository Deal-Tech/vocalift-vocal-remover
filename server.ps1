<#
.SYNOPSIS
  Menjalankan backend FastAPI dan frontend React di dua tab Windows Terminal.
.EXAMPLE
  .\server.ps1 -Setup
.EXAMPLE
  .\server.ps1 -Dev -NoBrowser
.EXAMPLE
  .\server.ps1 -Build
.EXAMPLE
  .\server.ps1 -Stop
#>
[CmdletBinding()]
param(
  [switch]$Setup, [switch]$Dev, [switch]$Build, [switch]$Stop, [switch]$NoBrowser,
  [ValidateRange(1,65535)][int]$Port = 8000,
  [ValidateRange(1,65535)][int]$FrontendPort = 5173,
  [string]$BindAddress = '127.0.0.1',
  [ValidateSet('critical','error','warning','info','debug','trace')][string]$LogLevel = 'info',
  [ValidateSet('Backend','Frontend')][string]$Service
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$frontend = Join-Path $root 'frontend'
$python = Join-Path $root '.venv\Scripts\python.exe'
$runtime = Join-Path $root '.runtime'
$vite = Join-Path $frontend 'node_modules\vite\bin\vite.js'
Set-Location -LiteralPath $root

function Invoke-Checked {
  param([string]$Executable, [string[]]$Arguments)
  & $Executable @Arguments
  if ($LASTEXITCODE -ne 0) { throw "$Executable gagal (exit $LASTEXITCODE)." }
}
function Get-PortOwner {
  param([int]$TargetPort)
  @(Get-NetTCPConnection -LocalPort $TargetPort -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
}
function Stop-Services {
  foreach ($name in @('Backend','Frontend')) {
    $recordPath = Join-Path $runtime "$name.json"
    if (-not (Test-Path -LiteralPath $recordPath)) { continue }
    $record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $($record.pid)" -ErrorAction SilentlyContinue
    # PID bisa dipakai ulang: cocokkan path script dan waktu proses dibuat.
    if ($processInfo -and $processInfo.CommandLine -and $processInfo.CommandLine.Contains($PSCommandPath) -and $processInfo.CommandLine.Contains('-Service') -and $processInfo.CreationDate.ToUniversalTime().Ticks.ToString() -eq $record.created) {
      & taskkill.exe /PID $record.pid /T /F | Out-Null
      if ($LASTEXITCODE -ne 0) { throw "Gagal menghentikan $name." }
      Write-Host "$name dihentikan."
    }
    Remove-Item -LiteralPath $recordPath -Force
  }
}
if ($Stop) { Stop-Services; return }

$envFile = Join-Path $root '.env'
if (Test-Path -LiteralPath $envFile) {
  foreach ($line in Get-Content -LiteralPath $envFile) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
    $split = $trimmed.IndexOf('=')
    if ($split -lt 1) { continue }
    $key = $trimmed.Substring(0,$split).Trim()
    $value = $trimmed.Substring($split + 1).Trim().Trim('"').Trim("'")
    if ($null -eq [Environment]::GetEnvironmentVariable($key)) { [Environment]::SetEnvironmentVariable($key,$value) }
  }
}
$node = (Get-Command node.exe -ErrorAction Stop).Source
$nodeVersion = [version]((& $node --version).TrimStart('v'))
if ($nodeVersion.Major -lt 20 -or ($nodeVersion.Major -eq 20 -and $nodeVersion.Minor -lt 19) -or $nodeVersion.Major -eq 21 -or ($nodeVersion.Major -eq 22 -and $nodeVersion.Minor -lt 12)) { throw 'Gunakan Node.js 20.19+ atau 22.12+.' }

if ($Setup) {
  if (-not (Test-Path -LiteralPath $python)) {
    Invoke-Checked (Get-Command python.exe -ErrorAction Stop).Source @('-m','venv',(Join-Path $root '.venv'))
  }
  Invoke-Checked $python @('-m','pip','install','-r',(Join-Path $root 'requirements.txt'))
  # Jalankan npm lewat Node, tanpa wrapper npm.cmd.
  $npmCommand = (Get-Command npm.cmd -ErrorAction Stop).Source
  $npmCli = Join-Path (Split-Path $npmCommand) 'node_modules\npm\bin\npm-cli.js'
  Push-Location -LiteralPath $frontend
  try {
    if (Test-Path -LiteralPath 'package-lock.json') { Invoke-Checked $node @($npmCli,'ci') }
    else { Invoke-Checked $node @($npmCli,'install') }
  } finally { Pop-Location }
  & $python -c 'from app.main import FFMPEG_AVAILABLE; raise SystemExit(0 if FFMPEG_AVAILABLE else 1)'
  if ($LASTEXITCODE -ne 0) {
    Invoke-Checked (Get-Command winget.exe -ErrorAction Stop).Source @('install','--id','Gyan.FFmpeg','--exact','--accept-package-agreements','--accept-source-agreements','--silent','--disable-interactivity')
  }
  Write-Host 'Setup selesai. Jalankan .\server.ps1.' -ForegroundColor Green
  return
}
if (-not (Test-Path -LiteralPath $vite)) { throw 'Dependensi frontend belum tersedia. Jalankan .\server.ps1 -Setup.' }
if ($Build) {
  Push-Location -LiteralPath $frontend
  try { Invoke-Checked $node @($vite,'build') } finally { Pop-Location }
  return
}
if (-not (Test-Path -LiteralPath $python)) { throw 'Environment backend belum tersedia. Jalankan .\server.ps1 -Setup.' }
if ($Port -eq $FrontendPort) { throw 'Port frontend dan backend harus berbeda.' }

if ($Service) {
  New-Item -ItemType Directory -Path $runtime -Force | Out-Null
  $recordPath = Join-Path $runtime "$Service.json"
  $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $PID"
  @{ pid = $PID; created = $processInfo.CreationDate.ToUniversalTime().Ticks.ToString() } | ConvertTo-Json | Set-Content -LiteralPath $recordPath -Encoding utf8
  try {
    if ($Service -eq 'Backend') {
      $backendArgs = @('-m','uvicorn','app.main:app','--host',$BindAddress,'--port',"$Port",'--log-level',$LogLevel)
      if ($Dev) { $backendArgs += @('--reload','--reload-dir',(Join-Path $root 'app')) }
      Write-Host "Backend: http://$BindAddress`:$Port (Ctrl+C untuk berhenti)"
      Invoke-Checked $python $backendArgs
    } else {
      $apiAddress = if ($BindAddress -eq '0.0.0.0') { '127.0.0.1' } elseif ($BindAddress -eq '::') { '[::1]' } elseif ($BindAddress.Contains(':')) { "[$BindAddress]" } else { $BindAddress }
      $env:VOCALIFT_API_URL = "http://$apiAddress`:$Port"
      Set-Location -LiteralPath $frontend
      Write-Host "Frontend React: http://127.0.0.1:$FrontendPort (Ctrl+C untuk berhenti)"
      Invoke-Checked $node @($vite,'--host','127.0.0.1','--port',"$FrontendPort",'--strictPort')
    }
  } finally {
    if (Test-Path -LiteralPath $recordPath) {
      $currentRecord = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
      if ($currentRecord.pid -eq $PID) { Remove-Item -LiteralPath $recordPath -Force }
    }
  }
  return
}
foreach ($targetPort in @($Port,$FrontendPort)) {
  $owners = @(Get-PortOwner $targetPort)
  if ($owners.Count) { throw "Port $targetPort sedang dipakai (PID $($owners -join ', ')). Hentikan server lama atau pilih port lain." }
}
$terminal = (Get-Command wt.exe -ErrorAction Stop).Source
$shellCommand = Get-Command pwsh.exe -ErrorAction SilentlyContinue
$shellExe = if ($shellCommand) { $shellCommand.Source } else { (Get-Command powershell.exe -ErrorAction Stop).Source }
$common = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Port $Port -FrontendPort $FrontendPort -BindAddress `"$BindAddress`" -LogLevel $LogLevel"
if ($Dev) { $common += ' -Dev' }
# PowerShell dipilih eksplisit, terlepas dari profil default Windows Terminal.
$terminalArguments = "-w new new-tab --title `"Vocalift Backend`" -d `"$root`" `"$shellExe`" $common -Service Backend ; new-tab --title `"Vocalift Frontend`" -d `"$frontend`" `"$shellExe`" $common -Service Frontend"
Start-Process -FilePath $terminal -ArgumentList $terminalArguments | Out-Null
$frontendUrl = "http://127.0.0.1:$FrontendPort"
$backendReady = $false
$frontendReady = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
  $backendReady = @(Get-PortOwner $Port).Count -gt 0
  $frontendReady = @(Get-PortOwner $FrontendPort).Count -gt 0
  if ($backendReady -and $frontendReady) { break }
  Start-Sleep -Milliseconds 500
}
if (-not $backendReady -or -not $frontendReady) {
  Stop-Services
  throw 'Server gagal siap. Periksa pesan error di tab Windows Terminal, lalu jalankan kembali.'
}
Write-Host "Frontend: $frontendUrl" -ForegroundColor Green
Write-Host "Backend: http://$BindAddress`:$Port"
Write-Host 'Kedua server berjalan di Windows Terminal. Hentikan dengan .\server.ps1 -Stop.'
if (-not $NoBrowser) { Start-Process $frontendUrl }
