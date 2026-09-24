#!/usr/bin/env python3
"""Isolated review-only A/B. prepare/seal/check are offline; run calls Codex."""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib

ROOT = Path(__file__).resolve().parent
REPO = Path('/Users/qinningxu/code/ai-ideas')
ROWS = [627, 639, 631, 643, 640, 630]
MODEL = 'gpt-6-astra'
REASONING = 'xhigh'
PROMPT = '''Review the frozen candidate using only the supplied candidate.json,
prior_work.md, review_contract.md and your role. Do not use tools, retrieve
information, or use remembered literature as independent evidence. History is
unavailable. The task concerns review quality; generation quotas and domain-theme
preferences do not constitute review defects. If the supplied packet is explicitly
synthetic, evaluate its stated hypothetical premises conditionally; it makes no
claim about real publications or measured research findings. Candidate predictions
are not evidence. Return exactly the strict host response schema, including its
request_attestation and the single review-markdown artifact. Follow the exact
markdown fields required by the supplied contract. Each field occupies one
nonempty line; count fields contain only digits. No expected verdict is supplied.'''

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def write(path, value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True)+'\n')

def read(path):
    return json.loads(Path(path).read_text())

def tree_hashes(directory):
    return {str(p.relative_to(ROOT)):sha(p) for p in sorted(Path(directory).rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}

def production_hashes():
    result = {}
    for rel in ('ledger.tsv','tmp/ledger.good','.ai-ideas/history.sqlite3',
                '.ai-ideas/history.sqlite3-wal','.ai-ideas/history.sqlite3-shm'):
        p=REPO/rel
        result[rel] = sha(p) if p.is_file() else None
    return result

def wrapper():
    actual=shutil.which('codex')
    if not actual:
        raise ValueError('explicit Codex provider unavailable')
    config_path=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))/'config.toml'
    config=tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    argv=[actual,'-c','web_search="disabled"','-c','sandbox_workspace_write.network_access=false',
          '-c','project_doc_max_bytes=0']
    for feature in ('shell_tool','apps','plugins','browser_use','browser_use_external','computer_use',
                    'in_app_browser','multi_agent','image_generation','workspace_dependencies','hooks'):
        argv.extend(['--disable',feature])
    for name in config.get('mcp_servers',{}):
        if not re.fullmatch(r'[A-Za-z0-9_-]+',name):
            raise ValueError('unhandled MCP configuration name')
        argv.extend(['-c',f'mcp_servers.{name}.enabled=false'])
    directory=ROOT/'bin';directory.mkdir(exist_ok=True)
    path=directory/'codex'
    path.write_text('#!/bin/sh\nexec '+shlex.join(argv)+' "$@"\n');path.chmod(0o700)
    return {'executable':actual,'executable_sha256':sha(Path(actual).resolve()),
            'version':subprocess.check_output([actual,'--version'],text=True).strip(),
            'config_path':str(config_path),'config_sha256':sha(config_path) if config_path.exists() else None,
            'wrapper_argv_prefix':argv,'wrapper_sha256':sha(path)}

def targets():
    return read(ROOT/'plan.json')['targets']

def verify_common(manifest):
    for rel,digest in manifest['frozen_sha256'].items():
        if sha(ROOT/rel)!=digest:
            raise ValueError('frozen artifact changed: '+rel)
    cli=manifest['provider']
    if sha(Path(cli['executable']).resolve())!=cli['executable_sha256']:
        raise ValueError('Codex CLI changed')
    config=Path(cli['config_path'])
    if (sha(config) if config.exists() else None)!=cli['config_sha256']:
        raise ValueError('Codex config changed; no provider launched')

