param(
    [string]$Config = "config.toml",
    [switch]$RestartOnFailure,
    [int]$MaxRestarts = 10,
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Create .venv and install the project first; see README.md.'
}
if ($MaxRestarts -lt 0 -or $MaxRestarts -gt 100) {
    throw 'MaxRestarts must be between 0 and 100.'
}
if (-not [IO.Path]::IsPathRooted($Config)) {
    $Config = Join-Path $projectRoot $Config
}
if (-not (Test-Path -LiteralPath $Config)) {
    throw 'Copy config.example.toml to config.toml and replace the example chat IDs.'
}
$Config = (Resolve-Path -LiteralPath $Config).Path

# Read only environment-variable NAMES; never echo their values or put secrets in arguments.
$envNames = & $pythonPath -m bridge --config $Config required-env
if ($LASTEXITCODE -ne 0) {
    throw 'Invalid configuration; see config.example.toml.'
}
foreach ($envName in $envNames) {
    if ($envName -notmatch '^[A-Z][A-Z0-9_]{0,79}$') {
        throw 'Invalid environment variable name in configuration.'
    }
    if (-not [Environment]::GetEnvironmentVariable($envName, 'Process') -and -not $NonInteractive) {
        $secureValue = Read-Host "Enter $envName (hidden input)" -AsSecureString
        $plainValue = [System.Net.NetworkCredential]::new('', $secureValue).Password
        [Environment]::SetEnvironmentVariable($envName, $plainValue, 'Process')
        $plainValue = $null
        $secureValue.Dispose()
    }
}

Push-Location -LiteralPath $projectRoot
try {
    & $pythonPath -m bridge --config $Config check
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    $restartCount = 0
    while ($true) {
        & $pythonPath -m bridge --config $Config run
        $runExit = $LASTEXITCODE
        if ($runExit -eq 0 -or $runExit -eq 2 -or -not $RestartOnFailure -or $restartCount -ge $MaxRestarts) {
            exit $runExit
        }
        $delaySeconds = [Math]::Min(60, [Math]::Pow(2, [Math]::Min($restartCount + 1, 6)))
        $restartCount++
        Write-Host "Bridge stopped; restart $restartCount/$MaxRestarts in $delaySeconds seconds."
        Start-Sleep -Seconds $delaySeconds
    }
}
finally {
    Pop-Location
}
