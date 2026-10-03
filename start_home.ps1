param([string]$PythonPath = '', [int]$Port = 8773)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if ($PythonPath) {
    & $PythonPath -m robot_voice_patrol --mode mock --home --fixture-skills --port $Port --db .runtime/home.sqlite3
    exit $LASTEXITCODE
}
$candidates = @()
$pyCommand = Get-Command python -ErrorAction SilentlyContinue
if ($pyCommand -and $pyCommand.Source -notlike '*WindowsApps*') { $candidates += $pyCommand.Source }
# Optional Codex-bundled runtime on this machine; no downloads or system edits.
$bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (Test-Path -LiteralPath $bundled) { $candidates += $bundled }
if ($candidates.Count -gt 0) {
    & $candidates[0] -m robot_voice_patrol --mode mock --home --fixture-skills --port $Port --db .runtime/home.sqlite3
    exit $LASTEXITCODE
}
$launcher = Get-Command py -ErrorAction SilentlyContinue
if ($launcher) {
    & $launcher.Source -3 -m robot_voice_patrol --mode mock --home --fixture-skills --port $Port --db .runtime/home.sqlite3
    exit $LASTEXITCODE
}
throw 'Python 3.10+ not found. Install Python or pass -PythonPath C:\path\to\python.exe.'