def prepare():
    if (ROOT/'plan.json').exists():
        raise ValueError('prepare is append-only: plan already exists')
    pool=read(ROOT/'pool-provenance.json')
    chosen=[next(x for x in pool if x.get('row_number')==row) for row in ROWS]
    chosen += [{'key':f'probe{n:03d}','candidate_id':'I1','source_type':'synthetic-conditional'} for n in range(25,29)]
    # Only selected cases enter the frozen experimental plan.
    for target in chosen:
        target['candidate']=f'frozen/{target["key"]}/candidate.json'
        target['prior_work']=f'frozen/{target["key"]}/prior_work.md'
    a=ROOT/'arms/A';a.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(ROOT/'baseline/history/review-contract-v1.md',a/'review_contract.md')
    shutil.copyfile(ROOT/'baseline/roles/review.md',ROOT/'runtime-A/roles/review.md')
    shutil.copyfile(ROOT/'baseline/history/review-contract-v1.md',ROOT/'runtime-A/history/review-contract-v1.md')
    (ROOT/'serialized_prompt.txt').write_text(PROMPT)
    expectation={
      'design':'Compare complete judge instruction packages using production bounded mounts; frozen evidence and seat IDs match. No expected labels are mounted.',
      'real_cases':[
        {'row_number':627,'criterion':'Inspect limited, repairable experiment specification; do not presuppose promotion.'},
        {'row_number':639,'criterion':'Inspect limited, repairable experiment specification; do not presuppose promotion.'},
        {'row_number':631,'criterion':'Negative challenge: retain justified rejection where occupation or substantive defects remain.'},
        {'row_number':643,'criterion':'Negative challenge: active experience and delayed-belief prior work must not disappear from the decision.'},
        {'row_number':640,'criterion':'Treat the alleged interaction attribution carefully; a spurious interaction contrast cannot establish the causal claim.'},
        {'row_number':630,'criterion':'A borrowed mechanism still requires a nontrivial target adaptation and independently valuable payoff.'}],
      'synthetic_cases':read(ROOT/'synthetic-expectations.json'),
      'interpretation':'No desired SA rate. Compare actual rationales and severity, not verdict counts alone. Preservation of valid negative findings is required. Three same-model seats measure repeatability, not independent human review.'}
    write(ROOT/'expectations.json',expectation)
    plan={'provider':wrapper(),'model':MODEL,'reasoning':REASONING,'seats':3,'calls_per_arm':30,
          'gate_config':{'min_read':5,'axiom_min_cracks':2},
          'targets':chosen,'production_before':production_hashes(),'prompt_sha256':sha(ROOT/'serialized_prompt.txt'),
          'scope':'review-only; no hunt, history mutation, ledger mutation, network research or expected labels',
          'arm_A_sha256':tree_hashes(ROOT/'arms/A'),
          'runtime_A_sha256':tree_hashes(ROOT/'runtime-A')}
    write(ROOT/'plan.json',plan)
    subprocess.run([sys.executable,__file__,'worker','A','preflight'],check=True)
    print('Offline baseline prepared: 10 cases, 3 seats, 0 provider calls. Await revised runtime/contract seal.')

def seal():
    if (ROOT/'manifest.json').exists():
        raise ValueError('revision is already sealed')
    plan=read(ROOT/'plan.json')
    for rel,digest in read(ROOT/'challenge-freeze.json')['frozen_sha256'].items():
        if sha(ROOT/rel)!=digest:raise ValueError('preregistered challenge changed: '+rel)
    for group in ('arm_A_sha256','runtime_A_sha256'):
        for rel,digest in plan[group].items():
            if sha(ROOT/rel)!=digest:raise ValueError('baseline changed: '+rel)
    destination=ROOT/'runtime-B'
    if destination.exists():raise ValueError('runtime-B already exists')
    for folder in ('lib','history','roles'):
        for p in (REPO/folder).glob('*'):
            if p.is_file() and p.suffix in ('.py','.json','.md'):
                q=destination/folder/p.name;q.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,q)
    b=ROOT/'arms/B';b.mkdir(parents=True)
    shutil.copyfile(REPO/'history/review-contract.md',b/'review_contract.md')
    (b/'review_protocol.json').write_text('{"aggregation_version":2,"review_output_version":2}\n')
    if (b/'review_contract.md').read_bytes()==(ROOT/'arms/A/review_contract.md').read_bytes():
        raise ValueError('revised contract equals baseline')
    frozen={}
    for path in ('runtime-A','runtime-B','arms','bin'):
        frozen.update(tree_hashes(ROOT/path))
    for item in targets():
        for key in ('candidate','prior_work'):frozen[item[key]]=sha(ROOT/item[key])
    for path in ('runner.py','serialized_prompt.txt','expectations.json','plan.json','challenge-freeze.json'):
        frozen[path]=sha(ROOT/path)
    manifest={**plan,'sealed_at_unix':time.time(),'frozen_sha256':frozen,
              'runtime_source':'A: baseline source snapshot; B: revised source snapshot',
              'planned_provider_calls':60,'call_retry_policy':'No automatic retry; failed responses remain archived and have no valid vote.'}
    write(ROOT/'manifest.json',manifest)
    verify_common(manifest)
    subprocess.run([sys.executable,__file__,'worker','B','preflight'],check=True)
    print('Both arms sealed and preflighted; no provider calls. Use run to launch the declared 60 votes.')

