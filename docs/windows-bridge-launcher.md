# Windows bridge launcher

The dashboard's Start local bridge and Restart local bridge links operate on the
Windows computer running the browser. They do not restart Vercel or a remote bridge.

From the repository root, install once under the Windows account used for browsing:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy\install_bridge_launcher.ps1 -PythonExecutable (python -c "import sys; print(sys.executable)")
```

This registers `smartstorage://` in the current user's registry, using the current
checkout and the specified working Python. The installer checks that Python can
load the bridge before registering it; its path is saved in `.runtime/bridge-launcher.json`.
No administrator account is required.
Reinstall after moving the repository. Existing local environment/model/ROI setup
is still required; the launcher runs the bridge from the repository root with its
normal defaults (port 8008). Keep any custom startup host/port/TLS configuration
on its existing launch path instead.

Open the dashboard and click Start local bridge. Accept the browser's request to
open the Windows launcher, then use Check Bridge to verify actual connectivity.
Launching the link alone does not establish that the bridge or models are healthy.
An existing bridge process for this checkout is left running by Start.

Restart asks for local confirmation, stops this checkout's bridge/supervisor and
their worker processes, then starts the bridge hidden. RAM counts and pending
authority are discarded; keep the rack clear for fresh paired scans. A forced
restart can interrupt an in-flight action, which must not become an accepted
historical transaction. Technical logs remain in the configured local data directory.
Do not combine this launcher with an independently auto-restarting service/task.

Only exact `smartstorage://start` and `smartstorage://restart` links (optional trailing
slash) are accepted. URL parameters, executable paths and shell arguments are
rejected. Any website can request a registered protocol; restart always requires
local confirmation when a bridge is running. No HTTP process-control route is added.

To remove the registration installed by this project:

```powershell
Remove-Item -LiteralPath 'HKCU:\Software\Classes\smartstorage' -Recurse
```

Manual checks after installation: Start while stopped; Start twice (one bridge);
cancel Restart (same run); confirm Restart (new run and fresh counts); ensure an
unrelated Python process remains running. Test browser protocol permission prompts
on the actual browser used for the dashboard.
