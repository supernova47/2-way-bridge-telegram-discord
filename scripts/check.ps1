$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
Push-Location -LiteralPath $projectRoot
try {
    & $pythonPath -m ruff check bridge tests
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $pythonPath -m ruff format --check bridge tests
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $pythonPath -m unittest discover -v
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $pythonPath -m build --no-isolation
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
