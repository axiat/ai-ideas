#!/usr/bin/env python3
"""Copy completed calibration evidence without moving or rewriting sources."""
from pathlib import Path
import hashlib
import json
import shutil
import time

ROOT=Path(__file__).resolve().parent
DEST=Path('/Users/qinningxu/code/ai-ideas/calib/observations/2026-09-23_评审校准')

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def read(path):return json.loads(Path(path).read_text())
def write(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True)+'\n')

def main():
    result=read(ROOT/'results.json')
    if sum(result[k] for k in ('valid_votes','invalid_votes','missing_votes'))!=60 or result['missing_votes']:
        raise ValueError('declared suite has not completed all 60 cells')
    if read(ROOT/'final-host-replay.json')['status']!='verified':raise ValueError('final host replay missing')
    copies={}
    def add(source,relative=None):
        relative=Path(relative) if relative is not None else source.relative_to(ROOT)
        if source.is_symlink() or not source.is_file():raise ValueError('source is not a regular file')
        target=DEST/relative;target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():
            if sha(target)!=sha(source):raise ValueError('refuse differing archive overwrite: '+str(relative))
        else:shutil.copyfile(source,target)
        if sha(source)!=sha(target):raise ValueError('copy hash mismatch')
        copies[str(relative)]={'source_path':str(source),'sha256':sha(source),'byte_count':source.stat().st_size}
    for directory in ('run','arms','bin','control-revisions'):
        for source in sorted((ROOT/directory).rglob('*')):
            if source.is_file() and '__pycache__' not in source.parts:add(source)
    for target in read(ROOT/'plan.json')['targets']:
        for key in ('candidate','prior_work'):add(ROOT/target[key])
    for source in sorted((ROOT/'baseline').rglob('*')):
        if source.is_file() and 'lib' not in source.relative_to(ROOT/'baseline').parts:add(source)
    for name in ('runner.py','create_synthetic.py','final_host_replay.py','archive_results.py','execution_summary.py','execution-summary.json',
                 'manifest.json','plan.json','expectations.json','synthetic-expectations.json',
                 'challenge-freeze.json','config-drift-runtime.json','run-started.json','results.json',
                 'aggregate.json','final-host-replay.json','preflight-A.json','preflight-B.json',
                 'input-limitations.md','synthetic-quality-audit.md'):
        source=ROOT/name
        if source.exists():add(source)
    for source in ROOT.glob('config-drift-runtime-*.json'):add(source)
    failed=ROOT/'offline-seal-attempts/config-drift'
    for name in ('manifest.json','rebind-receipt.json','preflight-A.json'):
        if (failed/name).exists():add(failed/name)
    snapshots={}
    for name in ('runtime-A','runtime-B','runtime-final-host'):
        source=ROOT/name
        snapshots[name]={'original_path':str(source),'archive_scope':'review role only; full runtime remains at original path',
                         'source_sha256':{str(p.relative_to(source)):sha(p) for p in sorted(source.rglob('*'))
                            if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}}
        add(source/'roles/review.md')
    receipt={'archived_at_unix':time.time(),'source_root':str(ROOT),'archive_root':str(DEST),
      'operation':'copy only; original data and all embedded paths retained unchanged',
      'copied_files':copies,'runtime_snapshots':snapshots,
      'provider_calls_completed':60,'provider_calls_repeated':0,
      'valid_votes':result['valid_votes'],'invalid_votes':result['invalid_votes'],
      'production_unchanged':result['production_unchanged'],
      'replay_limitation':'This copy is an evidence archive, not a self-contained executable replay. Full runtime snapshots and original absolute paths remain under source_root.',
      'excluded':'Regenerable preflight mirror directories, full runtime source trees, unrelated source-pool packets, temporary raw provider configuration. No existing TPAMI observation was changed.',
      'final_explanation':'Independent outcome review and final interpretation may be added later; the hash list covers the files present at this copy operation.'}
    write(DEST/'archive-receipt.json',receipt)
    hashes={str(p.relative_to(DEST)):sha(p) for p in sorted(DEST.rglob('*'))
            if p.is_file() and p.name!='files-sha256.json'}
    write(DEST/'files-sha256.json',hashes)
    print(json.dumps({'archive':str(DEST),'copied_files':len(copies),'hash_list_sha256':sha(DEST/'files-sha256.json'),
                      'source_files_moved':0,'existing_tpami_changed':False},ensure_ascii=False))

if __name__=='__main__':main()
