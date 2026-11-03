$ErrorActionPreference='Continue';$ProgressPreference='SilentlyContinue'
$task=Get-ScheduledTask -TaskName 'FPGAPoseAI-Relay' -ErrorAction SilentlyContinue
if($task){Write-Host ('Assistant task: '+$task.State)}else{Write-Host 'Assistant task: not installed'}
$private=Join-Path $env:LOCALAPPDATA 'FPGAPoseAI'
$config=Join-Path $private 'credentials.clixml'
$board='http://192.168.2.99:8081'
if(Test-Path -LiteralPath $config){$s=Import-Clixml -LiteralPath $config;if($s.Board){$board=$s.Board};$s=$null}
try{
 $s=Invoke-RestMethod -Uri ($board+'/api/status') -TimeoutSec 5
 [pscustomobject]@{BoardReady=$s.ready;CameraRunning=$s.running;AssistantConnected=$s.ai.relay_connected;AIEnabled=$s.ai.enabled;AIState=$s.ai.state;Error=$s.ai.error} | Format-List
}catch{Write-Host 'Board is currently unreachable. The background assistant keeps waiting.'}
$log=Join-Path $private 'relay.log'
if(Test-Path -LiteralPath $log){Write-Host 'Recent assistant log:';Get-Content -LiteralPath $log -Encoding UTF8 -Tail 8}
