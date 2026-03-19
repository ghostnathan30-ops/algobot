# AlgoBot -- Windows Task Scheduler Setup
# Run this ONCE in PowerShell as Administrator to install the daily bot task
# Right-click PowerShell -> "Run as administrator" -> paste this script path

$TaskName    = "AlgoBot_DailyRun"
$ScriptPath  = "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot\scripts\run_bot_daily.bat"
$Description = "Starts AlgoBot paper trading every weekday at 9:00 AM ET"

# Remove old task if exists
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

# Trigger: weekdays at 9:00 AM
$Trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "09:00AM"

# Action: run the batch file
$Action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"$ScriptPath`""

# Settings: run whether user is logged on or not, restart on failure
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable `
    -WakeToRun

# Register the task
Register-ScheduledTask `
    -TaskName $TaskName `
    -Description $Description `
    -Trigger $Trigger `
    -Action $Action `
    -Settings $Settings `
    -RunLevel Highest `
    -Force

Write-Host ""
Write-Host "Task '$TaskName' installed successfully." -ForegroundColor Green
Write-Host "Bot will auto-start every weekday at 9:00 AM ET." -ForegroundColor Green
Write-Host ""
Write-Host "To run manually right now:"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host ""
Write-Host "To view the task:"
Write-Host "  Open Task Scheduler -> Task Scheduler Library -> $TaskName"
