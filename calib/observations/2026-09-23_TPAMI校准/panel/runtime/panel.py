#!/opt/homebrew/bin/python3.11
"""Frozen review adapter. prepare is offline; only run launches providers."""
import argparse
import concurrent.futures
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unicodedata

REPO = pathlib.Path('/Users/qinningxu/code/ai-ideas')
sys.path.insert(0, str(REPO))
from lib import portable_stage, provider_adapters, stage_contract

SOURCES = ('roles/review.md', 'rubric.md', 'brainstorming_policy.md',
           'history/review-contract-v1.md', 'history/provider-adapters-v1.json',
           'lib/portable_stage.py', 'lib/portable_agent.py',
           'lib/provider_adapters.py', 'lib/stage_contract.py')
PROMPT = '''Review this frozen candidate under the supplied review contract. All
inputs are embedded in declared_input_texts. Do not call tools or retrieve any
external information. Judge academic novelty only from prior_work.md; candidate
claims and remembered literature are not independent evidence. If memory suggests
a published counterpart, mention "suspected published counterpart: <name>" inside
Reason, without changing the evidence-based verdict. The candidate is a computer
vision cross-domain probe. Apply the existing Review Calibration gates; generation
theme quotas and embodied-domain selection preferences are not review defects.
Distinguish unavailable packet evidence from supported method defects: use the
inline tags [evidence-incomplete] and [method-limitation] in the relevant assessment
and Reason where applicable. These tags do not change severity or verdict gates.
Return the exact host schema, including its request_attestation. The only artifact
is review-markdown. Use the exact 12 nonempty lines required by the review contract;
every value is single-line, without Markdown list prefixes or extra sections.
History is unavailable. No expected verdict is supplied.'''

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n')

def contract_text():
    return ('''# Frozen review instructions
The current brainstorming_policy.md section Review Calibration is the sole Strong
Accept definition and overrides conflicting calibration in all appended sources.
The current role and strict host schema govern transport: return exactly one
review-markdown artifact. The historical review-verdict-tsv wording below describes
a host projection, never a second model artifact. All substantive gates are retained.
The review markdown has exactly 12 nonempty lines: # <candidate-id>, then Verdict,
CRITICAL, MAJOR, Headline, Occupation, Experiment, Estimand, Payoff, Feasibility,
History, Reason, in that order. Each field uses Label: single-line value.
This frozen evidence protocol replaces live-retrieval instructions for this run.
Assess the adequacy of supplied prior work under the current gates.
\n''' + '\n\n'.join('<!-- Verbatim source: ' + name + ' -->\n' +
        (REPO / name).read_text() for name in
        ('history/review-contract-v1.md', 'brainstorming_policy.md', 'rubric.md')))

def split_candidates(text):
    headings, fence = [], None
    offset = 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if marker:
            run = marker.group(1)
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence):
                fence = None
        elif fence is None:
            match = re.match(r'^## (I[1-9][0-9]*)\s*$', line)
            if match:
                headings.append((offset, match.group(1)))
        offset += len(line)
    if fence or not headings or len({x[1] for x in headings}) != len(headings):
        raise ValueError('ideas.md must have unique exact ## I<n> headings and closed fences')
    preamble = text[:headings[0][0]].strip()
    for index, (start, identifier) in enumerate(headings):
        end = headings[index + 1][0] if index + 1 < len(headings) else len(text)
        yield identifier, (preamble + '\n\n' if preamble else '') + text[start:end].strip() + '\n'

def make_wrapper(runtime):
    actual = shutil.which('codex')
    if actual is None:
        raise ValueError('codex executable missing')
    config_file = pathlib.Path(os.environ.get('CODEX_HOME', str(pathlib.Path.home() / '.codex'))) / 'config.toml'
    config = tomllib.loads(config_file.read_text()) if config_file.exists() else {}
    args = [actual, '-c', 'web_search="disabled"', '-c', 'sandbox_workspace_write.network_access=false',
            '-c', 'project_doc_max_bytes=0']
    for feature in ('shell_tool', 'apps', 'plugins', 'browser_use', 'browser_use_external',
                    'computer_use', 'in_app_browser', 'multi_agent', 'image_generation',
                    'workspace_dependencies', 'hooks'):
        args += ['--disable', feature]
    for name in config.get('mcp_servers', {}):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
            raise ValueError('unsupported MCP configuration key: ' + name)
        args += ['-c', 'mcp_servers.' + name + '.enabled=false']
    directory = runtime / 'bin'
    directory.mkdir()
    wrapper = directory / 'codex'
    wrapper.write_text('#!/bin/sh\nexec ' + shlex.join(args) + ' "$@"\n')
    wrapper.chmod(0o700)
    return wrapper, args

