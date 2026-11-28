"""Local model-update transaction: prepare while the bridge runs, install only changed models with the bridge
stopped. No online requests or live bridge restart (real Node checks use the local staged bundle only)."""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[3]
spec=importlib.util.spec_from_file_location('tm_model_update',ROOT/'deploy/update_tm_models.py')
update=importlib.util.module_from_spec(spec);spec.loader.exec_module(update)
from src.bridge_runtime import model_update_status, read_model_sources  # noqa: E402

LAUNCHER=(ROOT/'deploy/bridge_launcher.ps1').read_text(encoding='utf-8')
SOURCES={'version':1,'Front':'https://teachablemachine.withgoogle.com/models/frontA/','Back':'https://teachablemachine.withgoogle.com/models/backB/'}


def bundle(folder,tag,back_tag=None):
    folder.mkdir(parents=True)
    manifest={'version':1}
    for role in ('Front','Back'):
        role_tag=back_tag if role=='Back' and back_tag else tag
        (folder/role).mkdir()
        spec={'source':f'https://models.example/{role}/','weights':[]}
        for key,name in (('model','model.json'),('metadata','metadata.json'),('weights','model.weights.bin')):
            data=(role_tag+role+key).encode();(folder/role/name).write_bytes(data)
            entry={'path':f'{role}/{name}','sha256':hashlib.sha256(data).hexdigest()}
            if key=='weights':spec[key]=[dict(entry,name='./model.weights.bin')]
            else:spec[key]=entry
        manifest[role]=spec
    data=(tag+'pose').encode();(folder/'yolo11n-pose.onnx').write_bytes(data)
    manifest['pose']={'path':'yolo11n-pose.onnx','sha256':hashlib.sha256(data).hexdigest()}
    (folder/'manifest.json').write_text(json.dumps(manifest))


def files(folder):
    return {str(p.relative_to(folder)):p.read_bytes() for p in folder.rglob('*') if p.is_file()}


def versions(front,back):
    return {'Front':{'weights_sha256':front*64},'Back':{'weights_sha256':back*64}}


class ModelUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.runtime=self.root/'.runtime';self.active=self.runtime/'edge-models'
        bundle(self.active,'old');self.before=files(self.active)
        (self.root/'deploy').mkdir();(self.root/'deploy/tm-model-sources.json').write_text(json.dumps(SOURCES))
        self.env=patch.dict(update.os.environ,{'SMART_STORAGE_EDGE_MANIFEST':''});self.env.start();self.addCleanup(self.env.stop)

    def patched(self,tag='new',back_tag=None,verify=None):
        def checked(root,manifest,node):
            return verify(root,manifest,node) if verify else (versions('b','b') if 'candidate' in str(manifest) else versions('a','a'))
        return (patch.object(update,'stage',side_effect=lambda root,target,node:bundle(target,tag,back_tag)),
                patch.object(update,'verify',side_effect=checked))

    def run_update(self,**kwargs):
        stage,verify=self.patched(**kwargs)
        with stage,verify:return update.update_models(self.root)

    def backups(self):return list(self.runtime.glob('edge-models-backup-*'))

    def test_success_keeps_backup_replaces_both_tm_models_and_preserves_pose(self):
        result=self.run_update()
        self.assertEqual(result['outcome'],'installed')
        self.assertEqual(files(self.runtime/result['backup']),self.before)
        self.assertEqual((self.active/'Front/model.json').read_bytes(),b'newFrontmodel')
        self.assertEqual((self.active/'Back/model.json').read_bytes(),b'newBackmodel')
        self.assertEqual((self.active/'yolo11n-pose.onnx').read_bytes(),self.before['yolo11n-pose.onnx'])
        self.assertEqual(list(self.runtime.glob('model-update-*')),[])
        self.assertEqual(result['roles']['Front'],dict(old='a'*64,new='b'*64,changed=True,source=SOURCES['Front']))

    def test_unchanged_models_need_no_backup_install_or_restart(self):
        result=self.run_update(tag='old')
        self.assertEqual(result['outcome'],'unchanged');self.assertNotIn('candidate',result)
        self.assertFalse(any(row['changed'] for row in result['roles'].values()))
        self.assertEqual(files(self.active),self.before);self.assertEqual(self.backups(),[])
        self.assertEqual(list(self.runtime.glob('model-update-*')),[])

    def test_roles_are_compared_separately(self):
        result=self.run_update(tag='old',back_tag='new')
        self.assertEqual((result['roles']['Front']['changed'],result['roles']['Back']['changed']),(False,True))
        self.assertEqual(result['outcome'],'installed')
        self.assertEqual((self.active/'Front/model.json').read_bytes(),self.before[str(Path('Front/model.json'))])
        self.assertEqual((self.active/'Back/model.json').read_bytes(),b'newBackmodel')

    def test_prepare_runs_while_the_bridge_runs_and_install_waits_for_it_to_stop(self):
        stage,verify=self.patched()
        with update.InstanceLock(self.runtime/'bridge.lock'),stage,verify:
            prepared=update.prepare(self.root)
            self.assertEqual(prepared['outcome'],'prepared');self.assertTrue((self.runtime/prepared['candidate']).is_dir())
            with self.assertRaises(update.UpdateFailure) as raised:update.install(prepared['candidate'],self.root)
        self.assertEqual(raised.exception.stage,'install');self.assertIn('still running',raised.exception.message)
        self.assertEqual(files(self.active),self.before);self.assertEqual(self.backups(),[])
        installed=update.install(prepared['candidate'],self.root)
        self.assertEqual(installed['outcome'],'installed');self.assertEqual((self.active/'Back/model.json').read_bytes(),b'newBackmodel')

    def test_failed_download_keeps_existing_models_and_makes_no_backup(self):
        for error in (OSError('offline'),update.UpdateFailure('download','HTTP 404 for model.json','Back')):
            with patch.object(update,'stage',side_effect=error):
                with self.assertRaises(type(error)):update.update_models(self.root)
            self.assertEqual(files(self.active),self.before);self.assertEqual(self.backups(),[])
            self.assertEqual(list(self.runtime.glob('model-update-*')),[],'staging is removed after a failure')

    def test_failed_model_validation_does_not_replace_active_files(self):
        def reject(root,manifest,node):
            if 'candidate' in str(manifest):raise update.UpdateFailure('check','Model did not load: MODEL_LABEL_MISMATCH','Front')
            return versions('a','a')
        with self.assertRaises(update.UpdateFailure) as raised:self.run_update(verify=reject)
        self.assertEqual((raised.exception.stage,raised.exception.role),('check','Front'))
        self.assertEqual(files(self.active),self.before)

    def test_failed_promotion_restores_old_active_directory(self):
        rename=Path.rename
        def fail_candidate(source,target):
            if source.name=='candidate':raise OSError('simulated replacement failure')
            return rename(source,target)
        with patch.object(Path,'rename',fail_candidate),self.assertRaises(update.UpdateFailure) as raised:self.run_update()
        self.assertEqual(raised.exception.stage,'install')
        self.assertEqual(files(self.active),self.before)

    def test_rollback_rename_failure_falls_back_to_restoring_a_copy(self):
        rename=Path.rename
        def locked_directory(source,target):
            if source.name in ('candidate','previous'):raise OSError('simulated Windows rename lock')
            return rename(source,target)
        with patch.object(Path,'rename',locked_directory),self.assertRaises(update.UpdateFailure):self.run_update()
        self.assertEqual(files(self.active),self.before)
        self.assertEqual(len(self.backups()),1)

    def test_custom_manifest_is_rejected_instead_of_updating_an_unused_bundle(self):
        with patch.dict(update.os.environ,{'SMART_STORAGE_EDGE_MANIFEST':'other/manifest.json'}):
            with self.assertRaises(update.UpdateFailure):update.update_models(self.root)
        self.assertEqual(files(self.active),self.before)

    def test_cleanup_lock_does_not_turn_a_successful_update_into_a_false_failure(self):
        stage,verify=self.patched()
        with stage,verify,patch.object(update.shutil,'rmtree',side_effect=OSError('temporary antivirus lock')):
            result=update.update_models(self.root)
        self.assertEqual(result['outcome'],'installed');self.assertTrue(result['cleanup_pending'])
        self.assertEqual((self.active/'Front/model.json').read_bytes(),b'newFrontmodel')

    def test_settings_file_is_the_only_source_and_is_validated(self):
        self.assertEqual(update.load_sources(self.root),{'Front':SOURCES['Front'],'Back':SOURCES['Back']})
        cases=[({**SOURCES,'Back':'http://evil.example/models/x/'},'Back'),({**SOURCES,'Back':SOURCES['Front']},None),
               ({**SOURCES,'version':2},None),({**SOURCES,'Front':'https://teachablemachine.withgoogle.com/models/x/../y/'},'Front')]
        for data,role in cases:
            (self.root/'deploy/tm-model-sources.json').write_text(json.dumps(data))
            with self.assertRaises(update.UpdateFailure) as raised:update.load_sources(self.root)
            self.assertEqual((raised.exception.stage,raised.exception.role),('settings',role),data)
            if role:self.assertEqual(raised.exception.message.split(' model')[0],'Please input the correct Teachable Machine link for the '+role)
        (self.root/'deploy/tm-model-sources.json').unlink()
        with self.assertRaises(update.UpdateFailure):update.load_sources(self.root)
        staged=(ROOT/'VisionValidation_Module/edge_service/stage_models.cjs').read_text(encoding='utf-8')
        self.assertNotIn('0cQO4TjOm',staged,'links are no longer hard-coded in the staging script')
        self.assertEqual(read_model_sources(ROOT/'deploy/tm-model-sources.json')['error'],None)

    def test_leftover_previous_copies_are_deleted_only_when_a_backup_matches(self):
        bundle(self.runtime/'edge-models-backup-1','old')
        bundle(self.runtime/'model-update-same/previous','old')
        bundle(self.runtime/'model-update-unique/previous','unique')
        (self.runtime/'model-update-empty').mkdir()
        # Half-deleted by an interrupted cleanup: only the manifest and pose file remain, both backed up.
        bundle(self.runtime/'model-update-partial/previous','old');shutil.rmtree(self.runtime/'model-update-partial/previous/Front')
        pending=update.sweep_staging(self.runtime,self.active)
        self.assertFalse((self.runtime/'model-update-same').exists());self.assertFalse((self.runtime/'model-update-empty').exists())
        self.assertFalse((self.runtime/'model-update-partial').exists())
        self.assertTrue((self.runtime/'model-update-unique/previous/manifest.json').exists())
        self.assertEqual(len(pending),1);self.assertIn('model-update-unique',pending[0])
        with patch.object(update.shutil,'rmtree',side_effect=OSError('locked')):
            bundle(self.runtime/'model-update-locked/previous','old')
            self.assertTrue(any('locked' in row and 'retry' in row for row in update.sweep_staging(self.runtime,self.active)))

    def test_stage_and_verify_failures_name_the_role_and_stage(self):
        report=json.dumps({'ok':False,'role':'Back','stage':'download','reason':'HTTP 404 for model.json (3 attempts at most)'})
        with patch.object(update.subprocess,'run',return_value=subprocess.CompletedProcess([],1,report+'\n','')):
            with self.assertRaises(update.UpdateFailure) as raised:update.stage(self.root,self.root/'x','node')
        self.assertEqual((raised.exception.role,raised.exception.stage),('Back','download'))
        report=json.dumps({'ok':False,'role':'Front','error':'MODEL_LABEL_MISMATCH'})
        with patch.object(update.subprocess,'run',return_value=subprocess.CompletedProcess([],1,report,'')):
            with self.assertRaises(update.UpdateFailure) as raised:update.verify(self.root,self.root/'m.json','node')
        self.assertEqual((raised.exception.role,raised.exception.stage),('Front','check'));self.assertIn('MODEL_LABEL_MISMATCH',raised.exception.message)
        with patch.object(update.subprocess,'run',side_effect=FileNotFoundError()):
            with self.assertRaisesRegex(update.UpdateFailure,'Node.js was not found'):update.stage(self.root,self.root/'x','node')

    def test_main_records_result_and_bounded_redacted_log(self):
        stage,verify=self.patched()
        with patch.object(update,'ROOT',self.root),stage,verify,patch('src.config_loader.find_and_load_env'):
            self.assertEqual(update.main(['prepare','attempt-1']),0)
            result=json.loads((self.runtime/'model-update-result.json').read_text())
            self.assertEqual((result['id'],result['outcome']),('attempt-1','prepared'))
            self.assertEqual(update.main(['install',result['candidate'],'attempt-1']),0)
            self.assertEqual(json.loads((self.runtime/'model-update-result.json').read_text())['outcome'],'installed')
        with patch.object(update,'ROOT',self.root),patch('src.config_loader.find_and_load_env'),\
             patch.object(update,'stage',side_effect=update.UpdateFailure('download','failed via http://user:secret@host/x','Front')):
            self.assertEqual(update.main(['prepare','attempt-2']),1)
        failed=json.loads((self.runtime/'model-update-result.json').read_text())
        self.assertEqual((failed['outcome'],failed['failure']['role'],failed['failure']['stage']),('failed','Front','download'))
        log=(self.runtime/'model-update.log').read_text();self.assertEqual(len(log.splitlines()),3);self.assertNotIn('secret',log)
        self.assertNotIn('secret',json.dumps(failed))

    def test_node_processes_do_not_inherit_cloud_or_camera_secrets(self):
        with patch.dict(update.os.environ,{'SUPABASE_SERVICE_ROLE_KEY':'private','EDGE_CCTV_01_SOURCE':'private','PATH':'node-path'}):
            env=update.node_environment()
        self.assertNotIn('SUPABASE_SERVICE_ROLE_KEY',env);self.assertNotIn('EDGE_CCTV_01_SOURCE',env)
        self.assertIn('PATH',{key.upper() for key in env})

    def test_launcher_accepts_only_fixed_actions_without_url_commands(self):
        rule=re.search(r"\$Uri -cnotmatch '([^']+)'",LAUNCHER).group(1)
        for action in ('start','restart','update-models'):
            self.assertIsNotNone(re.fullmatch(rule,'smartstorage://'+action))
        for value in ('smartstorage://update-models?cmd=whoami','smartstorage://update-models/other','smartstorage://update-models%22 -File other.ps1','https://update-models'):
            self.assertIsNone(re.fullmatch(rule,value))

    def test_launcher_checks_before_stopping_and_confirms_loaded_versions(self):
        branch=LAUNCHER[LAUNCHER.index('        if ($updating) {'):LAUNCHER.index("        if ($action -eq 'restart'")]
        order=[branch.index(token) for token in ("Invoke-ModelUpdate @('prepare'","$prepared.outcome -eq 'unchanged'",'Stop-Bridge $processes',"Invoke-ModelUpdate @('install'",'Start-Bridge','Wait-LoadedModels')]
        self.assertEqual(order,sorted(order),'download and check run before the bridge is stopped; install after')
        unchanged=branch[branch.index("$prepared.outcome -eq 'unchanged'"):branch.index('Stop-Bridge $processes')]
        self.assertIn('return',unchanged,'an unchanged update never restarts the bridge')
        self.assertIn("$loaded -eq 'verified'",branch)


