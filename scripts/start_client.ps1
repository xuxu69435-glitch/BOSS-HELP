param([switch]$OpenBrowser)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root 'venvs\boss-agent-cli\Scripts\pythonw.exe'
$data = Join-Path $root 'data\boss-agent'
$settings = Join-Path $data 'preferences\settings.json'
$runtime = Join-Path $data 'client-control\runtime.json'
$env:PYTHONHOME = $null

function Get-ClientUrl {
    if (-not (Test-Path -LiteralPath $runtime)) { return $null }
    try {
        $info = Get-Content -LiteralPath $runtime -Raw -Encoding UTF8 | ConvertFrom-Json
        $uri = [Uri]$info.url
        if ($uri.Scheme -ne 'http' -or $uri.Host -ne '127.0.0.1') { return $null }
        if (-not $info.launch_token) { return $null }
        $challenge = [Guid]::NewGuid().ToString('N')
        $health = Invoke-RestMethod -Uri ($info.url + '/health?challenge=' + $challenge) -TimeoutSec 2
        $mac = [System.Security.Cryptography.HMACSHA256]::new([Text.Encoding]::UTF8.GetBytes($info.launch_token))
        try { $proof = ([BitConverter]::ToString($mac.ComputeHash([Text.Encoding]::UTF8.GetBytes($challenge)))).Replace('-', '').ToLowerInvariant() } finally { $mac.Dispose() }
        if ($health.app -eq 'boss-local-client' -and $health.data_id -eq $info.data_id -and $health.proof -ceq $proof) { return ($info.url + '#client_token=' + $info.launch_token) }
    } catch { return $null }
    return $null
}

try {
    $url = Get-ClientUrl
    if (-not $url) {
        if (-not (Test-Path -LiteralPath $python)) { throw 'Project Python environment not found (pythonw.exe)' }
        if (-not (Test-Path -LiteralPath $settings)) {
            $initArgs = @('-I', '-X', 'utf8', ('"' + (Join-Path $PSScriptRoot 'initialize_config.py') + '"'), '--root', ('"' + $root + '"'))
            $initResult = Start-Process -FilePath $python -ArgumentList $initArgs -WorkingDirectory $root -WindowStyle Hidden -Wait -PassThru
            if ($initResult.ExitCode -ne 0) { throw 'Could not initialize local configuration.' }
        }
        $env:PYTHONUTF8 = '1'
        $arguments = @('-m', 'boss_agent_cli.local_client', '--data-dir', ('"' + $data + '"'), '--settings', ('"' + $settings + '"'))
        Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $root -WindowStyle Hidden | Out-Null
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            Start-Sleep -Milliseconds 500
            $url = Get-ClientUrl
            if ($url) { break }
        }
    }
    if (-not $url) { throw 'Client did not become ready. Check data\boss-agent\client-startup-error.log and client-control\server.log' }
    if ($OpenBrowser) {
        try {
            Start-Process -FilePath $url -ErrorAction Stop
        } catch {
            throw 'The client is running, but the default browser could not be opened. Check the Windows default browser setting.'
        }
    } else {
        Write-Output $url
    }
} catch {
    $message = $_.Exception.Message
    try {
        $log = Join-Path $root 'logs\client-launcher.log'
        New-Item -ItemType Directory -Path (Split-Path -Parent $log) -Force | Out-Null
        Add-Content -LiteralPath $log -Value ('[' + (Get-Date -Format o) + '] ' + $message) -Encoding UTF8
    } catch {}
    Write-Error -Message $message -ErrorAction Continue
    exit 1
}
