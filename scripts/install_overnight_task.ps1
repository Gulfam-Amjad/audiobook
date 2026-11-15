# Start the overnight book queue at every logon, so it carries on after a reboot.
#
#   .\scripts\install_overnight_task.ps1          # install (or update)
#   .\scripts\install_overnight_task.ps1 -Remove  # uninstall
#
# The task runs scripts\overnight.py run without a window. With an empty
# queue it exits at once. Progress is in <data root>\queue\logs.
param([switch]$Remove)

$TaskName = "AVM Overnight"
$Root = Split-Path -Parent $PSScriptRoot

if ($Remove) {
    schtasks /Delete /TN $TaskName /F | Out-Null
    Write-Output "Removed the '$TaskName' task."
    exit 0
}

$Pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Pythonw)) { $Pythonw = Join-Path $Root ".venv\Scripts\python.exe" }
if (-not (Test-Path $Pythonw)) { Write-Error "No .venv found under $Root."; exit 1 }
$Script = Join-Path $Root "scripts\overnight.py"

$Action = New-ScheduledTaskAction -Execute $Pythonw -Argument "-X utf8 `"$Script`" run" -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Priority 4 is normal. The default, 7, is below normal, so a browser always
# takes the CPU ahead of a book and the video takes longer than it should.
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -Priority 4
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "Makes queued audiobooks (youtube\scripts\overnight.py)." -Force | Out-Null
Write-Output "Installed '$TaskName': the queue starts at every logon."
Write-Output "Start it now with: Start-ScheduledTask -TaskName '$TaskName'"
