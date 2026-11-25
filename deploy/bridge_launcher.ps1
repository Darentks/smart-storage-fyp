param([Parameter(Mandatory=$true)][string]$Uri)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
try {
    # The browser supplies an action only, never executable paths or arguments.
    if ($Uri -cnotmatch '^smartstorage://(start|restart|update-models)/?$') { throw 'Unsupported launcher link.' }
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
        $updating = $action -eq 'update-models'
        if ($updating) {
            $answer = [System.Windows.Forms.MessageBox]::Show(
                'Back up the local models and download the latest Front and Back models? The bridge will stop and restart with fresh inventory counts. Failed downloads keep the existing models.',
                'Update Smart Storage TM models', 'YesNo', 'Warning')
            if ($answer -ne 'Yes') { return }
        }
        if ($action -eq 'start' -and $processes.Count -gt 0) { return }
        if (($action -eq 'restart' -or $updating) -and $processes.Count -gt 0) {
            if (!$updating) {
            $answer = [System.Windows.Forms.MessageBox]::Show(
                'Restart the local bridge? Current RAM inventory will be cleared. Keep hands clear while fresh counts are scanned.',
                'Smart Storage bridge', 'YesNo', 'Warning')
            if ($answer -ne 'Yes') { return }
            }
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
        $updateSucceeded = $true
        $updateBackup = ''
        if ($updating) {
            $progress = New-Object System.Windows.Forms.Form
            $progress.Text = 'Updating TM models'
            $progress.Width = 460; $progress.Height = 140
            $progress.StartPosition = 'CenterScreen'; $progress.ControlBox = $false
            $label = New-Object System.Windows.Forms.Label
            $label.Dock = 'Fill'; $label.Padding = New-Object System.Windows.Forms.Padding(16)
            $label.Text = 'Backing up, downloading and verifying Front + Back models. Please wait; the bridge will restart automatically.'
            $progress.Controls.Add($label); $progress.Show()
            try {
                $info = New-Object System.Diagnostics.ProcessStartInfo
                $info.FileName = $pythonPath
                $info.Arguments = '"' + (Join-Path $projectRoot 'deploy\update_tm_models.py') + '"'
                $info.WorkingDirectory = $projectRoot
                $info.UseShellExecute = $false; $info.CreateNoWindow = $true
                $info.RedirectStandardOutput = $true
                $worker = [System.Diagnostics.Process]::Start($info)
                while (!$worker.WaitForExit(200)) { [System.Windows.Forms.Application]::DoEvents() }
                $output = $worker.StandardOutput.ReadToEnd()
                $updateSucceeded = $worker.ExitCode -eq 0
                if ($updateSucceeded) {
                    $result = ($output.Trim() -split "`n")[-1] | ConvertFrom-Json
                    $updateBackup = $result.backup
                }
                $worker.Dispose()
            } catch {
                $updateSucceeded = $false
            } finally { $progress.Close(); $progress.Dispose() }
        }
        $child = Start-Process -FilePath $pythonPath -ArgumentList ('"' + $bridgePath + '"') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
        if ($child.WaitForExit(3000)) {
            throw 'Python exited during startup. Run camera_service.py with the configured Python in a terminal to inspect the error.'
        }
        if ($updating) {
            $message = if ($updateSucceeded) { 'Front and Back models updated and verified. Bridge restarted; keep hands clear for fresh counts. Backup: ' + $updateBackup } else { 'The model update failed. Existing models were retained and the bridge was restarted. Check internet, Node and the local model configuration.' }
            [System.Windows.Forms.MessageBox]::Show($message, 'TM model update', 'OK', $(if ($updateSucceeded) { 'Information' } else { 'Warning' })) | Out-Null
        }
    } finally {
        $mutex.ReleaseMutex()
        $mutex.Dispose()
    }
} catch {
    [System.Windows.Forms.MessageBox]::Show('The bridge launcher could not finish. ' + $_.Exception.Message, 'Smart Storage bridge', 'OK', 'Error') | Out-Null
    exit 1
}
