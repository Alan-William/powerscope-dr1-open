param([int]$Port=18767, [switch]$NoBrowser, [switch]$ConnectBoard)
$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent $PSScriptRoot
$runtime=Join-Path $projectRoot 'artifacts/live-sim-runtime'
$source=Join-Path $projectRoot 'pc_monitor/native/npc_core.c'
$buildManifest=Join-Path $runtime 'build.json'
$needsBuild=$true
if (Test-Path -LiteralPath $buildManifest) {
    $build=Get-Content -LiteralPath $buildManifest -Raw | ConvertFrom-Json
    $binary=Join-Path $runtime 'npc_core.dll'
    if (Test-Path -LiteralPath $binary) {
        $needsBuild=($build.source_sha256 -ne (Get-FileHash -LiteralPath $source).Hash.ToLower()) -or ($build.dll_sha256 -ne (Get-FileHash -LiteralPath $binary).Hash.ToLower())
    }
}
if ($needsBuild) { & (Join-Path $PSScriptRoot 'build_live_sim.ps1') }
$base='http://127.0.0.1:'+$Port
try { $state=Invoke-RestMethod -Uri ($base+'/api/state') -TimeoutSec 2 } catch { $state=$null }
if ($state -and $state.identity -ne 'powerscope-rs485-monitor') { throw 'Port is in use by another application.' }
if (-not $state) {
    $localPython='C:/Python314/pythonw.exe'
    if (-not (Test-Path -LiteralPath $localPython)) { $localPython=(Get-Command python.exe).Source }
    $logDir=Join-Path $projectRoot 'artifacts/live-sim-20261103'
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $stamp=Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $process=Start-Process -FilePath $localPython -ArgumentList @('-X','utf8','-m','pc_monitor','--port',$Port) -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDir ('server-'+$stamp+'.stdout.log')) -RedirectStandardError (Join-Path $logDir ('server-'+$stamp+'.stderr.log'))
    $process.Id | Set-Content -LiteralPath (Join-Path $logDir 'server.pid')
    $ready=$false
    for($attempt=0;$attempt -lt 30;$attempt++) {
        Start-Sleep -Milliseconds 200
        if($process.HasExited) { throw 'Local simulation service exited. Inspect the runtime logs.' }
        try { $simState=Invoke-RestMethod -Uri ($base+'/api/simulation/state') -TimeoutSec 1; $ready=$true; break } catch { }
    }
    if(-not $ready) { throw 'Local simulation service did not become ready.' }
}
if($ConnectBoard) { & (Join-Path $PSScriptRoot 'start_rs485_monitor.ps1') -Port $Port -NoBrowser }
if(-not $NoBrowser) { Start-Process ($base+'/simulation') }
Write-Output ($base+'/simulation')
