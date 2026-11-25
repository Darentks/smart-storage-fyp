param([Parameter(Mandatory=$true)][string]$PythonExecutable)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$launcherPath = Join-Path $projectRoot 'deploy\bridge_launcher.ps1'
$pythonPath = (Resolve-Path -LiteralPath $PythonExecutable).Path
$bridgePath = Join-Path $projectRoot 'VisionValidation_Module\camera_service.py'
# --help imports the real bridge without opening cameras or starting the server.
$null = & $pythonPath $bridgePath --help 2>&1
if ($LASTEXITCODE -ne 0) { throw 'This Python cannot load the bridge. Choose the interpreter used for your working manual launch.' }
$runtimePath = Join-Path $projectRoot '.runtime'
New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
@{python=$pythonPath} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtimePath 'bridge-launcher.json') -Encoding UTF8
$powershellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$protocolKey = 'HKCU:\Software\Classes\smartstorage'
New-Item -Path $protocolKey -Force | Out-Null
Set-Item -LiteralPath $protocolKey -Value 'URL:Smart Storage local bridge'
New-ItemProperty -LiteralPath $protocolKey -Name 'URL Protocol' -Value '' -PropertyType String -Force | Out-Null
New-Item -Path "$protocolKey\shell\open\command" -Force | Out-Null
$command = '"' + $powershellPath + '" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $launcherPath + '" -Uri "%1"'
Set-Item -LiteralPath "$protocolKey\shell\open\command" -Value $command
Write-Output 'Installed Start/Restart launcher for the current Windows user. No bridge was started or restarted.'
