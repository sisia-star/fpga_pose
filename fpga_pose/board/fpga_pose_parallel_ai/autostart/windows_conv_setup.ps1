$ErrorActionPreference='Stop'
$project=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$taskName='FPGAPose-Conv-Relay'
$old=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if($old -and $old.Description -notlike 'FPGA_POSE_CONV_RELAY_V1*'){throw 'Unrelated task name conflict'}
if($old){Write-Output 'Convolution task already exists; inspect before reinstalling.';exit 0}
$runner=Join-Path $PSScriptRoot 'windows_conv_runner.ps1'
$shell=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$action=New-ScheduledTaskAction -Execute $shell -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "'+$runner+'"') -WorkingDirectory $project
$user=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal=New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$trigger=New-ScheduledTaskTrigger -AtLogOn -User $user
$settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Trigger $trigger -Settings $settings -Description ('FPGA_POSE_CONV_RELAY_V1 - MediaPipe CPU prefix and mandatory PL head; '+$project)|Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Output 'Convolution assistant installed; original DeepSeek task and encrypted credentials retained.'
