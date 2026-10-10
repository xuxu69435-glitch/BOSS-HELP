param([string]$PythonCommand = 'py')

$ErrorActionPreference = 'Stop'
$workspaceRoot = Split-Path -Parent $PSScriptRoot
$environmentRoot = Join-Path $workspaceRoot 'venvs\boss-agent-cli'
$pythonPath = Join-Path $environmentRoot 'Scripts\python.exe'
$sourceRoot = Join-Path $workspaceRoot 'repos\boss-agent-cli'

try {
    # This PowerShell process is local to the installer; persistent user settings are unchanged.
    Get-ChildItem Env: | Where-Object { $_.Name -match '^(PYTHON|PIP_)' -or $_.Name -in @('VIRTUAL_ENV', 'UV_PROJECT_ENVIRONMENT') } | ForEach-Object {
        Remove-Item -LiteralPath ('Env:' + $_.Name)
    }
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        if ($PythonCommand -eq 'py') {
            & $PythonCommand -3.12 -I -X utf8 -m venv $environmentRoot
        } else {
            & $PythonCommand -I -X utf8 -m venv $environmentRoot
        }
        if ($LASTEXITCODE -ne 0) { throw 'Failed to create the project Python environment.' }
    }
    & $pythonPath -I -X utf8 -c 'import sys; from pathlib import Path; assert sys.prefix != sys.base_prefix and Path(sys.prefix).resolve() == Path(sys.argv[1]).resolve(), "Project virtual environment was not recognized"' $environmentRoot
    if ($LASTEXITCODE -ne 0) { throw 'Refusing to install packages outside the project environment.' }
    & $pythonPath -I -X utf8 -m pip --isolated install --disable-pip-version-check -e ($sourceRoot + '[bridge,client]')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check the output above.' }
    & $pythonPath -I -X utf8 -m pip --isolated check
    if ($LASTEXITCODE -ne 0) { throw 'Dependency verification failed.' }
    & $pythonPath -I -X utf8 (Join-Path $PSScriptRoot 'initialize_config.py') --root $workspaceRoot
    if ($LASTEXITCODE -ne 0) { throw 'Local configuration initialization failed.' }
    Write-Output 'Installation complete. Open the client and configure your own profile and filters.'
} catch {
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
    exit 1
}