class StatusTests(unittest.TestCase):
    def write(self,folder,result):
        path=Path(folder)/'result.json';path.write_text(json.dumps(result));return path

    def test_loaded_models_are_compared_with_the_installed_ones(self):
        result={'id':'a1','at':1.0,'outcome':'installed','roles':{'Front':{'old':'1'*64,'new':'2'*64,'changed':True},'Back':{'old':'3'*64,'new':'3'*64,'changed':False}},'backup':'edge-models-backup-x'}
        with tempfile.TemporaryDirectory() as folder:
            path=self.write(folder,result)
            self.assertEqual(model_update_status(path,{})['loaded'],'pending')
            self.assertEqual(model_update_status(path,{'Front':{'weights_sha256':'2'*64},'Back':{'weights_sha256':'3'*64}})['loaded'],'verified')
            status=model_update_status(path,{'Front':{'weights_sha256':'1'*64},'Back':{'weights_sha256':'3'*64}})
            self.assertEqual(status['loaded'],'mismatch');self.assertEqual(status['roles']['Front'],dict(old='1'*12,new='2'*12,changed=True))
            path=self.write(folder,{'id':'a2','outcome':'failed','failure':{'stage':'download','role':'Back','message':'token=abc failed'}})
            failed=model_update_status(path,{});self.assertIsNone(failed['loaded']);self.assertEqual(failed['failure']['role'],'Back')
            self.assertNotIn('abc',failed['failure']['message'])
            self.assertIsNone(model_update_status(Path(folder)/'missing.json',{}))

    def test_source_summary_shows_model_ids_or_a_clear_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path=self.write(folder,SOURCES)
            self.assertEqual(read_model_sources(path),dict(file='deploy/tm-model-sources.json',Front='frontA',Back='backB',error=None))
            path=self.write(folder,{**SOURCES,'Front':'https://example.com/'})
            self.assertEqual(read_model_sources(path)['error'],'Please input the correct Teachable Machine link for the Front model')


