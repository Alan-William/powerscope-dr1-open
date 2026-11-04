param([string]$CompilerRoot = 'D:/Program Files/MATLAB/R2023b/sys/lcc64/lcc64',
      [string]$BuildRoot = 'E:/fpga_workflow_runs/powerscope-live-sim-build')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $projectRoot 'pc_monitor/native/npc_core.c'
$buildDirectory = Join-Path $projectRoot 'artifacts/live-sim-runtime'
New-Item -ItemType Directory -Path $buildDirectory -Force | Out-Null
$compiler = Join-Path $CompilerRoot 'bin/lcc64.exe'
$linker = Join-Path $CompilerRoot 'bin/lcclnk64.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw 'Set -CompilerRoot to the installed MATLAB LCC64 directory.' }
Copy-Item -LiteralPath $source -Destination (Join-Path $buildDirectory 'npc_core.c')
# The installed LCC toolchain requires an ASCII build working directory.
New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null
Copy-Item -LiteralPath $source -Destination (Join-Path $BuildRoot 'npc_core.c')
Push-Location $BuildRoot
try {
    & $compiler '-dll' '-c' '-noregistrylookup' ('-I' + (Join-Path $CompilerRoot 'include64')) 'npc_core.c'
    if ($LASTEXITCODE -ne 0) { throw 'Simulation core compilation failed.' }
    & $linker '-dll' ('-L' + (Join-Path $CompilerRoot 'lib64')) '-o' 'npc_core.dll' 'npc_core.obj'
    if ($LASTEXITCODE -ne 0) { throw 'Simulation core linking failed.' }
    Copy-Item -LiteralPath 'npc_core.dll' -Destination (Join-Path $buildDirectory 'npc_core.dll')
    [ordered]@{schema=1; source_sha256=(Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLower(); dll_sha256=(Get-FileHash -LiteralPath 'npc_core.dll' -Algorithm SHA256).Hash.ToLower(); compiler=$compiler} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $buildDirectory 'build.json') -Encoding utf8
} finally { Pop-Location }
Write-Output (Join-Path $buildDirectory 'npc_core.dll')