def cli_identity(executable):
    executable = pathlib.Path(executable).resolve()
    config_file = pathlib.Path(os.environ.get('CODEX_HOME', str(pathlib.Path.home() / '.codex'))) / 'config.toml'
    raw = config_file.read_bytes() if config_file.exists() else b''
    config = tomllib.loads(raw.decode()) if raw else {}
    return {'executable_path': str(executable), 'executable_sha256': sha(executable.read_bytes()),
            'config_path': str(config_file.resolve()), 'config_sha256': sha(raw),
            'configured_model': config.get('model'),
            'configured_reasoning': config.get('model_reasoning_effort')}

def activate(manifest):
    runtime = pathlib.Path(manifest['runtime_root'])
    os.environ['PATH'] = str(runtime / 'bin') + os.pathsep + os.environ['PATH']
    return provider_adapters.resolve_command_intent(
        provider_adapters.load_registry(REPO / 'history/provider-adapters-v1.json'),
        'hunt', 'codex', max_output_tokens=16384)

def inputs_for(out, target):
    return {'candidate.json': out / target['candidate'],
            'prior_work.md': out / target['prior_work'],
            'review_contract.md': out / 'frozen/review_contract.md'}

def prepare_one(intent, out, manifest, target, seat, phase):
    root = pathlib.Path(manifest['runtime_root']) / phase / target['key'] / ('seat-' + str(seat))
    return portable_stage.prepare_stage(intent, stage='review',
        seat_id=target['key'] + '-seat-' + str(seat), serialized_prompt=PROMPT,
        input_paths=inputs_for(out, target), output_root=root / 'output', state_root=root / 'state')

def prepare(out, cases):
    out.mkdir(parents=True, exist_ok=False)
    frozen = out / 'frozen'
    frozen.mkdir()
    runtime = pathlib.Path(tempfile.mkdtemp(prefix='blind-panel-')).resolve()
    wrapper, args = make_wrapper(runtime)
    (frozen / 'review_contract.md').write_text(contract_text())
    (frozen / 'review_role.md').write_bytes((REPO / 'roles/review.md').read_bytes())
    (frozen / 'serialized_prompt.txt').write_text(PROMPT)
    manifest = {'reviewers': 3, 'runtime_root': str(runtime), 'targets': [],
                'repo_sha256': {name: sha((REPO / name).read_bytes()) for name in SOURCES},
                'wrapper_sha256': sha(wrapper.read_bytes()), 'codex_wrapper_argv_prefix': args,
                'cli_identity': cli_identity(args[0]),
                'cli_version': subprocess.check_output([args[0], '--version'], text=True).strip(),
                'model': 'inherited CLI default', 'reasoning': 'inherited CLI default',
                'output_token_budget': 16384, 'native_output_token_cap': 'unsupported by Codex',
                'aggregation': 'minimum-vote', 'prompt_sha256': sha(PROMPT.encode())}
    seen = set()
    for case in cases:
        name = case.name
        if not re.fullmatch(r'probe[0-9]+', name) or name in seen:
            raise ValueError('case directory names must be distinct neutral probeNN names')
        seen.add(name)
        target_dir = frozen / name
        target_dir.mkdir()
        for file in ('ideas.md', 'priorwork.md'):
            raw = (case / file).read_bytes()
            if not raw:
                raise ValueError('empty case input: ' + file)
            (target_dir / file).write_bytes(raw)
        for identifier, block in split_candidates((target_dir / 'ideas.md').read_text()):
            candidate = target_dir / (identifier + '.json')
            candidate_value = {'candidate_id': identifier,
                               'candidate_markdown': unicodedata.normalize('NFC', block)}
            candidate.write_bytes(portable_stage._canonical_json_bytes(candidate_value))
            portable_stage._parse_json_artifact(candidate.read_bytes(), 'review_candidate')
            manifest['targets'].append({'key': name + '-' + identifier, 'case': name,
                'candidate_id': identifier, 'candidate': str(candidate.relative_to(out)),
                'prior_work': str((target_dir / 'priorwork.md').relative_to(out))})
    manifest['frozen_sha256'] = {str(p.relative_to(out)): sha(p.read_bytes())
                               for p in sorted(frozen.rglob('*')) if p.is_file()}
    intent = activate(manifest)
    records = []
    for target in manifest['targets']:
        for seat in range(1, 4):
            prepared = prepare_one(intent, out, manifest, target, seat, 'preflight')
            records.append({'target': target['key'], 'seat': seat,
                'preflight_path': prepared['preflight_path'], 'exec_budget': prepared['exec_budget']})
    write_json(out / 'preflight.json', records)
    write_json(out / 'manifest.json', manifest)
    print('Prepared; no model calls. Targets:', len(manifest['targets']), 'planned calls:', len(records))
    print('Manifest:', out / 'manifest.json')

