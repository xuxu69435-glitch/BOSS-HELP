$ErrorActionPreference = 'Stop'
$workspaceRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $workspaceRoot 'venvs\boss-agent-cli\Scripts\pythonw.exe'
$dataPath = Join-Path $workspaceRoot 'data\boss-agent'
$bridgePath = Join-Path $dataPath 'bridge'
$env:PYTHONHOME = $null

try {
    $status = Invoke-RestMethod -Uri 'http://127.0.0.1:19826/status' -TimeoutSec 2
    if ($status.ok) {
        Write-Output 'Browser bridge is already running.'
        exit 0
    }
} catch {}

New-Item -ItemType Directory -Path $bridgePath -Force | Out-Null
$arguments = '-X utf8 -m boss_agent_cli.bridge.daemon --serve --log-to-file --data-dir "' + $dataPath + '"'
Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $workspaceRoot -WindowStyle Hidden | Out-Null

for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 250
    try {
        $status = Invoke-RestMethod -Uri 'http://127.0.0.1:19826/status' -TimeoutSec 1
        if ($status.ok) {
            Write-Output ('Bridge ready. PID=' + $status.pid)
            Write-Output ('Extension connected=' + $status.extensionConnected)
            exit 0
        }
    } catch {}
}
throw 'Browser bridge did not become ready. Check data\boss-agent\bridge\daemon.log.'
