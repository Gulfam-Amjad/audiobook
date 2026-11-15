# Keep the web app running: start it at every logon, bring it back if it stops,
# and put a "BookInAFlash Studio" shortcut on the desktop that opens it.
#
#   .\scripts\install_web_task.ps1          # install (or update) and start now
#   .\scripts\install_web_task.ps1 -Remove  # uninstall
#
# The task runs scripts\serve.py without a window. Its output is in
# <data root>\logs\web.log. Closing Cursor or a terminal no longer stops it.
param([switch]$Remove)

$TaskName = "AVM Web"
$Root = Split-Path -Parent $PSScriptRoot
$Shortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "BookInAFlash Studio.lnk"

if ($Remove) {
    schtasks /Delete /TN $TaskName /F | Out-Null
    Remove-Item -LiteralPath $Shortcut -ErrorAction SilentlyContinue
    Write-Output "Removed the '$TaskName' task and the desktop shortcut."
    exit 0
}

$Pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Pythonw)) { Write-Error "No .venv found under $Root."; exit 1 }
$Script = Join-Path $Root "scripts\serve.py"

$Action = New-ScheduledTaskAction -Execute $Pythonw -Argument "-X utf8 `"$Script`"" -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "Keeps the AI Video Generation Manager web app running (youtube\scripts\serve.py)." -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

$Url = "http://127.0.0.1:8765/book-video"
$Command = "Start-ScheduledTask -TaskName '$TaskName' -ErrorAction SilentlyContinue; Start-Sleep -Seconds 3; Start-Process '$Url'"
$Shell = New-Object -ComObject WScript.Shell
$Link = $Shell.CreateShortcut($Shortcut)
$Link.TargetPath = "powershell.exe"
$Link.Arguments = "-NoProfile -WindowStyle Hidden -Command `"$Command`""
$Link.WorkingDirectory = $Root
$Link.Description = "Open BookInAFlash Studio (starts the app if it is not running)"
$Link.Save()

Write-Output "Installed '$TaskName': the app starts at every logon and restarts if it stops."
Write-Output "Desktop shortcut: $Shortcut"
