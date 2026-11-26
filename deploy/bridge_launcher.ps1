param([Parameter(Mandatory=$true)][string]$Uri)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# ---- windows that stay in front of the browser that opened this launcher ----
function Show-LauncherMessage([string]$Text, [string]$Title = 'Smart Storage bridge', [string]$Icon = 'Information', [string]$Buttons = 'OK') {
    $owner = New-Object System.Windows.Forms.Form
    $owner.TopMost = $true; $owner.ShowInTaskbar = $false; $owner.Opacity = 0
    $owner.StartPosition = 'CenterScreen'; $owner.Size = New-Object System.Drawing.Size(1, 1)
    $owner.Show(); $owner.Activate()
    try { return [System.Windows.Forms.MessageBox]::Show($owner, $Text, $Title, $Buttons, $Icon) }
    finally { $owner.Close(); $owner.Dispose() }
}

function New-ProgressWindow([string]$Title, [string]$Text) {
    $form = New-Object System.Windows.Forms.Form
    $form.Text = $Title; $form.Width = 480; $form.Height = 150
    $form.StartPosition = 'CenterScreen'; $form.ControlBox = $false; $form.TopMost = $true
    $label = New-Object System.Windows.Forms.Label
    $label.Dock = 'Fill'; $label.Padding = New-Object System.Windows.Forms.Padding(16); $label.Text = $Text
    $form.Controls.Add($label); $form.Show(); [System.Windows.Forms.Application]::DoEvents()
    return @{ Form = $form; Label = $label }
}

function Set-ProgressText($Progress, [string]$Text) {
    if ($Progress) { $Progress.Label.Text = $Text; [System.Windows.Forms.Application]::DoEvents() }
}

function Close-ProgressWindow($Progress) {
    if ($Progress) { $Progress.Form.Close(); $Progress.Form.Dispose() }
}

# ---- finding this checkout's bridge ----
function Get-UnixSeconds($DateTime) { return ([DateTimeOffset]$DateTime).ToUnixTimeMilliseconds() / 1000.0 }

function Get-RecordedProcess([string]$Path) {
    # The bridge and supervisor record their PID and creation time. Both must match the live process,
    # so a PID reused by an unrelated program is never stopped.
    if (!(Test-Path -LiteralPath $Path)) { return $null }
    try { $record = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json } catch { return $null }
    if (!$record.pid -or $null -eq $record.started_at) { return $null }
    $process = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + [int]$record.pid) -ErrorAction SilentlyContinue
    if (!$process -or $process.Name -notmatch '^python(?:w|\d+(?:\.\d+)?)?\.exe$') { return $null }
    if ([Math]::Abs((Get-UnixSeconds $process.CreationDate) - [double]$record.started_at) -gt 1.5) { return $null }
    return $process
}

function Get-ListeningProcessId([int]$Port) {
    try { return @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -ExpandProperty OwningProcess -Unique) }
    catch { return @() }
}

function Get-BridgeProcesses {
    $found = @{}
    foreach ($path in @($supervisorRecordPath, $bridgeRecordPath)) {
        $process = Get-RecordedProcess $path
        if ($process) { $found[[int]$process.ProcessId] = $process }
    }
    # Fallback for bridges started before process records existed: a Python process whose first script
    # argument is this checkout's bridge, supervisor or launcher wrapper (full path), or a relative
    # camera_service.py that owns the bridge port. Never arbitrary Python processes.
    $portOwners = Get-ListeningProcessId $bridgePort
    foreach ($process in @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(?:w|\d+(?:\.\d+)?)?\.exe$' })) {
        $line = [string]$process.CommandLine
        if ($line -match $commandPattern -or ($line -match $relativePattern -and $portOwners -contains $process.ProcessId)) {
            $found[[int]$process.ProcessId] = $process
        }
    }
    # Supervisors first, so they cannot respawn the bridge while it is being stopped.
    return @($found.Values | Sort-Object @{ Expression = { if ([string]$_.CommandLine -match 'run_bridge\.py') { 0 } else { 1 } } })
}

function Get-BridgeRunId {
    try {
        $status = Invoke-RestMethod -Uri ('http://127.0.0.1:' + $bridgePort + '/edge_status') -TimeoutSec 3 -UseBasicParsing
        return [string]$status.startup_id
    } catch { return $null }
}

