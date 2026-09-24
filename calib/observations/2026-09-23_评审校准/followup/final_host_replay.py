#!/usr/bin/env python3
"""Offline revalidation of actual separate-followup outputs under a final host snapshot."""
from pathlib import Path
import hashlib
import json
import shutil
import sys

ROOT=Path(__file__).resolve().parent
REPO=Path('/Users/qinningxu/code/ai-ideas')
FINAL=ROOT/'runtime-final-host'

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def read(path):return json.loads(Path(path).read_text())
def write(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True)+'\n')

def main():
    for relative,digest in read(ROOT/'manifest.json')['frozen_sha256'].items():
        if sha(ROOT/relative)!=digest:raise ValueError('frozen input/control/runtime changed: '+relative)
    if FINAL.exists():raise ValueError('final host snapshot already exists; retain immutable prior replay')
    for folder in ('lib','history','roles'):
      for p in (REPO/folder).glob('*'):
        if p.is_file() and p.suffix in ('.py','.json','.md'):
            q=FINAL/folder/p.name;q.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,q)
    sys.dont_write_bytecode=True
    sys.path.insert(0,str(FINAL))
    from lib import stage_contract,review_assessment,history_runtime
    source_hashes={str(p.relative_to(FINAL)):sha(p) for p in FINAL.rglob('*') if p.is_file()}
    differences=[]
    for rel,digest in source_hashes.items():
        frozen=ROOT/'runtime-B'/rel
        if not frozen.exists() or sha(frozen)!=digest:
            differences.append({'source':rel,'run_frozen_sha256':sha(frozen) if frozen.exists() else None,'final_host_sha256':digest})
    validated=[];skipped=[]
    for target in read(ROOT/'plan.json')['targets']:
      candidate=read(ROOT/target['candidate'])['candidate_markdown']
      prior=(ROOT/target['prior_work']).read_text()
      for arm in ('B',):
       for seat in range(1,4):
        base=ROOT/'run'/arm/target['key']/f'seat-{seat}'
        if not (base/'result.json').exists() or read(base/'result.json')['status']!='valid':
            failed={'target':target['key'],'arm':arm,'seat':seat,'reason':'original vote invalid or missing'}
            imports=list((base/'state/imports').glob('*.json'))
            if len(imports)==1:
                envelope=read(imports[0]);failed['original_response_sha256']=sha(imports[0])
                artifacts=envelope.get('artifacts',[])
                if len(artifacts)==1 and artifacts[0].get('artifact_kind')=='review-markdown':
                    kwargs={} if arm=='A' else {'review_output_version':2,'candidate_markdown':candidate,'prior_work':prior}
                    try:
                        stage_contract.build_review_verdict_from_markdown(artifacts[0]['content'],target['candidate_id'],**kwargs)
                        failed['final_host_review_parse']='accepted; original invalid ballot remains excluded'
                    except Exception as exc:
                        failed['final_host_review_parse']='rejected'
                        failed['final_host_error']=type(exc).__name__+': '+str(exc)
            skipped.append(failed);continue
        result=read(base/'result.json')
        assert sha(base/'output/review.md')==result['review_sha256']
        assert sha(base/'output/verdict.tsv')==result['verdict_sha256']
        imports=list((base/'state/imports').glob('*.json'))
        assert len(imports)==1 and sha(imports[0])==imports[0].stem
        envelope=read(imports[0]);assert len(envelope['artifacts'])==1
        assert envelope['artifacts'][0]['artifact_kind']=='review-markdown'
        assert envelope['artifacts'][0]['content']==(base/'output/review.md').read_text()
        kwargs={} if arm=='A' else {'review_output_version':2,'candidate_markdown':candidate,'prior_work':prior}
        projected=stage_contract.build_review_verdict_from_markdown((base/'output/review.md').read_text(),target['candidate_id'],**kwargs)
        assert projected==result['projection']==(base/'output/verdict.tsv').read_text()
        validated.append({'target':target['key'],'arm':arm,'seat':seat,'original_response_sha256':sha(imports[0]),'review_sha256':result['review_sha256'],'projection_sha256':hashlib.sha256(projected.encode()).hexdigest()})
    aggregate=read(ROOT/'aggregate.json');ranks={'reject':0,'accept-w-rev':1,'strong-accept':2};checked_groups=[]
    for group in aggregate['groups']:
        if group['status']!='complete':continue
        raw_min=min(ranks[x] for x in group['raw_votes']);final_rank=ranks[group['final_verdict']]
        downgraded=raw_min==2 and not group['mechanical_gate_passed']
        args={'final_rank':final_rank,'raw_min':raw_min,'downgraded':downgraded,'overlap':group['prior_work_facts']['overlap']}
        target=next(x for x in read(ROOT/'plan.json')['targets'] if x['key']==group['target'])
        assert history_runtime._prior_work_facts((ROOT/target['prior_work']).read_bytes())==group['prior_work_facts']
        legacy=review_assessment.legacy_category(**args)
        category=legacy if group['arm']=='A' else review_assessment.classify(**args,assessments=group['assessment_records'])
        assert legacy==group['legacy_category_same_votes'] and category==group['category']
        eligibility={**args,'sa_votes':group['raw_votes'].count('strong-accept'),'story_count_after_append':1}
        assert review_assessment.eligible_for_reentry(**eligibility,category=legacy)==group['legacy_eligible_first_submission']
        if group['arm']=='B':
            assert review_assessment.aggregate_coverage(group['assessment_records'])==group['aggregate_coverage']
            assert review_assessment.eligible_for_reentry(**eligibility,category=category)==group['new_eligible_first_submission']
        checked_groups.append({'target':group['target'],'arm':group['arm'],'category':category})
    write(ROOT/'final-host-replay.json',{'status':'verified','provider_calls':0,'provider_outputs_reused_unchanged':True,
        'actual_provider_runtime':'runtime-B is separate followup condition C, as bound by its manifest; not the original A/B and not runtime-final-host',
        'final_host_source_hashes':source_hashes,'differences_from_run_B':differences,'valid_votes_replayed':validated,
        'original_invalid_or_missing_votes_retained':skipped,'aggregate_groups_replayed':checked_groups})
    print(json.dumps({'status':'verified','valid_votes_replayed':len(validated),'retained_invalid_or_missing':len(skipped),'groups_replayed':len(checked_groups),'host_source_files_changed':len(differences),'provider_calls':0}))

if __name__=='__main__':main()
