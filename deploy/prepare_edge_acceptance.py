"""Generate an UNAPPROVED local acceptance template; never activate a processor."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'VisionValidation_Module/backend'))
from src.autonomous_edge import CHECKS, acceptance_code_paths, digest
from src.runtime_inventory import runtime_enabled

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True)
    parser.add_argument('--manifest',required=True)
    parser.add_argument('--report',action='append',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    output=Path(args.output).resolve()
    if output.exists():raise ValueError('Refusing to replace an existing acceptance record')
    config=Path(args.config).resolve();settings=json.loads(config.read_text())
    record=dict(version=1,operator_accepted=False,operator='',accepted_at=0,
        inventory_contract='runtime-v1' if runtime_enabled() else 'persistent-v1',
        manifest_sha256=digest(args.manifest),calibration_sha256=digest(config.parent/settings['calibration_file']),
        code_sha256=hashlib.sha256(b''.join(p.read_bytes() for p in acceptance_code_paths(ROOT))).hexdigest(),
        checks={name:False for name in CHECKS},reports=[])
    for report in args.report:
        path=Path(report).resolve()
        record['reports'].append(dict(path=str(path),sha256=digest(path)))
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf8') as stream:json.dump(record,stream,indent=2)
    print('Unapproved template saved. No mode change or inventory write performed.')

if __name__=='__main__':main()
