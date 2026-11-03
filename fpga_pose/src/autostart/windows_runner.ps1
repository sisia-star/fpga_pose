[CmdletBinding()]
param([string]$Config=(Join-Path $env:LOCALAPPDATA 'FPGAPoseAI\credentials.clixml'))
$ErrorActionPreference='Stop'
if($env:OS -ne 'Windows_NT'){throw 'Windows required for DPAPI.'}
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
$private=Split-Path -Parent $Config
$log=Join-Path $private 'relay.log'
# Covers both a scheduled task and accidental manual invocation of this runner.
$mutex=[Threading.Mutex]::new($false,'Local\FPGAPoseAI-Relay')
$locked=$false
try{
 try{$locked=$mutex.WaitOne(0)}catch [Threading.AbandonedMutexException]{$locked=$true}
 if(-not $locked){exit 0}
 $settings=Import-Clixml -LiteralPath $Config
 if($settings.Schema -ne 'fpga-pose-relay-private-v1' -or $settings.ApiKey -isnot [Security.SecureString] -or $settings.PairToken -isnot [Security.SecureString]){throw 'Invalid encrypted configuration.'}
 $env:DEEPSEEK_API_KEY=[System.Net.NetworkCredential]::new('',$settings.ApiKey).Password
 $env:AI_COACH_PAIR_TOKEN=[System.Net.NetworkCredential]::new('',$settings.PairToken).Password
 $env:DEEPSEEK_MODEL=$settings.Model
 $env:PYTHONIOENCODING='utf-8'
 $env:PYTHONUNBUFFERED='1'
 $relay=Join-Path $settings.Project 'ai_coach_relay.py'
 Set-Location -LiteralPath $settings.Project
 while($true){
  if((Test-Path -LiteralPath $log) -and (Get-Item -LiteralPath $log).Length -gt 2097152){Move-Item -LiteralPath $log -Destination ($log+'.previous') -Force}
  ('['+(Get-Date -Format s)+'] assistant starting') | Out-File -LiteralPath $log -Append -Encoding utf8
  $ErrorActionPreference='Continue'
  & $settings.Python -u $relay --board $settings.Board 2>&1 | Out-File -LiteralPath $log -Append -Encoding utf8
  $code=$LASTEXITCODE
  $ErrorActionPreference='Stop'
  ('['+(Get-Date -Format s)+'] assistant exited '+$code+'; restarting in 15 seconds') | Out-File -LiteralPath $log -Append -Encoding utf8
  Start-Sleep -Seconds 15
 }
}finally{
 $env:DEEPSEEK_API_KEY=$null;$env:AI_COACH_PAIR_TOKEN=$null
 if($locked){$mutex.ReleaseMutex()};$mutex.Dispose()
}
