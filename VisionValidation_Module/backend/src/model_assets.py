"""One manifest-verified disk copy of each TM model; optional sources cache in RAM only."""
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import re
import threading


class ModelAssets:
    def __init__(self,manifest=None):
        self.manifest=manifest
        self.cache=OrderedDict();self.lock=threading.Lock()

    def read(self,source,name,fetch):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',name) or name in ('.','..'):
            raise ValueError('Invalid model asset')
        source=source.rstrip('/')+'/'
        manifest=Path(self.manifest or os.getenv('SMART_STORAGE_EDGE_MANIFEST') or
            Path(__file__).resolve().parents[3]/'.runtime/edge-models/manifest.json').resolve()
        if manifest.is_file():
            config=json.loads(manifest.read_text(encoding='utf-8'))
            for role in ('Front','Back'):
                spec=config[role]
                if spec['source'].rstrip('/')+'/'!=source:continue
                entries={'model.json':spec['model'],'metadata.json':spec['metadata']}
                entries.update({item['name'].removeprefix('./'):item for item in spec['weights']})
                if name not in entries:raise ValueError('Model asset not in staged manifest')
                entry=entries[name];path=(manifest.parent/entry['path']).resolve()
                if not path.is_relative_to(manifest.parent):raise ValueError('Invalid staged model path')
                data=path.read_bytes()
                if hashlib.sha256(data).hexdigest()!=entry['sha256']:raise ValueError('Staged model hash mismatch')
                return data
        # Custom diagnostic sources may be fetched, but never create duplicate files.
        key=(source,name)
        try:data=fetch(source,name)
        except (OSError,ValueError):
            with self.lock:
                if key not in self.cache:raise
                return self.cache[key]
        if not isinstance(data,bytes) or len(data)>50_000_000:raise ValueError('Invalid model download')
        with self.lock:
            self.cache[key]=data;self.cache.move_to_end(key)
            while len(self.cache)>16 or sum(map(len,self.cache.values()))>50_000_000:self.cache.popitem(last=False)
        return data
