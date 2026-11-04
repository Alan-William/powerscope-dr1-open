param(
    [int]$Port=18768,
    [string]$Matlab='D:/Program Files/MATLAB/R2023b/bin/matlab.exe',
    [string]$RuntimeRoot='E:/fpga_workflow_runs/powerscope-simulink-runtime',
    [switch]$ReceiverOnly,
    [switch]$ConnectBoard
)
$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $RuntimeRoot 'build.json'))) {
    throw 'Build the Simulink model first with tools/build_simulink_demo.ps1.'
}
$base='http://127.0.0.1:'+$Port
try { $state=Invoke-RestMethod -Uri ($base+'/api/simulation/state') -TimeoutSec 2 } catch { $state=$null }
if ($state -and $state.engine -ne 'simulink') { throw 'The selected port belongs to a different simulation engine.' }
if (-not $state) {
    $python='C:/Python314/pythonw.exe'
    if (-not (Test-Path -LiteralPath $python)) { $python=(Get-Command python.exe).Source }
    $logDir=Join-Path $projectRoot 'artifacts/simulink-live-20261103'
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $stamp=Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $args=@('-X','utf8','-m','pc_monitor','--port',$Port,'--simulation-engine','simulink','--simulink-runtime',('"'+$RuntimeRoot+'"'))
    $receiver=Start-Process -FilePath $python -ArgumentList $args -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDir ('receiver-'+$stamp+'.stdout.log')) -RedirectStandardError (Join-Path $logDir ('receiver-'+$stamp+'.stderr.log'))
    $receiver.Id | Set-Content -LiteralPath (Join-Path $logDir 'receiver.pid')
    $ready=$false
    for($attempt=0;$attempt -lt 30;$attempt++) {
        Start-Sleep -Milliseconds 200
        try { $state=Invoke-RestMethod -Uri ($base+'/api/simulation/state') -TimeoutSec 1; $ready=$true; break } catch { }
    }
    if(-not $ready) { throw 'Local Simulink receiver did not become ready. Inspect its logs.' }
}
if($ConnectBoard) { & (Join-Path $PSScriptRoot 'start_rs485_monitor.ps1') -Port $Port -NoBrowser }
if(-not $ReceiverOnly) {
    $logDir=Join-Path $projectRoot 'artifacts/simulink-live-20261103'
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $stamp=Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $escapedProject=$projectRoot.Replace('\','/').Replace("'","''")
    $escapedRuntime=$RuntimeRoot.Replace('\','/').Replace("'","''")
    $code="addpath('$escapedProject/matlab'); powerscope_demo('$escapedProject','$escapedRuntime',$Port);"
    # The requested deliverable is an interactive MATLAB/Simulink window.
    $app=Start-Process -FilePath $Matlab -ArgumentList @('-desktop','-logfile',('"'+(Join-Path $logDir ('console-'+$stamp+'.log'))+'"'),'-r',('"'+$code+'"')) -WorkingDirectory $RuntimeRoot -PassThru
    $app.Id | Set-Content -LiteralPath (Join-Path $logDir 'console.pid')
}
Write-Output 'PowerScope Simulink receiver ready. Main controls and waveforms are in MATLAB.'
