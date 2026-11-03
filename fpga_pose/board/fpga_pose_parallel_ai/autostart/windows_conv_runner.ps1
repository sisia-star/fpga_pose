[CmdletBinding()]
param([string]$Config=(Join-Path $env:LOCALAPPDATA 'FPGAPoseAI\credentials.clixml'))
$ErrorActionPreference='Stop'
$project=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python=Join-Path $project '.conv-venv\Scripts\python.exe'
if(-not (Test-Path $python)){throw 'Install convolution Python dependencies first.'}
$settings=Import-Clixml -LiteralPath $Config
if($settings.PairToken -isnot [Security.SecureString]){throw 'Encrypted board pairing configuration is unavailable.'}
$mutex=[Threading.Mutex]::new($false,'Local\FPGAPose-Conv-Relay')
$locked=$false
try{
 try{$locked=$mutex.WaitOne(0)}catch [Threading.AbandonedMutexException]{$locked=$true}
 if(-not $locked){exit 0}
 $env:AI_COACH_PAIR_TOKEN=[System.Net.NetworkCredential]::new('',$settings.PairToken).Password
 $env:PYTHONIOENCODING='utf-8'
 $log=Join-Path $env:LOCALAPPDATA 'FPGAPoseAI\conv_relay.log'
 $ErrorActionPreference='Continue'
 & $python -u (Join-Path $project 'mediapipe_conv_relay.py') --board $settings.Board 2>&1 | Out-File -LiteralPath $log -Append -Encoding utf8
 exit $LASTEXITCODE
}finally{
 $env:AI_COACH_PAIR_TOKEN=$null
 if($locked){$mutex.ReleaseMutex()};$mutex.Dispose()
}
