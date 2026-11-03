# Run in Windows PowerShell as the same Windows user that will use the project.
[CmdletBinding()]
param([string]$Board='http://192.168.2.99:8081')
$ErrorActionPreference='Stop'
if($env:OS -ne 'Windows_NT'){throw 'This installer only runs on Windows.'}
$project=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
foreach($name in @('ai_coach.py','ai_coach_relay.py')){
 if(-not (Test-Path -LiteralPath (Join-Path $project $name))){throw "Missing project file: $name"}
}
$uri=$null
if(-not [Uri]::TryCreate($Board,[UriKind]::Absolute,[ref]$uri) -or $uri.Scheme -notin @('http','https') -or $uri.UserInfo -or $uri.Query -or $uri.Fragment -or $uri.AbsolutePath -ne '/'){
 throw 'Board must be a root URL such as http://192.168.2.99:8081'
}
$Board=$Board.TrimEnd('/')
$taskName='FPGAPoseAI-Relay'
$marker='FPGA_POSE_AUTOCONNECT_V1'
$old=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if($old -and $old.Description -notlike "$marker*"){throw 'An unrelated scheduled task uses this name. Nothing changed.'}
$python=$null
foreach($name in @('python','python3','py')){
 $candidate=Get-Command $name -ErrorAction SilentlyContinue
 if(-not $candidate){continue}
 $prefix=@();if($name -eq 'py'){$prefix=@('-3')}
 $ErrorActionPreference='Continue'
 $probe=@(& $candidate.Source @prefix -c 'import sys; assert sys.version_info >= (3,10); print(sys.executable)' 2>$null)
 $code=$LASTEXITCODE
 $ErrorActionPreference='Stop'
 if($code -eq 0 -and $probe.Count -eq 1 -and (Test-Path -LiteralPath ([string]$probe[0]))){$python=[string]$probe[0];break}
}
if(-not $python){throw 'Python 3.10 or later was not found.'}
Write-Host 'First setup: enter your DeepSeek API Key. Input is hidden.'
$key=Read-Host 'DeepSeek API Key' -AsSecureString
if($key.Length -lt 2){throw 'The key was not fully entered. Run setup again.'}
$pair=Read-Host 'Board persistent pairing token (from board_manage show-token)' -AsSecureString
$pairText=[System.Net.NetworkCredential]::new('',$pair).Password
if($pairText -notmatch '^[A-Za-z0-9_-]{20,128}$'){$pairText=$null;throw 'Pairing token was not fully entered. Run setup again.'}
$pairText=$null
# These SecureStrings are encrypted by DPAPI through Export-Clixml on Windows.
# They are deliberately outside the project so a project ZIP cannot carry keys.
$private=Join-Path $env:LOCALAPPDATA 'FPGAPoseAI'
New-Item -ItemType Directory -Path $private -Force | Out-Null
$acl=New-Object System.Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true,$false)
$userSid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User
foreach($sid in @($userSid,[System.Security.Principal.SecurityIdentifier]::new('S-1-5-18'),[System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))){
 $rule=[System.Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
 $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $private -AclObject $acl
$configPath=Join-Path $private 'credentials.clixml'
$config=[pscustomobject]@{Schema='fpga-pose-relay-private-v1';ApiKey=$key;PairToken=$pair;Board=$Board;Model='deepseek-flash';Python=$python;Project=$project}
if($old){Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue}
$temp=Join-Path $private 'credentials.new.clixml'
$config | Export-Clixml -LiteralPath $temp -Encoding UTF8
# Verify DPAPI round trip without displaying either secret.
$verify=Import-Clixml -LiteralPath $temp
if($verify.ApiKey -isnot [Security.SecureString] -or $verify.PairToken -isnot [Security.SecureString]){throw 'Encrypted credential verification failed.'}
Move-Item -LiteralPath $temp -Destination $configPath -Force
$runner=Join-Path $PSScriptRoot 'windows_runner.ps1'
$shell=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$arguments='-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "'+$runner+'" -Config "'+$configPath+'"'
$action=New-ScheduledTaskAction -Execute $shell -Argument $arguments -WorkingDirectory $project
$user=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger=New-ScheduledTaskTrigger -AtLogOn -User $user
$principal=New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "$marker - encrypted credentials, user logon, project $project" -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
$key=$null;$pair=$null;$config=$null;$verify=$null
Write-Host 'Installed. The assistant starts now and whenever this Windows user logs in.'
Write-Host 'It waits and reconnects if the board is not yet available.'
Write-Host ('Check connection: powershell -NoProfile -File "'+(Join-Path $PSScriptRoot 'windows_status.ps1')+'"')
