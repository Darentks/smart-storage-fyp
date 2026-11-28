"""Exercise the real GET handler with two model sources and an offline cache."""
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import sys
import time
import types
import unittest
from unittest.mock import Mock
import urllib.parse
import urllib.request
sys.path.insert(0,str(Path(__file__).parents[1]))
from src.model_assets import ModelAssets


class ProxyTests(unittest.TestCase):
    def test_relative_weights_keep_their_source_and_offline_cache(self):
        service = Path(__file__).parents[2] / 'camera_service.py'
        tree = ast.parse(service.read_text(encoding='utf-8'))
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'do_GET')
        remote = {'https://models.example/front/': b'front-weights', 'https://models.example/back/': b'back-weights'}
        offline = False
        def fetch(request, timeout):
            if offline: raise OSError('offline')
            url = request.full_url
            base = next(base for base in remote if url.startswith(base))
            data = json.dumps({'weightsManifest':[{'paths':['weights.bin']}]}).encode() if '/model.json?' in url else remote[base]
            return io.BytesIO(data)
        with tempfile.TemporaryDirectory() as directory:
            urllib_fake = types.SimpleNamespace(parse=urllib.parse, request=types.SimpleNamespace(Request=urllib.request.Request, urlopen=fetch))
            ns = dict(urllib=urllib_fake, os=os, json=json, hashlib=hashlib, re=re, time=time,
                      TM_MODEL_ASSETS=ModelAssets(Path(directory)/'manifest.json'),
                      LAST_TM_BASE_URL='https://wrong.example/', __file__=str(Path(directory)/'camera_service.py'))
            exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), '<real handler>', 'exec'),ns)
            def request(base, filename):
                handler = types.SimpleNamespace(path=f'/tm_proxy/source/{urllib.parse.quote(base,safe="")}/123/{filename}',
                    wfile=io.BytesIO(), send_response=Mock(), send_header=Mock(), end_headers=Mock(), _send_cors_headers=Mock(), _send_json=Mock())
                ns['do_GET'](handler)
                handler.send_response.assert_called_once_with(200)
                return handler.wfile.getvalue()
            for base in remote:
                doc = json.loads(request(base, 'model.json'))
                self.assertEqual(doc['weightsManifest'][0]['paths'], ['weights.bin'])
                self.assertEqual(request(base, 'weights.bin'), remote[base])
            offline = True
            for base in remote:
                self.assertEqual(request(base, 'weights.bin'), remote[base])
            self.assertEqual(list(Path(directory).iterdir()),[],'proxy must not recreate disk model caches')

    def test_staged_assets_are_shared_hash_verified_and_never_replaced_by_remote(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config={'version':1}
            for role in ('Front','Back'):
                folder=root/role;folder.mkdir()
                spec={'source':f'https://models.example/{role}/','weights':[]}
                for key,name in (('model','model.json'),('metadata','metadata.json')):
                    data=(role+key).encode();(folder/name).write_bytes(data)
                    spec[key]={'path':f'{role}/{name}','sha256':hashlib.sha256(data).hexdigest()}
                data=role.encode();(folder/'model.weights.bin').write_bytes(data)
                spec['weights']=[{'name':'./model.weights.bin','path':f'{role}/model.weights.bin','sha256':hashlib.sha256(data).hexdigest()}]
                config[role]=spec
            manifest=root/'manifest.json';manifest.write_text(json.dumps(config))
            store=ModelAssets(manifest);fetch=Mock(side_effect=AssertionError('staged source must not fetch online'))
            for role in ('Front','Back'):
                self.assertEqual(store.read(config[role]['source'],'model.json',fetch),(role+'model').encode())
                self.assertEqual(store.read(config[role]['source'],'model.weights.bin',fetch),role.encode())
            (root/'Front/model.json').write_bytes(b'tampered')
            with self.assertRaisesRegex(ValueError,'hash mismatch'):store.read(config['Front']['source'],'model.json',fetch)
            for name in ('../model.json','..','unlisted.bin'):
                with self.assertRaises(ValueError):store.read(config['Back']['source'],name,fetch)
            fetch.assert_not_called()


if __name__ == '__main__': unittest.main()
