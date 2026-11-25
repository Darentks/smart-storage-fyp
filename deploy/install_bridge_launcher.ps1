$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$launcherPath = Join-Path $projectRoot 'deploy\bridge_launcher.ps1'
$pythonPath = Join-Path $projectRoot 'venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $pythonPath)) { throw 'Restore the project venv before installing the launcher.' }
$powershellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$protocolKey = 'HKCU:\Software\Classes\smartstorage'
New-Item -Path $protocolKey -Force | Out-Null
Set-Item -LiteralPath $protocolKey -Value 'URL:Smart Storage local bridge'
New-ItemProperty -LiteralPath $protocolKey -Name 'URL Protocol' -Value '' -PropertyType String -Force | Out-Null
New-Item -Path "$protocolKey\shell\open\command" -Force | Out-Null
$command = '"' + $powershellPath + '" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $launcherPath + '" -Uri "%1"'
Set-Item -LiteralPath "$protocolKey\shell\open\command" -Value $command
Write-Output 'Installed Start/Restart launcher for the current Windows user. No bridge was started or restarted.'