NODE=shutil.which('node')
LOCAL_BUNDLE=ROOT/'.runtime/edge-models/manifest.json'


@unittest.skipUnless(NODE and LOCAL_BUNDLE.exists() and (ROOT/'VisionValidation_Module/edge_service/node_modules').exists(),'Node, its packages and a staged local bundle are required')
class RealStagingTests(unittest.TestCase):
    """Runs the real staging script and real model check against the local bundle; no network."""
    def test_staging_from_the_local_bundle_and_real_model_check(self):
        with tempfile.TemporaryDirectory() as folder:
            destination=Path(folder)/'candidate'
            result=subprocess.run([NODE,str(ROOT/'VisionValidation_Module/edge_service/stage_models.cjs'),str(destination),'--from-cache'],
                cwd=ROOT,capture_output=True,text=True,timeout=120)
            report=json.loads(result.stdout.strip().splitlines()[-1])
            if not report['ok'] and report.get('reason')=='Local model source differs from the settings file':
                self.skipTest('the staged local bundle came from different links than deploy/tm-model-sources.json')
            self.assertTrue(report['ok'],result.stdout)
            loaded=update.verify(ROOT,destination/'manifest.json',NODE)
            for role in ('Front','Back'):self.assertRegex(loaded[role]['weights_sha256'],r'^[a-f0-9]{64}$')

    def test_invalid_settings_file_reports_settings_stage(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=Path(folder)/'sources.json';settings.write_text(json.dumps({**SOURCES,'Back':'https://example.com/x/'}))
            result=subprocess.run([NODE,str(ROOT/'VisionValidation_Module/edge_service/stage_models.cjs'),str(Path(folder)/'out'),'--sources',str(settings)],
                cwd=ROOT,capture_output=True,text=True,timeout=60)
            report=json.loads(result.stdout.strip().splitlines()[-1])
            self.assertNotEqual(result.returncode,0);self.assertEqual((report['ok'],report['stage'],report['role']),(False,'settings','Back'))
            self.assertTrue(report['reason'].startswith('Please input the correct Teachable Machine link for the Back model'))


if __name__=='__main__':unittest.main()
