"""Passive bounded per-process resource measurement. No images or intent history."""
import argparse
import json
import os
from pathlib import Path
import time

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--pid',action='append',type=int)
    parser.add_argument('--seconds',type=float,default=60);parser.add_argument('--interval',type=float,default=5)
    parser.add_argument('--conditions',required=True);parser.add_argument('--output',default='.runtime/phase6-soak.json')
    args=parser.parse_args()
    if not .1<=args.interval<=60 or not 0<args.seconds<=86400:parser.error('interval .1..60 seconds; duration >0..86400 seconds')
    try:import psutil
    except ImportError:parser.error('psutil is required in this local environment for per-process measurement')
    processes={pid:psutil.Process(pid) for pid in args.pid or [os.getpid()]};stats={}
    for pid,process in processes.items():
        process.cpu_percent();stats[str(pid)]={'samples':0,'cpu_percent_sum':0,'cpu_percent_max':0,'rss_bytes_max':0,'unavailable_samples':0,'created_at':process.create_time()}
    start=time.monotonic()
    while time.monotonic()-start<args.seconds:
        time.sleep(min(args.interval,max(0,args.seconds-(time.monotonic()-start))))
        for pid,process in processes.items():
            row=stats[str(pid)]
            try:
                if process.create_time()!=row['created_at']:raise psutil.NoSuchProcess(pid)
                cpu=process.cpu_percent();rss=process.memory_info().rss
                row['samples']+=1;row['cpu_percent_sum']+=cpu;row['cpu_percent_max']=max(row['cpu_percent_max'],cpu);row['rss_bytes_max']=max(row['rss_bytes_max'],rss)
            except (psutil.NoSuchProcess,psutil.AccessDenied):row['unavailable_samples']+=1
    for row in stats.values():row['cpu_percent_mean']=row.pop('cpu_percent_sum')/row['samples'] if row['samples'] else None
    report=dict(version=1,created_at=time.time(),actual_duration_seconds=time.monotonic()-start,conditions=args.conditions,
        target_scope='explicit_processes' if args.pid else 'measurement_tool_only',hardware_acceptance='BLOCKED',
        host={'logical_cpus':psutil.cpu_count(),'physical_cpus':psutil.cpu_count(logical=False),'total_ram_bytes':psutil.virtual_memory().total},processes=stats,
        missed_actions=None,wrong_accepted_updates=None,hold_rate=None,restarts=None,recovery=None,action_latency_ms=None,
        note='Unmeasured action/recovery outcomes are unknown, never zero. CPU uses psutil per-process convention; no acceptance thresholds invented.')
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(report,indent=2))
    print(json.dumps({'duration_seconds':report['actual_duration_seconds'],'processes':len(stats),'output':str(path)}))
if __name__=='__main__':main()
