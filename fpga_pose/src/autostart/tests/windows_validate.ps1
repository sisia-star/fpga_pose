$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
$review=Join-Path ([IO.Path]::GetTempPath()) ('FPGA-Pose-Review-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $review | Out-Null
Expand-Archive -LiteralPath 'D:\FPGA_AI\fpga_pose_autoconnect_20261026.zip' -DestinationPath $review
$results=@()
foreach($file in (Get-ChildItem -LiteralPath (Join-Path $review 'autostart') -Filter '*.ps1')){
 $tokens=$null;$errors=$null
 [System.Management.Automation.Language.Parser]::ParseFile($file.FullName,[ref]$tokens,[ref]$errors) | Out-Null
 if($errors.Count){throw ($file.Name+': '+(($errors | ForEach-Object {$_.Message}) -join '; '))}
 $results+=@{file=$file.Name;parse_ok=$true}
}
# Synthetic data only. No project/environment/user credential files are read.
$testKey=ConvertTo-SecureString 'test-only-not-a-real-api-key' -AsPlainText -Force
$testToken=ConvertTo-SecureString 'test-only-persistent-pair-token-1234567890' -AsPlainText -Force
$p=Join-Path $review 'synthetic.clixml'
[pscustomobject]@{ApiKey=$testKey;PairToken=$testToken} | Export-Clixml -LiteralPath $p -Encoding UTF8
$roundtrip=Import-Clixml -LiteralPath $p
if($roundtrip.ApiKey -isnot [Security.SecureString] -or $roundtrip.PairToken -isnot [Security.SecureString]){throw 'DPAPI type check failed'}
$a=[System.Net.NetworkCredential]::new('',$roundtrip.ApiKey).Password
$b=[System.Net.NetworkCredential]::new('',$roundtrip.PairToken).Password
if($a -ne 'test-only-not-a-real-api-key' -or $b -ne 'test-only-persistent-pair-token-1234567890'){throw 'DPAPI roundtrip failed'}
$raw=[IO.File]::ReadAllText($p)
if($raw.Contains($a) -or $raw.Contains($b)){throw 'Synthetic secrets were not encrypted'}
# Construct task settings in memory; never register or launch a task.
$user=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger=New-ScheduledTaskTrigger -AtLogOn -User $user
$principal=New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
$action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -NonInteractive -File "D:\FPGA_AI\fpga_pose_training_20261025\autostart\windows_runner.ps1"'
$task=New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings
if($settings.ExecutionTimeLimit -ne 'PT0S'){throw 'Task timeout was not unlimited'}
$acl=New-Object System.Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true,$false)
$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User
$rule=[System.Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
$acl.AddAccessRule($rule);Set-Acl -LiteralPath $review -AclObject $acl
$a=$null;$b=$null;$testKey=$null;$testToken=$null;$roundtrip=$null
[ordered]@{powershell=$PSVersionTable.PSVersion.ToString();scripts=$results;dpapi_synthetic_roundtrip=$true;task_settings_valid=$true;acl_valid=$true;startup_task_registered=$false;board_changed=$false;review_directory=$review} | ConvertTo-Json -Depth 5 -Compress
