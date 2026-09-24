#!/usr/bin/env python3
"""Compute operational counts and timings from actual cell receipts."""
from pathlib import Path
import collections
import json
import statistics

ROOT=Path(__file__).resolve().parent

def main():
    arms={arm:{'started':0,'completed':0,'valid':0,'invalid':0,'response_envelopes':0,
               'verdicts':collections.Counter(),'durations_seconds':[]} for arm in ('A','B')}
    by_case={};completed_at=[];failures=[]
    for path in sorted((ROOT/'run').glob('*/*/seat-*/attempt.json')):
        attempt=json.loads(path.read_text());arm=attempt['arm'];target=attempt['target'];seat=attempt['seat'];entry=arms[arm]
        entry['started']+=1;entry['response_envelopes']+=len(list((path.parent/'state/imports').glob('*.json')))
        result=path.parent/'result.json'
        if not result.exists():continue
        result=json.loads(result.read_text());entry['completed']+=1;entry[result['status']]+=1
        duration=result['completed_at_unix']-attempt['started_at_unix'];entry['durations_seconds'].append(duration);completed_at.append(result['completed_at_unix'])
        cell={'status':result['status'],'duration_seconds':round(duration,3)}
        if result['status']=='valid':
            fields=result['projection'].rstrip('\n').split('\t');cell.update({'verdict':fields[1],'major_count':int(fields[2])});entry['verdicts'][fields[1]]+=1
        else:failures.append({'arm':arm,'target':target,'seat':seat,'exception':result.get('exception'),'raw_response_paths':[str(p.relative_to(ROOT)) for p in (path.parent/'state/imports').glob('*.json')]})
        by_case.setdefault(target,{}).setdefault(arm,{})[str(seat)]=cell
    all_durations=[]
    for entry in arms.values():
        values=entry.pop('durations_seconds');all_durations.extend(values)
        entry['format_and_source_validation_rate']=entry['valid']/entry['completed'] if entry['completed'] else None
        entry['duration_seconds']={'min':min(values),'median':statistics.median(values),'max':max(values),'mean':statistics.mean(values)} if values else None
    start=json.loads((ROOT/'run-started.json').read_text())['started_at_unix']
    summary={'counts_by_arm':arms,'cells':by_case,'failures':failures,
      'planned_cells':60,'started_cells':sum(x['started'] for x in arms.values()),
      'completed_cells':sum(x['completed'] for x in arms.values()),
      'valid_votes':sum(x['valid'] for x in arms.values()),'invalid_votes':sum(x['invalid'] for x in arms.values()),
      'wall_clock_seconds_including_control_pauses':max(completed_at)-start if completed_at else None,
      'cell_duration_seconds':{'min':min(all_durations),'median':statistics.median(all_durations),'max':max(all_durations),'mean':statistics.mean(all_durations)} if all_durations else None,
      'interpretation':'Operational validation rate is separate from scientific review quality. Invalid outputs remain excluded from verdict aggregation. Same-model repeated seats do not estimate human reviewer accuracy.'}
    (ROOT/'execution-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,sort_keys=True)+'\n')
    print(json.dumps({k:summary[k] for k in ('started_cells','completed_cells','valid_votes','invalid_votes')},ensure_ascii=False))

if __name__=='__main__':main()
