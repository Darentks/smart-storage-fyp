"""Bridge launcher support: process records, redacted startup errors, wrapper and PowerShell matching.
No bridge is started or stopped; a throwaway Python child stands in for it."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import importlib.util

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'VisionValidation_Module/backend'))
from src.bridge_runtime import redact, write_process_record, remove_process_record, process_started_at, write_startup_error

LAUNCHER=(ROOT/'deploy/bridge_launcher.ps1').read_text(encoding='utf-8')
POWERSHELL=shutil.which('powershell.exe') if os.name=='nt' else None


def load_wrapper():
    spec=importlib.util.spec_from_file_location('launch_bridge',ROOT/'deploy/launch_bridge.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


class RedactionTests(unittest.TestCase):
    def test_credentials_tokens_and_camera_urls_are_removed(self):
        text=('rtsp://admin:hunter2@192.168.0.50:554/stream1 failed; http://user:pw@host/x; '
              'Authorization: Bearer abc.def-123; SUPABASE_SERVICE_ROLE_KEY=eyJhbGciOi.eyJyb2xlIjoi.c2lnbmF0dXJl; '
              'password="s3cret" token: zzz api_key=K1')
        clean=redact(text)
        for secret in ('hunter2','admin','192.168.0.50','pw@','abc.def-123','eyJhbGciOi','s3cret','zzz','K1'):
            self.assertNotIn(secret,clean,secret)
        self.assertIn('rtsp://[redacted]',clean);self.assertIn('http://[redacted]@host/x',clean)

    def test_ordinary_diagnostics_survive(self):
        line='RuntimeError: Another bridge instance owns this deployment (port 8008, SHELF_R1_C1)'
        self.assertEqual(redact(line),line)

    def test_report_is_bounded_and_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'error.log'
            write_startup_error(path,'x'*10000+' password=abc')
            first=path.read_text(encoding='utf-8');self.assertLess(len(first),4200);self.assertNotIn('abc',first)
            write_startup_error(path,'second');self.assertNotIn('xxxx',path.read_text(encoding='utf-8'))


class ProcessRecordTests(unittest.TestCase):
    def test_record_names_this_process_and_is_removed_only_by_its_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'bridge-process.json'
            self.assertTrue(write_process_record(path,'bridge',startup_id='run-1',port=8008))
            record=json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual((record['role'],record['pid'],record['startup_id'],record['port']),('bridge',os.getpid(),'run-1',8008))
            if os.name=='nt':self.assertLessEqual(record['started_at'],time.time()+1)
            path.write_text(json.dumps(dict(record,pid=record['pid']+1)),encoding='utf-8')
            remove_process_record(path);self.assertTrue(path.exists(),'another process record is left alone')
            path.write_text(json.dumps(record),encoding='utf-8');remove_process_record(path);self.assertFalse(path.exists())

    def test_unwritable_location_never_raises(self):
        with tempfile.TemporaryDirectory() as folder:
            blocker=Path(folder)/'file';blocker.write_text('x')
            self.assertFalse(write_process_record(blocker/'child/record.json','bridge'))

    def test_bridge_and_supervisor_write_and_remove_their_records(self):
        service=(ROOT/'VisionValidation_Module/camera_service.py').read_text(encoding='utf-8')
        self.assertRegex(service,r"write_process_record\(process_record,'bridge',startup_id=READINESS\.run_id,port=port\)")
        self.assertIn('remove_process_record(process_record)',service)
        supervisor=(ROOT/'deploy/run_bridge.py').read_text(encoding='utf-8')
        self.assertIn("write_process_record(RECORD,'supervisor')",supervisor);self.assertIn('remove_process_record(RECORD)',supervisor)


class WrapperTests(unittest.TestCase):
    def run_fake(self,body):
        wrapper=load_wrapper()
        with tempfile.TemporaryDirectory() as folder:
            bridge=Path(folder)/'fake_bridge.py';bridge.write_text(textwrap.dedent(body),encoding='utf-8')
            log=Path(folder)/'bridge-startup-error.log';log.write_text('stale report')
            argv=list(sys.argv)
            try:code=wrapper.main(bridge,log)
            finally:sys.argv=argv
            return code,(log.read_text(encoding='utf-8') if log.exists() else None)

    def test_crash_is_reported_redacted(self):
        code,report=self.run_fake("raise RuntimeError('cannot open rtsp://admin:pw@10.0.0.9/live with password=letmein')")
        self.assertEqual(code,1);self.assertIn('RuntimeError',report)
        for secret in ('admin','10.0.0.9','letmein'):self.assertNotIn(secret,report)

    def test_ordinary_stop_leaves_no_report(self):
        for body in ('raise KeyboardInterrupt()','raise SystemExit(0)','x=1'):
            self.assertEqual(self.run_fake(body),(0,None),body)

    def test_nonzero_exit_is_reported(self):
        code,report=self.run_fake('raise SystemExit(3)')
        self.assertEqual(code,1);self.assertIn('Exit code 3',report)

    def test_runs_the_script_as_main(self):
        code,report=self.run_fake("if __name__!='__main__':raise RuntimeError('not main')")
        self.assertEqual((code,report),(0,None))


class LauncherSourceTests(unittest.TestCase):
    def test_success_requires_a_new_answering_run_not_three_seconds_of_life(self):
        self.assertNotIn('WaitForExit(3000)',LAUNCHER)
        self.assertIn("$runId -and $runId -ne $PreviousRunId",LAUNCHER)
        self.assertIn('AddSeconds(60)',LAUNCHER)
        self.assertIn('/edge_status',LAUNCHER)

    def test_every_result_and_the_busy_case_use_a_topmost_window(self):
        self.assertIn('$owner.TopMost = $true',LAUNCHER)
        self.assertNotRegex(LAUNCHER,r'\[System\.Windows\.Forms\.MessageBox\]::Show\(\s*[\'"$](?!owner)','every message box has the topmost owner')
        self.assertIn('Another Smart Storage launcher action is already open',LAUNCHER)
        self.assertNotRegex(LAUNCHER,r'if \(!\$mutex\.WaitOne\(0\)\) \{ return \}','a busy launcher is never silent')

    def test_relative_launch_is_matched_only_with_the_bridge_port(self):
        pattern=re.search(r"\$relativePattern = '([^']+)'",LAUNCHER).group(1)
        for line in ('python VisionValidation_Module/camera_service.py','"C:\\Py\\python.exe" VisionValidation_Module\\camera_service.py --port 8008',
                     'python camera_service.py','python .\\camera_service.py'):
            self.assertRegex(line,pattern)
        for line in ('python other/camera_service.py.bak','python -c "import camera_service.py"','python my_camera_service.py'):
            self.assertNotRegex(line,pattern)
        self.assertIn('$portOwners -contains $process.ProcessId',LAUNCHER)


@unittest.skipUnless(POWERSHELL,'Windows PowerShell is required')
class PowerShellTests(unittest.TestCase):
    def powershell(self,script):
        return subprocess.run([POWERSHELL,'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-Command',script],
            capture_output=True,text=True,timeout=60)

    def test_launcher_parses(self):
        path=str(ROOT/'deploy/bridge_launcher.ps1').replace("'","''")
        result=self.powershell(f"$e=$null;$null=[System.Management.Automation.Language.Parser]::ParseFile('{path}',[ref]$null,[ref]$e);$e.Count")
        self.assertEqual(result.stdout.strip(),'0',result.stdout+result.stderr)

    def load_functions(self):
        launcher=str(ROOT/'deploy/bridge_launcher.ps1').replace("'","''")
        return ("$t=$null;$e=$null;$ast=[System.Management.Automation.Language.Parser]::ParseFile('"+launcher+"',[ref]$t,[ref]$e);"
                "foreach($f in $ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]},$true)){"
                ". ([scriptblock]::Create($f.Extent.Text))};")

    def test_a_single_running_bridge_counts_as_one(self):
        # Regression: PowerShell unwraps a one-item function result and a lone process object has no Count,
        # so Start once saw "0 bridges" and launched a second bridge into the instance lock.
        self.assertIn('$processes = @(Get-BridgeProcesses)',LAUNCHER)
        with tempfile.TemporaryDirectory() as folder:
            fake=Path(folder)/'camera_service.py';fake.write_text('import time\ntime.sleep(60)\n',encoding='utf-8')
            child=subprocess.Popen([sys.executable,str(fake)])
            try:
                time.sleep(.5)
                setup=("$bridgePath='"+str(fake).replace("'","''")+"';$supervisorPath='"+str(Path(folder)/'run_bridge.py')+"';"
                       "$wrapperPath='"+str(Path(folder)/'launch_bridge.py')+"';$bridgeRecordPath='"+str(Path(folder)/'none.json')+"';"
                       "$supervisorRecordPath=$bridgeRecordPath;$bridgePort=1;"
                       "$scriptPattern='(?:'+[regex]::Escape($bridgePath)+'|'+[regex]::Escape($supervisorPath)+'|'+[regex]::Escape($wrapperPath)+')';"
                       "$commandPattern='^\\s*(?:\"[^\"]+\"|\\S+)\\s+(?:\"'+$scriptPattern+'\"|'+$scriptPattern+')(?:\\s|$)';$relativePattern='^$';")
                result=self.powershell(self.load_functions()+setup+"$p=@(Get-BridgeProcesses);[string]$p.Count+' '+$p[0].ProcessId")
                self.assertEqual(result.stdout.strip(),f'1 {child.pid}',result.stdout+result.stderr)
            finally:
                child.kill();child.wait()

    def test_error_window_shows_a_reason_and_the_last_line_not_the_traceback(self):
        with tempfile.TemporaryDirectory() as folder:
            log=Path(folder)/'bridge-startup-error.log'
            log.write_text('2026-11-26 bridge stopped with an error\nTraceback (most recent call last):\n  File "x.py", line 1\n    ^^^^^^^^^^\n'
                           'RuntimeError: Another bridge instance owns this deployment\n',encoding='utf-8')
            setup="$startupLogPath='"+str(log).replace("'","''")+"';$bridgePort=8008;"
            text=self.powershell(self.load_functions()+setup+'Read-StartupError').stdout
            self.assertIn('Another bridge for this project is already running. Use Restart local bridge',text)
            self.assertIn('Error: RuntimeError: Another bridge instance owns this deployment',text)
            self.assertNotIn('^^^',text);self.assertNotIn('Traceback',text);self.assertIn('Full report',text)

    def test_model_update_messages_name_role_stage_and_versions(self):
        result=json.dumps({'ok':True,'outcome':'installed','roles':{'Front':{'old':'1'*64,'new':'2'*64,'changed':True},'Back':{'old':'3'*64,'new':'3'*64,'changed':False}},
                           'cleanup_pending':['model-update-x: locked (for example by OneDrive); will retry on the next update']})
        failed=json.dumps({'ok':False,'outcome':'failed','failure':{'stage':'download','role':'Back','message':'HTTP 404 for model.json'}})
        script=(self.load_functions()+"$r='"+result+"'|ConvertFrom-Json;$f='"+failed+"'|ConvertFrom-Json;"
                "Format-ModelRoles $r;'---';Format-UpdateFailure $f;'---';Format-Cleanup $r")
        text=self.powershell(script).stdout
        self.assertIn('Front: 111111111111 -> 222222222222 (updated)',text);self.assertIn('Back: 333333333333 (unchanged)',text)
        self.assertIn('Back model Download failed: HTTP 404 for model.json',text)
        self.assertIn('model-update-x: locked',text)

    def test_recorded_process_matches_pid_and_creation_time(self):
        # A real Python child writes its own record; the launcher's own functions must find it, and must
        # reject the same PID once the recorded creation time no longer matches (a reused PID).
        with tempfile.TemporaryDirectory() as folder:
            record=Path(folder)/'bridge-process.json'
            child=subprocess.Popen([sys.executable,'-c',
                'import sys,time;sys.path.insert(0,sys.argv[1]);from src.bridge_runtime import write_process_record;'
                'write_process_record(sys.argv[2],"bridge");time.sleep(60)',str(ROOT/'VisionValidation_Module/backend'),str(record)])
            try:
                for _ in range(100):
                    if record.exists():break
                    time.sleep(.1)
                launcher=str(ROOT/'deploy/bridge_launcher.ps1').replace("'","''")
                load=("$t=$null;$e=$null;$ast=[System.Management.Automation.Language.Parser]::ParseFile('"+launcher+"',[ref]$t,[ref]$e);"
                      "foreach($f in $ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]},$true)){"
                      "if($f.Name -in 'Get-UnixSeconds','Get-RecordedProcess'){. ([scriptblock]::Create($f.Extent.Text))}};")
                path=str(record).replace("'","''")
                found=self.powershell(load+f"$p=Get-RecordedProcess '{path}';if($p){{$p.ProcessId}}else{{'none'}}")
                self.assertEqual(found.stdout.strip(),str(child.pid),found.stdout+found.stderr)
                data=json.loads(record.read_text(encoding='utf-8'));data['started_at']-=30
                record.write_text(json.dumps(data),encoding='utf-8')
                reused=self.powershell(load+f"$p=Get-RecordedProcess '{path}';if($p){{$p.ProcessId}}else{{'none'}}")
                self.assertEqual(reused.stdout.strip(),'none',reused.stdout+reused.stderr)
            finally:
                child.kill();child.wait()


if __name__=='__main__':unittest.main()
