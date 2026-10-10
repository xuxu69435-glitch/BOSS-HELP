param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$BossArgs
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$boss = Join-Path $root "venvs\boss-agent-cli\Scripts\boss.exe"
$dataDir = Join-Path $root "data\boss-agent"
$env:PYTHONHOME = $null

if (-not (Test-Path $boss)) {
    throw "boss.exe not found: $boss"
}

& $boss --data-dir $dataDir @BossArgs
exit $LASTEXITCODE
