# Run this once (as Administrator if it complains about access) to:
#  1. Disable the old "Grocery Tracker Weekly Update" task, which ran the
#     full scrape locally and only worked when the PC was on at 9am Wed.
#  2. Register a new lightweight "Grocery Tracker iCloud Sync" task that
#     just pulls the results GitHub Actions already produced and copies
#     them into iCloud Drive. Runs daily at 9:15am and at every logon, so
#     you get fresh data whenever the PC is next on, not just at 9am Wed.

schtasks /change /tn "Grocery Tracker Weekly Update" /disable

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument '-NoProfile -ExecutionPolicy Bypass -File "C:\Users\user\repos\grocery-tracker\scripts\sync_to_icloud.ps1"'
$trigger1 = New-ScheduledTaskTrigger -Daily -At 9:15AM
$trigger2 = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName "Grocery Tracker iCloud Sync" `
    -Action $action -Trigger @($trigger1, $trigger2) -Settings $settings `
    -Description "Pulls the weekly grocery export produced by GitHub Actions and copies it into iCloud Drive." `
    -Force

Get-ScheduledTask -TaskName "Grocery Tracker iCloud Sync" | Select-Object TaskName, State
