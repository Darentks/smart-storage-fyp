# Windows bridge launcher

The dashboard's Restart local bridge link (which also starts a stopped bridge) operates on the
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

Open the dashboard and click Restart local bridge. Accept the browser's request to
open the Windows launcher. If no bridge is running it starts one without asking. The
dashboard no longer has a separate Start button; the launcher still accepts
`smartstorage://start` (old bookmarks), which leaves an existing bridge running and says so.

Restart asks for local confirmation, stops this checkout's bridge/supervisor and
their worker processes, then starts the bridge hidden. RAM counts and pending
authority are discarded; keep the rack clear for fresh paired scans. A forced
restart can interrupt an in-flight action, which must not become an accepted
historical transaction. Technical logs remain in the configured local data directory.
Do not combine this launcher with an independently auto-restarting service/task.

How the launcher finds the bridge: the bridge writes `.runtime/bridge-process.json`
(and `deploy/run_bridge.py` writes `.runtime/bridge-supervisor.json`) with its process
ID and creation time. The launcher stops a recorded process only when both still match,
so a reused process ID is never touched. Bridges without a record (started before this
change) are still found by their full script path, or as a relative
`camera_service.py` that owns port 8008. Supervisors are stopped first.

How success is decided: after starting `deploy/launch_bridge.py` (which runs the bridge in
the same process), the launcher waits up to 60 seconds for `/edge_status` on port 8008 to
answer with a new run ID from that process. Only then does it report success, showing the
new and previous run IDs. If the bridge exits or does not answer in time, the result window
says so and shows the end of `.runtime/bridge-startup-error.log`: the latest failure only,
at most about 4 KB, with passwords, tokens, keys and camera source URLs removed.

Every confirmation, progress and result window stays in front of the browser. Only one
launcher action runs at a time; a second click while one is open shows a message instead
of doing nothing.

Only exact `smartstorage://start` and `smartstorage://restart` links (optional trailing
slash), plus `smartstorage://update-models`, are accepted. URL parameters, executable paths and shell arguments are
rejected. Any website can request a registered protocol; restart always requires
local confirmation when a bridge is running. No HTTP process-control route is added.

The dashboard's **Get latest TM models** link also works from the Vercel page when
the launcher is installed on the browser's Windows PC. After a Windows confirmation it runs
`deploy/update_tm_models.py` in two phases:

1. **Prepare (bridge keeps running):** reads the Front and Back links from
   `deploy/tm-model-sources.json` (the only place they are set; each must be a
   `https://teachablemachine.withgoogle.com/models/<id>/` link and the two must differ),
   downloads both exports into a temporary `.runtime/model-update-*` folder (each file is
   tried up to 3 times), checks the class names, and loads both models exactly as the bridge
   does. Each role is then compared file by file with the active models.
   - **Neither changed:** the temporary folder is deleted, the bridge is **not** restarted
     and counts are untouched. The window says "Already up to date".
   - **Failure:** the window names the model (Front or Back) and the part that failed
     (settings check, download or model check). Current models are kept; no restart.
2. **Install (only if a model changed):** the launcher stops the bridge, the updater backs up
   `.runtime/edge-models/` to `edge-models-backup-<time>` and swaps in the checked models. The
   installed pose model is always kept. A swap error restores the previous folder. The bridge is
   then started and the launcher waits up to 2 minutes for it to report the loaded versions.
   Success is reported only when they match the installed versions (old -> new short codes per role).

Each outcome is written to `.runtime/model-update-result.json` and appended to
`.runtime/model-update.log` (last 200 lines, passwords and tokens removed). The bridge reports the
latest result in `/edge_status`, so after **Get latest TM models** the dashboard shows "Launcher
opened - waiting for result" and then the verified result, and the "Loaded TM versions" line also
shows which links the settings file holds.

Leftover `model-update-*` folders from earlier runs are deleted at the start of the next update only
when every file still in them is identical to a file in the active models or a backup; otherwise
they are kept and listed in the result. Backup folders are retained, not automatically deleted.

Existing launcher registration points to the script in this checkout, so this action needs no
reinstall. The link accepts no model URL or command arguments from the website. Changing the
dashboard's browser model URL inputs does not change `deploy/tm-model-sources.json`. Editing that file
changes which models the next update downloads. This does not deploy code or model files to Vercel.

To remove the registration installed by this project:

```powershell
Remove-Item -LiteralPath 'HKCU:\Software\Classes\smartstorage' -Recurse
```

Manual checks after installation: Start while stopped (result shows a run ID); Start twice
(one bridge, "already running"); cancel Restart (same run); confirm Restart (new run ID and
fresh counts); Restart a bridge started from a terminal with
`python VisionValidation_Module/camera_service.py`; click twice quickly (busy message);
ensure an unrelated Python process remains running. Test browser protocol permission prompts
on the actual browser used for the dashboard.