def worker(arm,action,key=None,seat=None):
    runtime=ROOT/('runtime-'+arm)
    sys.dont_write_bytecode=True
    sys.path.insert(0,str(runtime))
    from lib import portable_stage,provider_adapters,stage_contract
    os.environ['PATH']=str(ROOT/'bin')+os.pathsep+os.environ['PATH']
    if action=='run':verify_common(read(ROOT/'manifest.json'))
    intent=provider_adapters.resolve_command_intent(
        provider_adapters.load_registry(runtime/'history/provider-adapters-v1.json'),
        'hunt','codex',model=MODEL,reasoning=REASONING,max_output_tokens=16384)
    selected=targets() if action=='preflight' else [next(t for t in targets() if t['key']==key)]
    records=[]
    for target in selected:
      for current_seat in (range(1,4) if action=='preflight' else [int(seat)]):
        base=ROOT/action/arm/target['key']/f'seat-{current_seat}'
        base.mkdir(parents=True,exist_ok=True)
        mark=base/'attempt.json'
        if action=='run':
            with mark.open('x') as stream:
                json.dump({'started_at_unix':time.time(),'arm':arm,'target':target['key'],'seat':current_seat},stream)
        inputs={'candidate.json':ROOT/target['candidate'],'prior_work.md':ROOT/target['prior_work'],
                'review_contract.md':ROOT/'arms'/arm/'review_contract.md'}
        if arm=='B':inputs['review_protocol.json']=ROOT/'arms/B/review_protocol.json'
        prepared=portable_stage.prepare_stage(intent,stage='review',
            seat_id=target['key']+'-seat-'+str(current_seat),serialized_prompt=PROMPT,
            input_paths=inputs,
            output_root=base/'output',state_root=base/'state')
        records.append({'target':target['key'],'seat':current_seat,'preflight_path':prepared['preflight_path'],
                        'exec_budget':prepared['exec_budget']})
        if action=='run':
            try:
                portable_stage.run_stage(prepared,timeout_seconds=1800)
                portable_stage.verify_completion(prepared)
                text=(base/'output/review.md').read_text()
                kwargs={} if arm=='A' else {'review_output_version':2,
                    'candidate_markdown':read(ROOT/target['candidate'])['candidate_markdown'],
                    'prior_work':(ROOT/target['prior_work']).read_text()}
                projected=stage_contract.build_review_verdict_from_markdown(text,target['candidate_id'],**kwargs)
                if projected!=(base/'output/verdict.tsv').read_text():raise ValueError('host projection mismatch')
                write(base/'result.json',{'status':'valid','projection':projected,'completed_at_unix':time.time(),
                      'review_sha256':sha(base/'output/review.md'),'verdict_sha256':sha(base/'output/verdict.tsv')})
            except Exception as exc:
                write(base/'result.json',{'status':'invalid','exception_type':type(exc).__name__,
                                         'exception':str(exc),'completed_at_unix':time.time()})
                raise
    if action=='preflight':write(ROOT/f'preflight-{arm}.json',records)

