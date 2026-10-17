# Registers the Windows scheduled tasks:
#   eClass Digest - Morning   "due today" email               (default 7:30 AM)
#   eClass Digest - Checks    alert only if something is new  (every 2 hours, 9 AM - 9 PM)
#   eClass Digest - Evening   full digest                     (default 8:00 PM)
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Evening 21:00 -CheckEvery 3
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Remove
param(
    [string]$Morning = "07:30",
    [string]$Evening = "20:00",
    [string]$FirstCheck = "09:00",
    [string]$LastCheck = "21:00",
    [int]$CheckEvery = 2,
    [string]$Python = "",
    [switch]$Remove
)

$Names = @("eClass Digest - Morning", "eClass Digest - Checks", "eClass Digest - Evening",
           "eClass Daily Digest")  # the last one is the old single 7 AM task

foreach ($n in $Names) {
    if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $n -Confirm:$false
    }
}
if ($Remove) {
    Write-Host "Removed the eClass Digest tasks."
    exit 0
}

$ProjectDir = Split-Path -Parent $PSScriptRoot
# pythonw runs without popping up a console window; output goes to logs\eclass_digest.log
if (-not $Python) { $Python = Join-Path $ProjectDir ".venv\Scripts\pythonw.exe" }
if (-not (Test-Path $Python)) {
    Write-Error "No Python at $Python. Follow the README setup first."
    exit 1
}

# StartWhenAvailable: if the PC was off/asleep at the time, run as soon as it's back.
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

function Add-DigestTask($Name, $Mode, $Trigger, $Description) {
    $Action = New-ScheduledTaskAction -Execute $Python -Argument "-m eclass_digest --mode $Mode" `
        -WorkingDirectory $ProjectDir
    Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -Settings $Settings `
        -Description $Description -Force | Out-Null
}

Add-DigestTask "eClass Digest - Morning" "morning" (New-ScheduledTaskTrigger -Daily -At $Morning) `
    "Emails what's due today on eClass."

$span = [datetime]::Parse($LastCheck) - [datetime]::Parse($FirstCheck) + (New-TimeSpan -Minutes 1)
$Checks = New-ScheduledTaskTrigger -Daily -At $FirstCheck
$Checks.Repetition = (New-ScheduledTaskTrigger -Once -At $FirstCheck `
    -RepetitionInterval (New-TimeSpan -Hours $CheckEvery) -RepetitionDuration $span).Repetition
Add-DigestTask "eClass Digest - Checks" "check" $Checks `
    "Checks eClass every few hours; emails only when there's a new grade, announcement, file or deadline change."

Add-DigestTask "eClass Digest - Evening" "digest" (New-ScheduledTaskTrigger -Daily -At $Evening) `
    "Emails the full eClass digest and syncs deadlines to Google Calendar."

Write-Host "Scheduled:"
Write-Host "  Morning 'due today'   $Morning"
Write-Host "  Checks                every $CheckEvery h, $FirstCheck - $LastCheck (email only if something's new)"
Write-Host "  Evening digest        $Evening"
Write-Host "Run one now with e.g.: Start-ScheduledTask -TaskName 'eClass Digest - Checks'"
