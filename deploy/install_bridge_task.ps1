param([Parameter(Mandatory=$true)][string]$PythonExecutable)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonPath = (Resolve-Path -LiteralPath $PythonExecutable).Path
$runnerPath = Join-Path $projectRoot 'deploy\run_bridge.py'
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument ('"' + $runnerPath + '"') -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
# Install disabled; configure an appropriate account/permissions and secrets locally first.
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings
Register-ScheduledTask -TaskName 'SmartStorageBridge' -InputObject $task -Force | Out-Null
Disable-ScheduledTask -TaskName 'SmartStorageBridge' | Out-Null
Write-Output 'Task installed disabled. Configure its execution account and secure environment, then enable/start it. Do not start another manual bridge.'