def verify(out, manifest):
    if cli_identity(manifest['codex_wrapper_argv_prefix'][0]) != manifest['cli_identity']:
        raise ValueError('Codex executable or inherited default configuration changed')
    for name, expected in manifest['repo_sha256'].items():
        if sha((REPO / name).read_bytes()) != expected:
            raise ValueError('repository source changed: ' + name)
    for name, expected in manifest['frozen_sha256'].items():
        if sha((out / name).read_bytes()) != expected:
            raise ValueError('frozen input changed: ' + name)
    if sha(PROMPT.encode()) != manifest['prompt_sha256']:
        raise ValueError('adapter prompt changed')
    if sha((pathlib.Path(manifest['runtime_root']) / 'bin/codex').read_bytes()) != manifest['wrapper_sha256']:
        raise ValueError('Codex wrapper changed')

def seat_worker(out_text, manifest, seat):
    out = pathlib.Path(out_text)
    verify(out, manifest)
    intent = activate(manifest)
    for target in manifest['targets']:
        prepared = prepare_one(intent, out, manifest, target, seat, 'run')
        print('Launching', target['key'], 'seat', seat, flush=True)
        portable_stage.run_stage(prepared, timeout_seconds=1800)
        portable_stage.verify_completion(prepared)
        destination = out / 'reviews' / target['key'] / ('seat-' + str(seat))
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(pathlib.Path(prepared['state_root']).parent, destination)
        print('Validated', target['key'], 'seat', seat, flush=True)

def run(out):
    manifest = json.loads((out / 'manifest.json').read_text())
    verify(out, manifest)
    marker = out / 'run-started.json'
    with marker.open('x') as stream:
        json.dump({'planned_calls': len(manifest['targets']) * 3}, stream)
    with concurrent.futures.ProcessPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(seat_worker, str(out), manifest, seat) for seat in range(1, 4)]
        for future in concurrent.futures.as_completed(futures):
            future.result()
    rows = []
    rank = {'reject': 0, 'accept-w-rev': 1, 'strong-accept': 2}
    for target in manifest['targets']:
        votes = []
        for seat in range(1, 4):
            base = out / 'reviews' / target['key'] / ('seat-' + str(seat)) / 'output'
            projected = stage_contract.build_review_verdict_from_markdown(
                (base / 'review.md').read_text(), target['candidate_id'])
            if projected != (base / 'verdict.tsv').read_text():
                raise ValueError('host projection mismatch')
            votes.append(projected.split('\t')[1])
        rows.append(target['key'] + '\t' + ','.join(votes) + '\t' + min(votes, key=rank.get))
    (out / 'aggregate.tsv').write_text('\n'.join(rows) + '\n')
    print('\n'.join(rows))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('prepare', 'run'))
    parser.add_argument('output', type=pathlib.Path)
    parser.add_argument('cases', nargs='*', type=pathlib.Path)
    args = parser.parse_args()
    if args.mode == 'prepare':
        if not args.cases:
            parser.error('prepare requires neutral case directories')
        prepare(args.output.resolve(), [p.resolve() for p in args.cases])
    else:
        if args.cases:
            parser.error('run uses only the frozen output directory')
        run(args.output.resolve())
