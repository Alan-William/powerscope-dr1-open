param(
    [string]$Matlab='D:/Program Files/MATLAB/R2023b/bin/matlab.exe',
    [string]$RuntimeRoot='E:/fpga_workflow_runs/powerscope-simulink-runtime',
    [string]$ResearchSource=(Join-Path $PSScriptRoot '../simulation/source')
)
$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath $Matlab)) { throw 'MATLAB executable not found.' }
New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null
$escapedProject=$projectRoot.Replace('\','/').Replace("'","''")
$escapedRuntime=$RuntimeRoot.Replace('\','/').Replace("'","''")
$escapedResearch=$ResearchSource.Replace('\','/').Replace("'","''")
$buildCode="addpath('$escapedProject/matlab'); powerscope_build('$escapedProject','$escapedRuntime','$escapedResearch');"
$stamp=Get-Date -Format 'yyyyMMdd-HHmmss'
$log=Join-Path $RuntimeRoot ('build-'+$stamp+'.log')
$process=Start-Process -FilePath $Matlab -ArgumentList @('-batch',('"'+$buildCode+'"'),'-logfile',('"'+$log+'"')) -WindowStyle Hidden -PassThru
$process.Id | Set-Content -LiteralPath (Join-Path $RuntimeRoot 'build.pid')
Write-Output ('MATLAB build PID '+$process.Id+'; log '+$log)