function Get-ShortRunId([string]$RunId) { if ($RunId) { return $RunId.Substring(0, [Math]::Min(8, $RunId.Length)) } return 'unknown' }

function Stop-Bridge($Processes, $Progress) {
    Set-ProgressText $Progress 'Stopping the current bridge...'
    foreach ($process in $Processes) {
        $current = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + [int]$process.ProcessId) -ErrorAction SilentlyContinue
        if ($current -and $current.CreationDate -eq $process.CreationDate) {
            # /T includes this bridge's own inference workers; never all Python or Node processes.
            $null = & "$env:SystemRoot\System32\taskkill.exe" /PID $process.ProcessId /T /F 2>&1
        }
    }
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline) {
        $alive = @($Processes | Where-Object {
            $current = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + [int]$_.ProcessId) -ErrorAction SilentlyContinue
            $current -and $current.CreationDate -eq $_.CreationDate })
        if ($alive.Count -eq 0) { return }
        Start-Sleep -Milliseconds 250; [System.Windows.Forms.Application]::DoEvents()
    }
    throw 'The current bridge did not stop within 15 seconds. Close its window or end it in Task Manager, then try again.'
}

function Read-StartupError {
    # The window shows a plain-language reason and the final error line; the full (redacted) traceback
    # stays in the report file instead of filling the window with code lines.
    if (!(Test-Path -LiteralPath $startupLogPath)) { return '' }
    try {
        $lines = @(Get-Content -LiteralPath $startupLogPath | Where-Object { $_.Trim() -and $_ -notmatch '^\s*[\^~]+\s*$' })
        if ($lines.Count -eq 0) { return '' }
        $last = $lines[-1].Trim()
        if ($last.Length -gt 300) { $last = $last.Substring(0, 300) + '...' }
        $reason = if ($last -match 'Another bridge instance owns this deployment') { "`n`nAnother bridge for this project is already running. Use Restart local bridge instead of Start." }
                  elseif ($last -match 'ModuleNotFoundError|ImportError') { "`n`nA Python package is missing for the configured Python. Reinstall the launcher with the Python you use to run the bridge." }
                  elseif ($last -match 'address already in use|WinError 10048') { "`n`nPort $bridgePort is used by another program." }
                  else { '' }
        return $reason + "`n`nError: " + $last + "`n`nFull report (credentials removed): " + $startupLogPath
    } catch { return '' }
}

function Start-Bridge([string]$PreviousRunId, $Progress) {
    # Success means the bridge answers /edge_status with a new run ID, not merely that Python started.
    Set-ProgressText $Progress 'Starting the bridge and waiting for it to answer (up to 60 seconds)...'
    $child = Start-Process -FilePath $pythonPath -ArgumentList ('"' + $wrapperPath + '"') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
    $deadline = (Get-Date).AddSeconds(60)
    $otherAnswered = $false
    while ((Get-Date) -lt $deadline) {
        if ($child.HasExited) {
            return @{ Ok = $false; Message = 'The bridge stopped while starting (exit code ' + $child.ExitCode + ').' + (Read-StartupError) }
        }
        $runId = Get-BridgeRunId
        if ($runId -and $runId -ne $PreviousRunId) {
            $record = $null
            try { $record = Get-Content -LiteralPath $bridgeRecordPath -Raw | ConvertFrom-Json } catch { }
            if (!$record -or [int]$record.pid -eq $child.Id) { return @{ Ok = $true; RunId = $runId } }
            $otherAnswered = $true
        }
        $left = [int]($deadline - (Get-Date)).TotalSeconds
        Set-ProgressText $Progress ('Starting the bridge and waiting for it to answer (' + $left + ' s left)...')
        Start-Sleep -Milliseconds 500; [System.Windows.Forms.Application]::DoEvents()
    }
    if ($otherAnswered) { return @{ Ok = $false; Message = 'A different bridge process is answering on port ' + $bridgePort + '. Close other bridge windows and use Restart.' } }
    return @{ Ok = $false; Message = 'The bridge did not answer within 60 seconds. It may still be starting or may have hung.' + (Read-StartupError) }
}

