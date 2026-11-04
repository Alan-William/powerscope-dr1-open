param([int]$Port = 18766, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$monitorUrl = 'http://127.0.0.1:' + $Port + '/'
# The serial monitor remains available if the board network is disconnected.
$bridgeReady = $false
try {
    $boardState = Invoke-RestMethod -Uri 'http://127.0.0.1:18765/api/state' -TimeoutSec 2
    $bridgeReady = $boardState.backend -eq 'Alnpu'
} catch { }
if (-not $bridgeReady) {
    $sshConfig = $env:POWERSCOPE_SSH_CONFIG
    if (-not $sshConfig) { $sshConfig = 'E:/fpga_workflow_runs/hardware-test-20261002/ssh_config' }
    $sshCommand = Get-Command ssh.exe -ErrorAction SilentlyContinue
    if ($sshCommand -and (Test-Path -LiteralPath $sshConfig)) {
        $bridgeLogs = Join-Path $projectRoot 'artifacts/pc-rs485-monitor'
        New-Item -ItemType Directory -Path $bridgeLogs -Force | Out-Null
        $bridgeStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
        Start-Process -FilePath $sshCommand.Source -ArgumentList @('-F', ('"' + $sshConfig + '"'), '-N', '-L', '127.0.0.1:18765:127.0.0.1:8765', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes', '-o', 'ConnectTimeout=5', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2', 'anlogic-board-test') -WindowStyle Hidden -RedirectStandardError (Join-Path $bridgeLogs ('tunnel-' + $bridgeStamp + '.log')) | Out-Null
    }
}
try {
    $state = Invoke-RestMethod -Uri ($monitorUrl + 'api/state') -TimeoutSec 2
} catch { $state = $null }
if ($null -ne $state) {
    if ($state.identity -ne 'powerscope-rs485-monitor') { throw 'This port is used by another application.' }
} else {
    $pythonCandidates = @('C:/Python314/pythonw.exe', 'C:/Python314/python.exe')
    $registeredPython = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($registeredPython) { $pythonCandidates += $registeredPython.Source }
    $pythonLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pythonLauncher) {
        $launcherPython = & $pythonLauncher.Source -3 -c 'import sys; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $launcherPython) { $pythonCandidates += $launcherPython }
    }
    $monitorPython = $null
    foreach ($candidate in $pythonCandidates) {
        if (Test-Path -LiteralPath $candidate) {
            $probe = $candidate -replace 'pythonw.exe$', 'python.exe'
            & $probe -c 'import serial' 2>$null
            if ($LASTEXITCODE -eq 0) { $monitorPython = $candidate; break }
        }
    }
    if (-not $monitorPython) { throw 'Python with pyserial is required. Install requirements-pl.txt using your Python environment.' }
    $logDirectory = Join-Path $projectRoot 'artifacts/pc-rs485-monitor'
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $process = Start-Process -FilePath $monitorPython -ArgumentList @('-m', 'pc_monitor', '--port', $Port) -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDirectory 'server.stdout.log') -RedirectStandardError (Join-Path $logDirectory 'server.stderr.log')
    $process.Id | Set-Content -LiteralPath (Join-Path $logDirectory 'server.pid')
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 200
        if ($process.HasExited) { throw 'Monitor could not start. See artifacts/pc-rs485-monitor/server.stderr.log.' }
        try {
            $state = Invoke-RestMethod -Uri ($monitorUrl + 'api/state') -TimeoutSec 1
            if ($state.identity -eq 'powerscope-rs485-monitor') { $ready = $true; break }
        } catch { }
    }
    if (-not $ready) { throw 'Monitor did not become ready. Inspect its local log before starting another copy.' }
}
if (-not $NoBrowser) { Start-Process $monitorUrl }
Write-Output $monitorUrl
