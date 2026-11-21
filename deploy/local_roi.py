"""Explicit, local-only ROI migration. No service or cloud writes."""
import argparse
import json
import os
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'VisionValidation_Module/backend'))
from src.local_operations import RoiStore,atomic_json

def main():
    parser=argparse.ArgumentParser();parser.add_argument('operation',choices=['import','export','recover'])
    parser.add_argument('--file');parser.add_argument('--directory',default=os.getenv('SMART_STORAGE_DATA_DIR',str(ROOT/'.runtime')))
    args=parser.parse_args();store=RoiStore(Path(args.directory)/'camera-rois.json')
    if args.operation=='import':
        if not args.file:parser.error('--file required')
        store.import_config(json.loads(Path(args.file).read_text(encoding='utf8')))
    elif args.operation=='export':
        if not args.file:parser.error('--file required')
        atomic_json(args.file,store.load())
    else:
        previous=json.loads(Path(str(store.path)+'.previous').read_text(encoding='utf8'))
        # Recovery validates backup before replacing corrupt current configuration.
        from src.local_operations import validate_rois
        atomic_json(store.path,validate_rois(previous))
    print('Local ROI operation completed. Restart/reaccept service after changes.')
if __name__=='__main__':main()