# ---- TM model update ----
function Invoke-ModelUpdate([string[]]$Arguments, $Progress) {
    # Runs deploy/update_tm_models.py and returns its final JSON result (always an object with ok/outcome).
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $pythonPath
    $info.Arguments = (@('"' + $updaterPath + '"') + @($Arguments | ForEach-Object { '"' + $_ + '"' })) -join ' '
    $info.WorkingDirectory = $projectRoot
    $info.UseShellExecute = $false; $info.CreateNoWindow = $true; $info.RedirectStandardOutput = $true
    try {
        $worker = [System.Diagnostics.Process]::Start($info)
        $output = $worker.StandardOutput.ReadToEndAsync()
        while (!$worker.WaitForExit(200)) { [System.Windows.Forms.Application]::DoEvents() }
        $lines = @($output.Result.Trim() -split "`r?`n" | Where-Object { $_.Trim() })
        $worker.Dispose()
        if ($lines.Count -gt 0) { return ($lines[-1] | ConvertFrom-Json) }
    } catch { }
    return [pscustomobject]@{ ok = $false; outcome = 'failed'; failure = [pscustomobject]@{ stage = 'launcher'; role = $null; message = 'The model updater did not return a result. Check that the configured Python can run deploy\update_tm_models.py.' } }
}

function Format-UpdateFailure($Result) {
    $failure = $Result.failure
    $stages = @{ settings = 'Settings check'; download = 'Download'; check = 'Model check'; install = 'Install'; launcher = 'Launcher' }
    $stage = if ($failure -and $stages.ContainsKey([string]$failure.stage)) { $stages[[string]$failure.stage] } else { 'Update' }
    $role = if ($failure -and $failure.role) { [string]$failure.role + ' model ' } else { '' }
    return $role + $stage + ' failed: ' + [string]$failure.message
}

function Format-ModelRoles($Result) {
    $lines = foreach ($role in @('Front', 'Back')) {
        $row = $Result.roles.$role
        $new = if ($row.new) { ([string]$row.new).Substring(0, 12) } else { 'unknown' }
        if ($row.changed) {
            $old = if ($row.old) { ([string]$row.old).Substring(0, 12) } else { 'none' }
            $role + ': ' + $old + ' -> ' + $new + ' (updated)'
        } else { $role + ': ' + $new + ' (unchanged)' }
    }
    return ($lines -join "`n")
}

function Format-Cleanup($Result) {
    $pending = @($Result.cleanup_pending | Where-Object { $_ })
    if ($pending.Count -eq 0) { return '' }
    return "`n`nLeftover update folders not removed yet (retried on the next update):`n" + ($pending -join "`n")
}

function Wait-LoadedModels([string]$AttemptId, $Progress) {
    # Success needs the restarted bridge to report that the versions it loaded match the installed ones.
    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $deadline) {
        try {
            $status = Invoke-RestMethod -Uri ('http://127.0.0.1:' + $bridgePort + '/edge_status') -TimeoutSec 3 -UseBasicParsing
            $update = $status.model_update
            if ($update -and $update.id -eq $AttemptId -and $update.loaded -and $update.loaded -ne 'pending') { return [string]$update.loaded }
        } catch { }
        $left = [int]($deadline - (Get-Date)).TotalSeconds
        Set-ProgressText $Progress ('Bridge restarted. Waiting for it to load the new models (' + $left + ' s left)...')
        Start-Sleep -Milliseconds 500; [System.Windows.Forms.Application]::DoEvents()
    }
    return 'pending'
}

