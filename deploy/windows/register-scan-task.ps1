# Register the laptop's LinkedIn scan as a Windows scheduled task:
# at logon, then every 6 hours while you are logged on. Output: data\scan_and_push.log
#   powershell -ExecutionPolicy Bypass -File deploy\windows\register-scan-task.ps1
#   (remove later with: Unregister-ScheduledTask -TaskName "ContentPilot scan and push" -Confirm:$false)
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $repo "venv\Scripts\pythonw.exe"      # no console window; the script logs to a file
if (-not (Test-Path $python)) { throw "venv not found at $python" }

$action = New-ScheduledTaskAction -Execute $python -Argument "-m scripts.scan_and_push" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$trigger.Delay = "PT2M"                                     # let Wi-Fi and Tailscale connect first
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 6)).Repetition
# Interactive: runs only while you are logged on, because the scan opens your logged-in browser.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew -RunOnlyIfNetworkAvailable

Register-ScheduledTask -TaskName "ContentPilot scan and push" -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description "LinkedIn saved posts -> ContentPilot on the VM" -Force | Out-Null
Get-ScheduledTask -TaskName "ContentPilot scan and push" | Select-Object TaskName, State
