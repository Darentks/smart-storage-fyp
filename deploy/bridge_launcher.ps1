param([Parameter(Mandatory=$true)][string]$Uri)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
try {
    # The browser supplies an action only, never executable paths or arguments.
    if ($Uri -cnotmatch '^smartstorage://(start|restart)/?$') { throw 'Unsupported launcher link.' }
    $action = $Matches[1]
    $projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
    $configPath = Join-Path $projectRoot '.runtime\bridge-launcher.json'
    if (!(Test-Path -LiteralPath $configPath)) { throw 'Reinstall the launcher with your working Python interpreter.' }
    $pythonPath = (Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json).python
    $bridgePath = Join-Path $projectRoot 'VisionValidation_Module\camera_service.py'
    $supervisorPath = Join-Path $projectRoot 'deploy\run_bridge.py'
    if (!(Test-Path -LiteralPath $pythonPath)) { throw 'Configured Python is missing. Reinstall the launcher with your working interpreter.' }
    $mutex = [Threading.Mutex]::new($false, 'Local\SmartStorageBridgeLauncher')
    if (!$mutex.WaitOne(0)) { return }
    try {
        # Match the script as the first Python argument, not arbitrary command text or a port owner.
        $scriptPattern = '(?:' + [regex]::Escape($bridgePath) + '|' + [regex]::Escape($supervisorPath) + ')'
        $commandPattern = '^\s*(?:"[^"]+"|\S+)\s+(?:"' + $scriptPattern + '"|' + $scriptPattern + ')(?:\s|$)'
        $processes = @(Get-CimInstance Win32_Process | Where-Object {
            $_.Name -match '^python(?:w|\d+(?:\.\d+)?)?\.exe$' -and $_.CommandLine -match $commandPattern
        })
        if ($action -eq 'start' -and $processes.Count -gt 0) { return }
        if ($action -eq 'restart' -and $processes.Count -gt 0) {
            $answer = [System.Windows.Forms.MessageBox]::Show(
                'Restart the local bridge? Current RAM inventory will be cleared. Keep hands clear while fresh counts are scanned.',
                'Smart Storage bridge', 'YesNo', 'Warning')
            if ($answer -ne 'Yes') { return }
            # Stop supervisors first so they cannot respawn the bridge during restart.
            $processes = $processes | Sort-Object @{Expression={ if ($_.CommandLine -match [regex]::Escape($supervisorPath)) { 0 } else { 1 } }}
            foreach ($process in $processes) {
                $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.ProcessId)"
                if ($current -and $current.CreationDate -eq $process.CreationDate -and $current.CommandLine -match $commandPattern) {
                    # Include this bridge's inference worker; never kill all Python/Node processes.
                    $null = & "$env:SystemRoot\System32\taskkill.exe" /PID $process.ProcessId /T /F 2>&1
                    if ($LASTEXITCODE -ne 0) { throw 'Could not stop the bridge. Close its terminal and try Start again.' }
                }
            }
        }
        $child = Start-Process -FilePath $pythonPath -ArgumentList ('"' + $bridgePath + '"') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
        if ($child.WaitForExit(3000)) {
            throw 'Python exited during startup. Run camera_service.py with the configured Python in a terminal to inspect the error.'
        }
    } finally {
        $mutex.ReleaseMutex()
        $mutex.Dispose()
    }
} catch {
    [System.Windows.Forms.MessageBox]::Show('The bridge launcher could not finish. ' + $_.Exception.Message, 'Smart Storage bridge', 'OK', 'Error') | Out-Null
    exit 1
}