try {
    # The browser supplies an action only, never executable paths or arguments.
    if ($Uri -cnotmatch '^smartstorage://(start|restart|update-models)/?$') { throw 'Unsupported launcher link.' }
    $action = [regex]::Match($Uri, '^smartstorage://([a-z-]+)').Groups[1].Value
    $projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
    $runtimePath = Join-Path $projectRoot '.runtime'
    $configPath = Join-Path $runtimePath 'bridge-launcher.json'
    if (!(Test-Path -LiteralPath $configPath)) { throw 'Reinstall the launcher with your working Python interpreter.' }
    $pythonPath = (Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json).python
    if (!(Test-Path -LiteralPath $pythonPath)) { throw 'Configured Python is missing. Reinstall the launcher with your working interpreter.' }
    $bridgePath = Join-Path $projectRoot 'VisionValidation_Module\camera_service.py'
    $supervisorPath = Join-Path $projectRoot 'deploy\run_bridge.py'
    $wrapperPath = Join-Path $projectRoot 'deploy\launch_bridge.py'
    $updaterPath = Join-Path $projectRoot 'deploy\update_tm_models.py'
    $bridgeRecordPath = Join-Path $runtimePath 'bridge-process.json'
    $supervisorRecordPath = Join-Path $runtimePath 'bridge-supervisor.json'
    $startupLogPath = Join-Path $runtimePath 'bridge-startup-error.log'
    $bridgePort = 8008
    # Match the script as the first Python argument, not arbitrary command text.
    $scriptPattern = '(?:' + [regex]::Escape($bridgePath) + '|' + [regex]::Escape($supervisorPath) + '|' + [regex]::Escape($wrapperPath) + ')'
    $commandPattern = '^\s*(?:"[^"]+"|\S+)\s+(?:"' + $scriptPattern + '"|' + $scriptPattern + ')(?:\s|$)'
    $relativePattern = '^\s*(?:"[^"]+"|\S+)\s+"?(?:\.[\\/])?(?:VisionValidation_Module[\\/])?camera_service\.py"?(?:\s|$)'

    $mutex = [Threading.Mutex]::new($false, 'Local\SmartStorageBridgeLauncher')
    if (!$mutex.WaitOne(0)) {
        Show-LauncherMessage 'Another Smart Storage launcher action is already open (a confirmation, progress or result window). Finish or close it, then try again.' 'Smart Storage bridge' 'Warning' | Out-Null
        return
    }
    $progress = $null
    try {
        # @() keeps a single result a list: PowerShell unwraps one-item function results, and a lone
        # process object has no Count, which once made Start launch a second bridge.
        $processes = @(Get-BridgeProcesses)
        $updating = $action -eq 'update-models'
        $answeringRunId = Get-BridgeRunId
        if ($processes.Count -eq 0 -and $answeringRunId) {
            # Something answers on the bridge port but is not a process this launcher can identify safely
            # (another checkout, or a different Python). Never start a second bridge or stop an unknown one.
            Show-LauncherMessage ('A bridge is already answering on port ' + $bridgePort + ' (run ' + (Get-ShortRunId $answeringRunId) + '), but it was not started from this project folder, so the launcher will not ' + $(if ($action -eq 'start') { 'start a second one' } else { 'stop it' }) + '. Close that bridge window, then try again.') 'Smart Storage bridge' 'Warning' | Out-Null
            return
        }
        if ($action -eq 'start' -and $processes.Count -gt 0) {
            $runId = $answeringRunId
            $text = if ($runId) { 'The bridge is already running and answering (run ' + (Get-ShortRunId $runId) + '). Nothing was changed. Use Restart local bridge to restart it.' }
                    else { 'A bridge process is running but is not answering on port ' + $bridgePort + '. Use Restart local bridge to replace it.' }
            Show-LauncherMessage $text 'Smart Storage bridge' $(if ($runId) { 'Information' } else { 'Warning' }) | Out-Null
            return
        }
        $previousRunId = $answeringRunId
        if ($updating) {
            $answer = Show-LauncherMessage ('Check for newer Front and Back models?' + "`n`n" + 'They are downloaded and checked while the bridge keeps running. The bridge restarts (clearing RAM counts) only if a model changed. A failed download keeps the current models.') 'Update Smart Storage TM models' 'Question' 'YesNo'
            if ($answer -ne 'Yes') { return }
            $attempt = [guid]::NewGuid().ToString()
            $progress = New-ProgressWindow 'Updating TM models' 'Downloading and checking the Front and Back models. The bridge keeps running...'
            $prepared = Invoke-ModelUpdate @('prepare', $attempt) $progress
            if (!$prepared.ok) {
                Close-ProgressWindow $progress; $progress = $null
                Show-LauncherMessage ((Format-UpdateFailure $prepared) + "`n`nThe current models were kept and the bridge was not restarted." + (Format-Cleanup $prepared)) 'TM model update' 'Error' | Out-Null
                return
            }
            if ($prepared.outcome -eq 'unchanged') {
                Close-ProgressWindow $progress; $progress = $null
                Show-LauncherMessage ('Already up to date. Both models match the links in deploy	m-model-sources.json.' + "`n`n" + (Format-ModelRoles $prepared) + "`n`nThe bridge was not restarted and counts are unchanged." + (Format-Cleanup $prepared)) 'TM model update' 'Information' | Out-Null
                return
            }
            Set-ProgressText $progress 'New models checked. Stopping the bridge to install them...'
            if ($processes.Count -gt 0) { Stop-Bridge $processes $progress }
            Set-ProgressText $progress 'Backing up the current models and installing the new ones...'
            $installed = Invoke-ModelUpdate @('install', [string]$prepared.candidate, $attempt) $progress
            $started = Start-Bridge $previousRunId $progress
            $loaded = if ($installed.ok -and $started.Ok) { Wait-LoadedModels $attempt $progress } else { 'pending' }
            Close-ProgressWindow $progress; $progress = $null
            $bridgeText = if ($started.Ok) { 'Bridge restarted (run ' + (Get-ShortRunId $started.RunId) + ').' } else { 'The bridge is not running correctly. ' + $started.Message }
            if (!$installed.ok) {
                Show-LauncherMessage ((Format-UpdateFailure $installed) + "`n`nThe current models were kept.`n`n" + $bridgeText) 'TM model update' 'Error' | Out-Null
            } elseif (!$started.Ok) {
                Show-LauncherMessage ('The new models were installed (backup: ' + $installed.backup + '), but ' + $bridgeText) 'TM model update' 'Error' | Out-Null
            } elseif ($loaded -eq 'verified') {
                Show-LauncherMessage ('Models updated and verified: the restarted bridge loaded the new versions.' + "`n`n" + (Format-ModelRoles $installed) + "`n`n" + $bridgeText + ' Backup: ' + $installed.backup + '. Keep hands clear while the shelves are counted.' + (Format-Cleanup $installed)) 'TM model update' 'Information' | Out-Null
            } elseif ($loaded -eq 'mismatch') {
                Show-LauncherMessage ('The models were installed, but the restarted bridge loaded different versions than expected. Do not rely on its counts; restart it once more or run the update again.' + "`n`n" + (Format-ModelRoles $installed) + "`n`nBackup: " + $installed.backup) 'TM model update' 'Error' | Out-Null
            } else {
                Show-LauncherMessage ('The models were installed and the bridge restarted, but it had not finished loading them after 2 minutes. Check "Loaded TM versions" on the dashboard.' + "`n`n" + (Format-ModelRoles $installed) + "`n`nBackup: " + $installed.backup) 'TM model update' 'Warning' | Out-Null
            }
            return
        }
        if ($action -eq 'restart' -and $processes.Count -gt 0) {
            $answer = Show-LauncherMessage 'Restart the local bridge? Current RAM inventory will be cleared. Keep hands clear while fresh counts are scanned.' 'Smart Storage bridge' 'Warning' 'YesNo'
            if ($answer -ne 'Yes') { return }
        }
        $progress = New-ProgressWindow 'Smart Storage bridge' 'Preparing...'
        if ($processes.Count -gt 0) { Stop-Bridge $processes $progress }
        $started = Start-Bridge $previousRunId $progress
        Close-ProgressWindow $progress; $progress = $null
        $restarted = $processes.Count -gt 0
        if ($started.Ok) {
            $bridgeText = 'Bridge ' + $(if ($restarted) { 'restarted' } else { 'started' }) + ' and answering (run ' + (Get-ShortRunId $started.RunId) + $(if ($previousRunId) { ', previous run ' + (Get-ShortRunId $previousRunId) } else { '' }) + ').'
            Show-LauncherMessage ($bridgeText + $(if ($restarted) { ' RAM counts were cleared; keep hands clear while the shelves are counted.' } else { '' })) 'Smart Storage bridge' 'Information' | Out-Null
        } else {
            Show-LauncherMessage ('The bridge is not running correctly. ' + $started.Message) 'Smart Storage bridge' 'Error' | Out-Null
        }
    } finally {
        Close-ProgressWindow $progress
        $mutex.ReleaseMutex()
        $mutex.Dispose()
    }
} catch {
    Show-LauncherMessage ('The bridge launcher could not finish. ' + $_.Exception.Message) 'Smart Storage bridge' 'Error' | Out-Null
    exit 1
}
