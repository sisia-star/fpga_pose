$ErrorActionPreference='Stop'
$task=Get-ScheduledTask -TaskName 'FPGAPoseAI-Relay' -ErrorAction SilentlyContinue
if(-not $task){Write-Host 'No automatic assistant task installed.';exit 0}
if($task.Description -notlike 'FPGA_POSE_AUTOCONNECT_V1*'){throw 'Unrelated task; nothing changed.'}
Stop-ScheduledTask -TaskName 'FPGAPoseAI-Relay' -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName 'FPGAPoseAI-Relay' -Confirm:$false
Write-Host 'Automatic startup removed. Encrypted credentials retained locally for reinstall.'