def run():
    manifest=read(ROOT/'manifest.json');verify_common(manifest)
    start=ROOT/'run-started.json'
    if not start.exists():write(start,{'started_at_unix':time.time(),'production_before':production_hashes()})
    def seat_sequence(seat):
        for index,target in enumerate(targets()):
            order=('A','B') if (index+seat)%2 else ('B','A')
            for arm in order:
                base=ROOT/'run'/arm/target['key']/f'seat-{seat}'
                if (base/'attempt.json').exists():
                    print('Existing attempt retained',arm,target['key'],seat,flush=True);continue
                verify_common(manifest)
                print('Launching',arm,target['key'],'seat',seat,flush=True)
                result=subprocess.run([sys.executable,__file__,'worker',arm,'run',target['key'],str(seat)],
                                      capture_output=True,text=True)
                base.mkdir(parents=True,exist_ok=True)
                (base/'worker.stdout').write_text(result.stdout);(base/'worker.stderr').write_text(result.stderr)
                print('Completed',arm,target['key'],'seat',seat,'exit',result.returncode,flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures=[executor.submit(seat_sequence,s) for s in range(1,4)]
        for future in concurrent.futures.as_completed(futures):future.result()
    summarize()

def summarize():
    manifest=read(ROOT/'manifest.json');verify_common(manifest)
    rows=[];valid=0;invalid=0;missing=0
    for target in targets():
      for arm in ('A','B'):
       for seat in range(1,4):
        path=ROOT/'run'/arm/target['key']/f'seat-{seat}'/'result.json'
        value=read(path) if path.exists() else ({'status':'invalid','exception':'Attempt started but produced no completion/result receipt'}
              if (path.parent/'attempt.json').exists() else {'status':'missing'})
        status=value['status'];valid+=status=='valid';invalid+=status=='invalid';missing+=status=='missing'
        rows.append({'target':target['key'],'row_number':target.get('row_number'),'arm':arm,'seat':seat,**value})
    after=production_hashes();before=read(ROOT/'run-started.json')['production_before']
    result={'valid_votes':valid,'invalid_votes':invalid,'missing_votes':missing,'rows':rows,
            'production_before':before,'production_after':after,'production_unchanged':before==after,
            'note':'Behavioral assessment requires independent reading of the actual reviews; no string test declares success.'}
    write(ROOT/'results.json',result)
    subprocess.run([sys.executable,__file__,'aggregate'],check=True)
    print(json.dumps({k:result[k] for k in ('valid_votes','invalid_votes','missing_votes','production_unchanged')}))

def aggregate():
    sys.dont_write_bytecode=True
    sys.path.insert(0,str(ROOT/'runtime-B'))
    from lib import stage_contract,history_runtime,review_assessment
    manifest=read(ROOT/'manifest.json');verify_common(manifest)
    ranks={'reject':0,'accept-w-rev':1,'strong-accept':2}
    labels={v:k for k,v in ranks.items()};groups=[]
    for target in targets():
      for arm in ('A','B'):
        group={'target':target['key'],'row_number':target.get('row_number'),'arm':arm}
        reviews=[];assessments=[];votes=[]
        for seat in range(1,4):
            base=ROOT/'run'/arm/target['key']/f'seat-{seat}';result=base/'result.json'
            if not result.exists() or read(result)['status']!='valid':continue
            saved=read(result)
            if sha(base/'output/review.md')!=saved['review_sha256'] or sha(base/'output/verdict.tsv')!=saved['verdict_sha256']:
                raise ValueError('completed review changed')
            markdown=(base/'output/review.md').read_text()
            kwargs={} if arm=='A' else {'review_output_version':2,
                    'candidate_markdown':read(ROOT/target['candidate'])['candidate_markdown'],
                    'prior_work':(ROOT/target['prior_work']).read_text()}
            row=stage_contract.build_review_verdict_from_markdown(markdown,target['candidate_id'],**kwargs)
            if row!=saved['projection']:raise ValueError('replayed verdict mismatch')
            parts=row.rstrip('\n').split('\t');votes.append(parts[1]);reviews.append(markdown)
            if arm=='B':assessments.append(json.loads(next(s[len('Assessment:'):].strip()
                                                for s in markdown.splitlines() if s.startswith('Assessment:'))))
        group['raw_votes']=votes
        if len(votes)!=3:
            group['status']='incomplete-no-aggregate';groups.append(group);continue
        facts=history_runtime._prior_work_facts((ROOT/target['prior_work']).read_bytes())
        candidate=read(ROOT/target['candidate'])['candidate_markdown']
        experiments=re.findall(r'(?m)^Minimal Falsification Experiment:[ \t]*(\S.*)$',candidate)
        axiom=bool(re.search(r'(?mi)^Form:[ \t]*remove-load-bearing-assumption[ \t]*$',candidate))
        gates=manifest['gate_config']
        mechanical=facts['papers_read']>=gates['min_read'] and len(experiments)==1 and len(experiments[0].encode())>=30 and (not axiom or facts['supported_cracks']>=gates['axiom_min_cracks'])
        raw_min=min(ranks[v] for v in votes);downgraded=raw_min==2 and not mechanical;final_rank=0 if downgraded else raw_min
        common={'final_rank':final_rank,'raw_min':raw_min,'downgraded':downgraded,'overlap':facts['overlap']}
        legacy=review_assessment.legacy_category(**common)
        category=legacy if arm=='A' else review_assessment.classify(**common,assessments=assessments)
        eligibility={**common,'sa_votes':votes.count('strong-accept'),'story_count_after_append':1}
        group.update({'status':'complete','final_verdict':labels[final_rank],'mechanical_gate_passed':mechanical,
          'prior_work_facts':facts,'aggregate_coverage':None if arm=='A' else review_assessment.aggregate_coverage(assessments),
          'assessment_records':assessments,'category':category,'legacy_category_same_votes':legacy,
          'legacy_eligible_first_submission':review_assessment.eligible_for_reentry(**eligibility,category=legacy),
          'new_eligible_first_submission':None if arm=='A' else review_assessment.eligible_for_reentry(**eligibility,category=category),
          'awr_verdict_intake':labels[final_rank]=='accept-w-rev'})
        groups.append(group)
    write(ROOT/'aggregate.json',{'groups':groups,'story_count_assumption':1,
        'eligibility_scope':'First-submission predicate only. No DB lineage, real resubmission or sidecar state is exercised.',
        'awr_scope':'Verdict intake predicate (hypothetical source=hunt) only; sidecar completion/cooldown/budget state is not exercised.',
        'provider_calls_by_aggregator':0})

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('prepare','seal','check','run','summarize','aggregate','worker'))
    parser.add_argument('args',nargs='*')
    args=parser.parse_args()
    if args.command=='worker':worker(*args.args)
    elif args.command=='check':
        verify_common(read(ROOT/'manifest.json'));print('Frozen identities verified; no model calls.')
    else:globals()[args.command]()
