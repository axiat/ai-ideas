#!/usr/bin/env python3
"""Summarize and copy the separate followup without editing original A/B files."""
from pathlib import Path
import hashlib
import json
import shutil
import statistics
import time

ROOT=Path(__file__).resolve().parent
DEST=Path('/Users/qinningxu/code/ai-ideas/calib/observations/2026-09-23_评审校准/followup')
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def read(p):return json.loads(Path(p).read_text())
def write(p,d):Path(p).write_text(json.dumps(d,ensure_ascii=False,indent=2,sort_keys=True)+'\n')

def main():
    result=read(ROOT/'results.json')
    assert result['valid_votes']+result['invalid_votes']==6 and not result['missing_votes']
    assert read(ROOT/'final-host-replay.json')['status']=='verified'
    cells=[]
    for path in sorted((ROOT/'run/B').glob('*/seat-*/result.json')):
        outcome=read(path);attempt=read(path.parent/'attempt.json')
        duration=outcome['completed_at_unix']-attempt['started_at_unix']
        cell={'target':attempt['target'],'seat':attempt['seat'],'status':outcome['status'],'duration_seconds':duration}
        if outcome['status']=='valid':
            fields=outcome['projection'].rstrip('\n').split('\t');cell.update({'verdict':fields[1],'major_count':int(fields[2])})
        else:cell['error']=outcome.get('exception')
        cells.append(cell)
    durations=[x['duration_seconds'] for x in cells]
    summary={'condition':'C-final-active-contract','planned_calls':6,'completed_calls':len(cells),'valid_votes':result['valid_votes'],
      'invalid_votes':result['invalid_votes'],'format_and_source_validation_rate':result['valid_votes']/6,
      'cells':cells,'duration_seconds':{'min':min(durations),'median':statistics.median(durations),'max':max(durations)},
      'production_unchanged':result['production_unchanged'],'original_AB_unchanged':result['original_AB_unchanged'],
      'interpretation':'Targeted post-observation followup of two unchanged hypothetical packets. Kept separate from original A/B; no predetermined verdict or independent-holdout claim.'}
    write(ROOT/'execution-summary.json',summary)
    copies={}
    def add(source):
        relative=source.relative_to(ROOT);target=DEST/relative;target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():assert sha(target)==sha(source),'refuse differing existing archive file'
        else:shutil.copyfile(source,target)
        assert sha(target)==sha(source)
        copies[str(relative)]={'source_path':str(source),'sha256':sha(source),'byte_count':source.stat().st_size}
    for folder in ('run','arms','bin','frozen'):
        for p in sorted((ROOT/folder).rglob('*')):
            if p.is_file() and '__pycache__' not in p.parts:add(p)
    for p in sorted(ROOT.glob('*')):
        if p.is_file() and p.suffix in ('.py','.json','.md','.txt'):add(p)
    snapshots={}
    for folder in ('runtime-B','runtime-final-host'):
        directory=ROOT/folder
        snapshots[folder]={'original_path':str(directory),'source_sha256':{str(p.relative_to(directory)):sha(p) for p in sorted(directory.rglob('*')) if p.is_file() and '__pycache__' not in p.parts}}
        add(directory/'roles/review.md')
    receipt={'archived_at_unix':time.time(),'source_root':str(ROOT),'destination':str(DEST),'copied_files':copies,
      'runtime_snapshots':snapshots,'operation':'Copy only; preserve original paths and bytes. Original 60-cell archive evidence is untouched.',
      'replay_limitation':'Full runtime source snapshots remain at original /tmp paths; this evidence copy is not a self-contained executable replay.',
      'top_level_hash_list':'Parent will update the encompassing file hash list after final reports are added.'}
    write(DEST/'archive-receipt.json',receipt)
    print(json.dumps({'archive':str(DEST),'copied_files':len(copies),'valid_votes':result['valid_votes'],'invalid_votes':result['invalid_votes']},ensure_ascii=False))

if __name__=='__main__':main()
