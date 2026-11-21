"""Exercise the real GET handler with two model sources and an offline cache."""
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time
import types
import unittest
from unittest.mock import Mock
import urllib.parse
import urllib.request


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


if __name__ == '__main__': unittest.main()
