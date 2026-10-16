# Registers a Windows scheduled task that runs the digest every day.
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1            # 7:00 AM
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Time 06:30
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Remove
param(
    [string]$Time = "07:00",
    [string]$Python = "",
    [switch]$Remove
)

$TaskName = "eClass Daily Digest"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed '$TaskName'."
    exit 0
}

$ProjectDir = Split-Path -Parent $PSScriptRoot
# pythonw runs without popping up a console window; output goes to logs\eclass_digest.log
if (-not $Python) { $Python = Join-Path $ProjectDir ".venv\Scripts\pythonw.exe" }
if (-not (Test-Path $Python)) {
    Write-Error "No Python at $Python. Follow the README setup first."
    exit 1
}

$Action = New-ScheduledTaskAction -Execute $Python -Argument "-m eclass_digest" -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -Daily -At $Time
# StartWhenAvailable: if the PC was off/asleep at $Time, run as soon as it's back.
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "Emails a summary of York eClass and syncs deadlines to Google Calendar." -Force | Out-Null

Write-Host "Scheduled '$TaskName' daily at $Time."
Write-Host "Run it now with: Start-ScheduledTask -TaskName '$TaskName'"
