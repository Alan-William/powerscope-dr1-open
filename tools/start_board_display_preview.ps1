param([switch]$Fullscreen)
$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent $PSScriptRoot
$python=Join-Path (Split-Path -Parent $projectRoot) 'qt_demo/.venv/Scripts/pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Use the existing qt_demo PySide6 environment.' }
$logDir=Join-Path $projectRoot 'artifacts/board-display-20261104'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$stamp=Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$launchArgs=@('-X','utf8','-m','board_display','--preview-simulink')
if ($Fullscreen) { $launchArgs+='--fullscreen' }
# The requested deliverable is a visible native touchscreen-layout preview.
$app=Start-Process -FilePath $python -ArgumentList $launchArgs -WorkingDirectory $projectRoot -PassThru -RedirectStandardOutput (Join-Path $logDir ($stamp+'.stdout.log')) -RedirectStandardError (Join-Path $logDir ($stamp+'.stderr.log'))
$app.Id | Set-Content -LiteralPath (Join-Path $logDir 'preview.pid')
Write-Output 'Qt display opened. It reads only the local Simulink receiver; no board probe or diagnosis.'
