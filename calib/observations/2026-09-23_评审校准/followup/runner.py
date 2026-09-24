#!/usr/bin/env python3
"""Separate six-cell conditional followup; never appended to the original A/B."""
from pathlib import Path
import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import time
import tomllib

ROOT=Path(__file__).resolve().parent
ORIGINAL=Path('/tmp/judge-revision/calibration')
REPO=Path('/Users/qinningxu/code/ai-ideas')
EXPECTED_CONTRACT='52c94b98fe9f09f407b255832d8fcdfb846e9f06dd29770d2ed4b60b1f33dffe'

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def read(path):return json.loads(Path(path).read_text())
def write(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True)+'\n')

def base_module():
    spec=importlib.util.spec_from_file_location('frozen_ab_runner_reference',ROOT/'ab_runner_reference.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.ROOT=ROOT;module.verify_common=verify
    return module

def config_identity(path):
    raw=Path(path).read_bytes();config=tomllib.loads(raw.decode())
    ignored=[]
    for name,settings in list(config.get('projects',{}).items()):
        if settings=={'trust_level':'trusted'}:
            ignored.append('projects.'+name+'.trust_level');del config['projects'][name]
    if not config.get('projects'):config.pop('projects',None)
    desktop=config.get('desktop',{})
    if type(desktop.get('external-agent-import-sync-enabled')) is bool:
        ignored.append('desktop.external-agent-import-sync-enabled');del desktop['external-agent-import-sync-enabled']
    if not desktop:config.pop('desktop',None)
    digest=hashlib.sha256(json.dumps(config,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'raw_sha256':hashlib.sha256(raw).hexdigest(),'effective_config_sha256':digest,'ignored_metadata_keys':sorted(ignored)}

def verify(manifest):
    for rel,digest in manifest['frozen_sha256'].items():
        if sha(ROOT/rel)!=digest:raise ValueError('frozen artifact changed: '+rel)
    for rel,digest in manifest['original_ab_evidence_sha256'].items():
        if sha(ORIGINAL/rel)!=digest:raise ValueError('original A/B evidence changed: '+rel)
    provider=manifest['provider']
    if sha(Path(provider['executable']).resolve())!=provider['executable_sha256']:raise ValueError('CLI changed')
    current=config_identity(provider['config_path'])
    if current['effective_config_sha256']!=manifest['provider_config_identity']['effective_config_sha256']:
        raise ValueError('effective provider configuration changed; no launch')

def prepare():
    if (ROOT/'plan.json').exists():raise ValueError('already prepared')
    shutil.copyfile(ORIGINAL/'runner.py',ROOT/'ab_runner_reference.py')
    for directory in ('bin','frozen','arms/B'):(ROOT/directory).mkdir(parents=True,exist_ok=True)
    shutil.copyfile(ORIGINAL/'bin/codex',ROOT/'bin/codex');(ROOT/'bin/codex').chmod(0o700)
    source=read(ORIGINAL/'plan.json')
    targets=[target for target in source['targets'] if target['key'] in ('probe025','probe028')]
    inputs={}
    for target in targets:
        for key in ('candidate','prior_work'):
            rel=target[key];(ROOT/rel).parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(ORIGINAL/rel,ROOT/rel);assert sha(ROOT/rel)==sha(ORIGINAL/rel)
            inputs[rel]={'original_path':str(ORIGINAL/rel),'sha256':sha(ROOT/rel)}
    module=base_module();(ROOT/'serialized_prompt.txt').write_text(module.PROMPT)
    assert sha(ROOT/'serialized_prompt.txt')==sha(ORIGINAL/'serialized_prompt.txt')
    write(ROOT/'expectations.json',{
      'purpose':'Check the revised general falsification-scope instruction on two unchanged hypothetical packets after the original A/B revealed a logical omission.',
      'probe028':'Check whether an interaction is necessary for the entire alleged mechanism, including identity-insensitive independent counting. Distinguish a scoped falsification signature from a study stopping threshold. No required verdict.',
      'probe025':'Preserve conditional assessment of substantive unrun work and assess concrete design/scope issues. No required verdict.',
      'unrun_and_repair':'Unrun experiments or absence of a repair alone do not mechanically determine severity or verdict.',
      'interpretation':'A post-observation, targeted followup, not an independent holdout or additional original A/B cells. Expectations are never mounted.'})
    write(ROOT/'plan.json',{'targets':targets,'provider':source['provider'],'model':source['model'],'reasoning':source['reasoning'],
      'gate_config':source['gate_config'],'planned_provider_calls':6,'seats':3,'arm':'B','condition_label':'C-final-active-contract',
      'source_suite':str(ORIGINAL),'original_manifest_sha256':sha(ORIGINAL/'manifest.json'),
      'original_ab_evidence_sha256':{str(p.relative_to(ORIGINAL)):sha(p) for p in [ORIGINAL/'manifest.json',ORIGINAL/'results.json',ORIGINAL/'aggregate.json',*(ORIGINAL/'run').rglob('*')] if p.is_file()},
      'inputs':inputs,'seat_order':{'1':['probe028','probe025'],'2':['probe025','probe028'],'3':['probe028','probe025']},
      'production_before':module.production_hashes(),'retry_policy':'One attempt per declared cell. Invalid output retained; no retry.',
      'separation':'This is a separate followup. B is only the reused runner internal mount label; it is not original A/B condition B.'})
    print('Prepared six cells; no runtime seal or provider calls.')

def seal():
    if (ROOT/'manifest.json').exists():raise ValueError('already sealed')
    if sha(REPO/'history/review-contract.md')!=EXPECTED_CONTRACT:raise ValueError('active contract hash differs from parent freeze')
    subprocess.run([sys.executable,str(ORIGINAL/'runner.py'),'check'],check=True)
    for folder in ('lib','history','roles'):
        for p in (REPO/folder).glob('*'):
            if p.is_file() and p.suffix in ('.py','.json','.md'):
                dest=ROOT/'runtime-B'/folder/p.name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,dest)
    shutil.copyfile(REPO/'history/review-contract.md',ROOT/'arms/B/review_contract.md')
    shutil.copyfile(ORIGINAL/'arms/B/review_protocol.json',ROOT/'arms/B/review_protocol.json')
    plan=read(ROOT/'plan.json');module=base_module()
    frozen={}
    for directory in ('runtime-B','arms','bin','frozen'):frozen.update(module.tree_hashes(ROOT/directory))
    for name in ('runner.py','ab_runner_reference.py','serialized_prompt.txt','plan.json','expectations.json'):frozen[name]=sha(ROOT/name)
    identity=config_identity(plan['provider']['config_path'])
    manifest={**plan,'sealed_at_unix':time.time(),'frozen_sha256':frozen,'contract_sha256':EXPECTED_CONTRACT,
      'provider':{**plan['provider'],'config_sha256':identity['raw_sha256']},
      'original_provider_config_sha256':plan['provider']['config_sha256'],
      'provider_config_identity':identity,
      'effective_profile_proof':'Original sealed runner config guard passed immediately before this seal; identical provider wrapper bytes, explicit model and reasoning. Only trusted project metadata and desktop import-sync boolean are excluded from the semantic configuration hash.'}
    write(ROOT/'manifest.json',manifest);verify(manifest)
    module.worker('B','preflight')
    print('Sealed six offline preflights; zero provider calls. Await explicit parent start confirmation.')

def run():
    manifest=read(ROOT/'manifest.json');verify(manifest);module=base_module()
    start=ROOT/'run-started.json'
    if not start.exists():write(start,{'started_at_unix':time.time(),'production_before':module.production_hashes()})
    def sequence(seat):
        for key in manifest['seat_order'][str(seat)]:
            verify(manifest);dest=ROOT/'run/B'/key/f'seat-{seat}'
            if (dest/'attempt.json').exists():continue
            dest.mkdir(parents=True,exist_ok=True)
            outcome=subprocess.run([sys.executable,__file__,'worker',key,str(seat)],capture_output=True,text=True)
            (dest/'worker.stdout').write_text(outcome.stdout);(dest/'worker.stderr').write_text(outcome.stderr)
            print('Completed separate followup',key,'seat',seat,'exit',outcome.returncode,flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        for future in concurrent.futures.as_completed([executor.submit(sequence,s) for s in (1,2,3)]):future.result()
    summarize()

def summarize():
    manifest=read(ROOT/'manifest.json');verify(manifest);module=base_module();rows=[]
    for target in manifest['targets']:
      for seat in (1,2,3):
        dest=ROOT/'run/B'/target['key']/f'seat-{seat}'
        value=read(dest/'result.json') if (dest/'result.json').exists() else {'status':'invalid' if (dest/'attempt.json').exists() else 'missing'}
        rows.append({'target':target['key'],'seat':seat,'condition':'C-final-active-contract',**value})
    before=read(ROOT/'run-started.json')['production_before'];after=module.production_hashes()
    result={'rows':rows,'valid_votes':sum(x['status']=='valid' for x in rows),'invalid_votes':sum(x['status']=='invalid' for x in rows),
      'missing_votes':sum(x['status']=='missing' for x in rows),'production_before':before,'production_after':after,'production_unchanged':before==after,
      'original_AB_unchanged':True,'separate_followup':True}
    write(ROOT/'results.json',result)
    module.aggregate();aggregate=read(ROOT/'aggregate.json');aggregate['groups']=[x for x in aggregate['groups'] if x['arm']=='B']
    for group in aggregate['groups']:group['condition']='C-final-active-contract'
    aggregate['separate_followup']=True;write(ROOT/'aggregate.json',aggregate)
    print(json.dumps({key:result[key] for key in ('valid_votes','invalid_votes','missing_votes','production_unchanged')}))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=('prepare','seal','check','run','worker','summarize'));parser.add_argument('args',nargs='*');args=parser.parse_args()
    if args.command=='check':verify(read(ROOT/'manifest.json'));print('Frozen followup verified; zero provider calls.')
    elif args.command=='worker':
        manifest=read(ROOT/'manifest.json');verify(manifest)
        cell=ROOT/'run/B'/args.args[0]/('seat-'+args.args[1]);cell.mkdir(parents=True,exist_ok=True)
        receipt=cell/'provider-config-receipt.json'
        with receipt.open('x') as stream:
            json.dump(config_identity(manifest['provider']['config_path']),stream,sort_keys=True,indent=2);stream.write('\n')
        base_module().worker('B','run',*args.args)
    else:globals()[args.command]()
