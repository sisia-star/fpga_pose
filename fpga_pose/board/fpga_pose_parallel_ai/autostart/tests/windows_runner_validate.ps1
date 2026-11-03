$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
$review=Join-Path ([IO.Path]::GetTempPath()) ('FPGA-Pose-Runner-Test-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $review | Out-Null
Expand-Archive -LiteralPath 'D:\FPGA_AI\fpga_pose_autoconnect_20261026.zip' -DestinationPath $review
$python=(Get-Command python).Source
$fixture="import os,sys`nassert os.environ['DEEPSEEK_API_KEY']=='synthetic-api-key-only'`nassert os.environ['AI_COACH_PAIR_TOKEN']=='synthetic-pair-token-1234567890123456'`nprint('synthetic relay received its protected configuration')`nprint('\u5df2\u8fde\u63a5\u677f\u7aef\u6d4b\u8bd5')`n"
[IO.File]::WriteAllText((Join-Path $review 'ai_coach_relay.py'),$fixture,(New-Object Text.UTF8Encoding($false)))
[IO.File]::WriteAllText((Join-Path $review 'ai_coach.py'),'# inert fixture',(New-Object Text.UTF8Encoding($false)))
$config=Join-Path $review 'settings.clixml'
[pscustomobject]@{Schema='fpga-pose-relay-private-v1';ApiKey=(ConvertTo-SecureString 'synthetic-api-key-only' -AsPlainText -Force);PairToken=(ConvertTo-SecureString 'synthetic-pair-token-1234567890123456' -AsPlainText -Force);Board='http://127.0.0.1:9';Model='deepseek-flash';Python=$python;Project=$review} | Export-Clixml -LiteralPath $config -Encoding UTF8
$source=[IO.File]::ReadAllText((Join-Path $review 'autostart\windows_runner.ps1'),[Text.Encoding]::UTF8)
# Bounded fixture: same launcher/config/native pipeline, only one loop and its own mutex.
$source=$source.Replace('while($true){','foreach($testPass in @(1)){').Replace('Start-Sleep -Seconds 15','# one pass only').Replace('Local\FPGAPoseAI-Relay',('Local\FPGA-Pose-Test-'+[Guid]::NewGuid().ToString('N')))
$runner=Join-Path $review 'runner_once.ps1'
[IO.File]::WriteAllText($runner,$source,(New-Object Text.UTF8Encoding($false)))
$ErrorActionPreference='Continue'
& powershell.exe -NoProfile -NonInteractive -File $runner -Config $config
$code=$LASTEXITCODE
$ErrorActionPreference='Stop'
if($code -ne 0){throw "Synthetic runner failed: $code"}
$log=Get-Content -LiteralPath (Join-Path $review 'relay.log') -Raw -Encoding UTF8
if(-not $log.Contains('synthetic relay received its protected configuration')){throw 'Child did not receive protected credentials'}
$chinese=-join @([char]0x5df2,[char]0x8fde,[char]0x63a5,[char]0x677f,[char]0x7aef,[char]0x6d4b,[char]0x8bd5)
if(-not $log.Contains($chinese)){throw 'Chinese relay log encoding failed'}
if($log.Contains('synthetic-api-key-only') -or $log.Contains('synthetic-pair-token-1234567890123456')){throw 'Credential leaked into child log'}
[ordered]@{synthetic_runner_ok=$true;credentials_reached_child=$true;chinese_log_utf8=$true;secrets_absent_from_log=$true;startup_task_registered=$false;network_requests=0} | ConvertTo-Json -Compress
