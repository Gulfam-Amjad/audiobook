<#
    Start the AI Video Generation Manager.

    Usage:
        .\start.ps1              # start the server and open the dashboard
        .\start.ps1 -NoBrowser   # start without opening a browser
        .\start.ps1 -Reload      # auto-reload on code changes (development)

    The server keeps running with the browser closed; generation jobs do not
    depend on a page being open. Press Ctrl+C in this window to stop it.
#>
[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [switch]$Reload
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Write-Host 'Virtual environment not found. Creating it now...' -ForegroundColor Yellow
    python -m venv .venv
    & $python -m pip install --upgrade pip
    & $python -m pip install -r requirements.txt
}

if (-not (Test-Path (Join-Path $PSScriptRoot '.env'))) {
    Write-Host 'No .env file found; copying .env.example.' -ForegroundColor Yellow
    Write-Host 'Provider credentials stay blank until you add them.' -ForegroundColor Yellow
    Copy-Item (Join-Path $PSScriptRoot '.env.example') (Join-Path $PSScriptRoot '.env')
}

# Read host/port from .env so the banner and browser URL match the real bind.
$bindHost = '127.0.0.1'
$bindPort = '8765'
Get-Content (Join-Path $PSScriptRoot '.env') | ForEach-Object {
    if ($_ -match '^\s*AVM_HOST\s*=\s*(.+?)\s*$') { $bindHost = $Matches[1] }
    if ($_ -match '^\s*AVM_PORT\s*=\s*(.+?)\s*$') { $bindPort = $Matches[1] }
}
$url = "http://${bindHost}:${bindPort}/"

Write-Host ''
Write-Host '  AI Video Generation Manager' -ForegroundColor Cyan
Write-Host "  Dashboard: $url"
Write-Host '  Stop with Ctrl+C'
Write-Host ''

if (-not $NoBrowser) {
    Start-Job -ScriptBlock {
        param($target)
        Start-Sleep -Seconds 4
        Start-Process $target
    } -ArgumentList $url | Out-Null
}

$arguments = @('-m', 'uvicorn', 'app.main:app', '--host', $bindHost, '--port', $bindPort)
if ($Reload) { $arguments += '--reload' }

& $python @arguments
