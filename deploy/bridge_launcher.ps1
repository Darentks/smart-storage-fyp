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
    # Runs the one updater with fixed flags; values passed to it are hashes or a backup name it produced.
    function Invoke-Updater([string]$Arguments) {
        $info = New-Object System.Diagnostics.ProcessStartInfo
        $info.FileName = $pythonPath
        $info.Arguments = '"' + (Join-Path $projectRoot 'deploy\update_tm_models.py') + '" ' + $Arguments
        $info.WorkingDirectory = $projectRoot
        $info.UseShellExecute = $false; $info.CreateNoWindow = $true
        $info.RedirectStandardOutput = $true
        $worker = [System.Diagnostics.Process]::Start($info)
        $reading = $worker.StandardOutput.ReadToEndAsync()
        while (!$worker.WaitForExit(200)) { [System.Windows.Forms.Application]::DoEvents() }
        $output = $reading.Result
        $worker.Dispose()
        try { return ($output.Trim() -split "`n")[-1] | ConvertFrom-Json } catch { return $null }
    }
    function Start-Bridge {
        return Start-Process -FilePath $pythonPath -ArgumentList ('"' + $bridgePath + '"') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
    }
    function Stop-BridgeTree($Id) {
        $null = & "$env:SystemRoot\System32\taskkill.exe" /PID $Id /T /F 2>&1
    }

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
            # Read the saved links before anything is stopped, so the user confirms exactly what is installed.
            $selected = Invoke-Updater '--show-sources'
            if (!$selected -or !$selected.ok -or $selected.fingerprint -cnotmatch '^[0-9a-f]{64}$') {
                throw 'The saved model links could not be read. Nothing was changed. Save valid Front and Back links on the local dashboard.'
            }
            $origin = if ($selected.origin -eq 'saved') { 'saved on this bridge' } else { 'default links' }
            $prompt = "Install the Front and Back models from these links ($origin)?`n`nFront: $($selected.sources.Front)`nBack:  $($selected.sources.Back)`n`n" +
                'This changes counting for everyone using this bridge. Current models are backed up. The bridge stops, restarts and counts every shelf again; keep hands clear.'
            $answer = [System.Windows.Forms.MessageBox]::Show($prompt, 'Update Smart Storage TM models', 'YesNo', 'Warning', 'Button2')
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
        $update = $null
        if ($updating) {
            $progress = New-Object System.Windows.Forms.Form
            $progress.Text = 'Updating TM models'
            $progress.Width = 460; $progress.Height = 140
            $progress.StartPosition = 'CenterScreen'; $progress.ControlBox = $false
            $label = New-Object System.Windows.Forms.Label
            $label.Dock = 'Fill'; $label.Padding = New-Object System.Windows.Forms.Padding(16)
            $label.Text = 'Backing up, downloading and verifying Front + Back models. Please wait; the bridge will restart automatically.'
            $progress.Controls.Add($label); $progress.Show()
            try { $update = Invoke-Updater ('--expect ' + $selected.fingerprint) }
            finally { $progress.Hide() }
        }
        $child = Start-Bridge
        $exited = $child.WaitForExit(3000)
        if (!$updating) {
            if ($exited) { throw 'Python exited during startup. Run camera_service.py with the configured Python in a terminal to inspect the error.' }
        } else {
            $icon = 'Warning'
            if (!$update -or !$update.ok) {
                # Never claim the old models survived unless the updater compared the files.
                if ($update -and $update.retained) {
                    $message = "The update failed at the '$($update.stage)' step. The installed models and their links are unchanged. Your saved links are still marked as not installed if they differ. Check internet, Node and the links."
                } else {
                    $message = "The update failed and the installed models could NOT be confirmed as unchanged. Do not rely on counts. Keep these recovery copies in the .runtime folder: backup '$($update.backup)', recovery '$($update.recovery)'."
                }
                if ($exited) { $message += ' The bridge also exited during startup.' }
            } else {
                $front = [string]$update.versions.Front.weights_sha256
                $back = [string]$update.versions.Back.weights_sha256
                $state = 'unconfirmed'
                if ($exited) { $state = 'failed' }
                elseif ($front -cmatch '^[0-9a-f]{64}$' -and $back -cmatch '^[0-9a-f]{64}$') {
                    $label.Text = 'Models installed. Waiting for the restarted bridge to report that it loaded them...'
                    $progress.Show()
                    $check = Invoke-Updater ('--confirm-loaded ' + $front + ' ' + $back)
                    $progress.Hide()
                    if ($child.HasExited) { $state = 'failed' } elseif ($check -and $check.ok) { $state = $check.state }
                }
                if ($state -eq 'loaded') {
                    $icon = 'Information'
                    $message = "The restarted bridge loaded the new models: Front $($front.Substring(0,12)), Back $($back.Substring(0,12)). It is counting again; keep hands clear and check every shelf. Backup: $($update.backup)"
                } elseif ($state -eq 'failed' -and [string]$update.backup -cmatch '^edge-models-backup-[0-9-]{1,40}$') {
                    $progress.Show(); $label.Text = 'The new models did not load. Restoring the previous models...'
                    if (!$child.HasExited) { Stop-BridgeTree $child.Id; $null = $child.WaitForExit(10000) }
                    $restore = Invoke-Updater ('--restore ' + $update.backup)
                    $child = Start-Bridge
                    $stopped = $child.WaitForExit(3000)
                    $progress.Hide()
                    if ($restore -and $restore.ok) {
                        $message = "The new models did not load, so the previous models and their links were restored from $($update.backup)."
                        if ($stopped) { $message += ' However the bridge exited during startup; run camera_service.py in a terminal to see why.' }
                        else { $message += ' The bridge restarted; confirm Loaded TM versions on the dashboard and check every shelf.' }
                    } else {
                        $message = "The new models did not load and restoring FAILED. Do not rely on counts. Keep the backup '$($update.backup)' and any model-update folder in .runtime."
                    }
                } else {
                    $message = "Model files were installed, but the bridge did not confirm it loaded them. This is NOT a confirmed success. Check Loaded TM versions on the dashboard before relying on counts. Previous models: $($update.backup)"
                }
            }
            $progress.Dispose()
            [System.Windows.Forms.MessageBox]::Show($message, 'TM model update', 'OK', $icon) | Out-Null
        }
    } finally {
        $mutex.ReleaseMutex()
        $mutex.Dispose()
    }
} catch {
    [System.Windows.Forms.MessageBox]::Show('The bridge launcher could not finish. ' + $_.Exception.Message, 'Smart Storage bridge', 'OK', 'Error') | Out-Null
    exit 1
}
