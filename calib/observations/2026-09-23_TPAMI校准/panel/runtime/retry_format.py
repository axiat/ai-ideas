#!/opt/homebrew/bin/python3.11
"""Check or run one formatting-only replacement for probe03-I1 seat 1."""
import argparse
import copy
import datetime
import json
import os
import pathlib
import shlex
import shutil
import tempfile
import panel

KEY, SEAT = 'probe03-I1', 1
CLARIFICATION = '''\nOutput formatting clarification: CRITICAL and MAJOR values must each
contain only bare ASCII digits matching ^[0-9]+$. Put all explanations and evidence
tags in Reason or other descriptive fields, never after either count. Keep every
other review instruction unchanged.\n'''

def ballot(out, target, seat):
    base = out / 'reviews' / target['key'] / ('seat-' + str(seat)) / 'output'
    projected = panel.stage_contract.build_review_verdict_from_markdown(
        (base / 'review.md').read_text(), target['candidate_id'])
    if projected != (base / 'verdict.tsv').read_text():
        raise ValueError('host projection mismatch: ' + str(base))
    return projected.split('\t')[1]

def existing_hashes(out, manifest):
    hashes = {}
    for target in manifest['targets']:
        for seat in range(1, 4):
            if (target['key'], seat) == (KEY, SEAT):
                continue
            ballot(out, target, seat)
            base = out / 'reviews' / target['key'] / ('seat-' + str(seat))
            for path in base.rglob('*'):
                if path.is_file():
                    hashes[str(path.relative_to(out))] = panel.sha(path.read_bytes())
    return hashes

def prepare(intent, out, target, root):
    return panel.portable_stage.prepare_stage(intent, stage='review',
        seat_id=KEY + '-seat-' + str(SEAT),
        serialized_prompt=panel.PROMPT + CLARIFICATION,
        input_paths=panel.inputs_for(out, target),
        output_root=root / 'output', state_root=root / 'state')

def bound_intent(manifest, directory):
    directory.mkdir(parents=True, exist_ok=False)
    frozen = manifest['cli_identity']
    argv = manifest['codex_wrapper_argv_prefix'] + [
        '-m', frozen['configured_model'], '-c',
        'model_reasoning_effort=' + frozen['configured_reasoning']]
    wrapper = directory / 'codex'
    wrapper.write_text('#!/bin/sh\nexec ' + shlex.join(argv) + ' "$@"\n')
    wrapper.chmod(0o700)
    os.environ['PATH'] = str(directory) + os.pathsep + os.environ['PATH']
    intent = panel.provider_adapters.resolve_command_intent(
        panel.provider_adapters.load_registry(panel.REPO / 'history/provider-adapters-v1.json'),
        'hunt', 'codex', max_output_tokens=manifest['output_token_budget'])
    return intent, {'wrapper_sha256': panel.sha(wrapper.read_bytes()), 'argv_prefix': argv}

def timing(manifest):
    def stamp(path):
        return datetime.datetime.fromtimestamp(path.stat().st_mtime,
            datetime.timezone.utc).isoformat()
    rows = []
    for target in manifest['targets']:
        for seat in range(1, 4):
            state = pathlib.Path(manifest['runtime_root']) / 'run' / target['key'] / ('seat-' + str(seat)) / 'state'
            imports = list((state / 'imports').glob('*.json'))
            rows.append({'target': target['key'], 'seat': seat,
                'preflight_mtime_utc': stamp(state / 'preflight.json'),
                'response_import_mtime_utc': stamp(imports[0])})
    return {'current_global_config_mtime_utc': stamp(pathlib.Path(manifest['cli_identity']['config_path'])),
            'original_calls': rows,
            'limitation': 'File mtimes are launch/return proxies; the original host did not preserve effective CLI reasoning declarations.'}

def main(out, execute):
    manifest = json.loads((out / 'manifest.json').read_text())
    current_identity = panel.cli_identity(manifest['codex_wrapper_argv_prefix'][0])
    frozen_identity = manifest['cli_identity']
    for key in ('executable_path', 'executable_sha256'):
        if current_identity[key] != frozen_identity[key]:
            raise ValueError('the original Codex binary changed')
    verification_snapshot = copy.deepcopy(manifest)
    verification_snapshot['cli_identity'] = current_identity
    panel.verify(out, verification_snapshot)
    target = next(item for item in manifest['targets'] if item['key'] == KEY)
    destination = out / 'reviews' / KEY / 'seat-1'
    if destination.exists():
        raise ValueError('replacement destination already exists; refusing another retry')
    archive = out / 'invalid-attempts/probe03-I1-seat-1'
    original = pathlib.Path(manifest['runtime_root']) / 'run' / KEY / 'seat-1/state'
    imported = list((original / 'imports').glob('*.json'))
    if len(imported) != 1 or not archive.is_dir():
        raise ValueError('original invalid-response evidence or archive is unavailable')
    original_raw = imported[0].read_bytes()
    archived_matches = [p for p in archive.rglob(imported[0].name)
                        if p.is_file() and p.read_bytes() == original_raw]
    if not archived_matches:
        raise ValueError('archived invalid response does not match original bytes')
    envelope = json.loads(original_raw)
    markdown = envelope['artifacts'][0]['content']
    try:
        panel.stage_contract.build_review_verdict_from_markdown(markdown, target['candidate_id'])
    except panel.stage_contract.StageError as exc:
        if str(exc) != 'review markdown count fields are invalid':
            raise ValueError('original failure differs from the authorized formatting retry') from exc
    else:
        raise ValueError('original response is already valid; retry is not applicable')
    before = existing_hashes(out, manifest)
    retry_root = pathlib.Path(manifest['runtime_root']) / 'format-retry' / KEY / 'seat-1'
    record_path = out / 'format-retry.json'
    if retry_root.exists() or record_path.exists():
        raise ValueError('one formatting retry has already been prepared or attempted')
    if not execute:
        with tempfile.TemporaryDirectory(prefix='blind-format-preflight-') as directory:
            intent, binding = bound_intent(manifest, pathlib.Path(directory) / 'bin')
            prepared = prepare(intent, out, target, pathlib.Path(directory))
            print('Check passed: 8 valid ballots, original invalid count fields preserved, '
                  'same frozen inputs/binary and explicit frozen model/reasoning; no model call.')
            print('Retry exec bytes:', prepared['exec_budget']['conservative_total_bytes'])
        return
    intent, binding = bound_intent(manifest, pathlib.Path(manifest['runtime_root']) / 'format-retry/bin')
    record = {'target': KEY, 'seat': SEAT, 'retry_number': 1, 'status': 'started',
              'reason': 'host rejected non-numeric count field',
              'prompt_addition': CLARIFICATION,
              'original_response_sha256': panel.sha(original_raw),
              'original_response_archive': str(archived_matches[0].relative_to(out)),
              'unchanged_artifact_sha256': before,
              'frozen_input_sha256': manifest['frozen_sha256'],
              'frozen_cli_identity': frozen_identity, 'current_global_cli_identity': current_identity,
              'local_cli_binding': binding, 'original_call_timing': timing(manifest),
              'runtime_root': str(retry_root)}
    with record_path.open('x') as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write('\n')
    try:
        prepared = prepare(intent, out, target, retry_root)
        print('Launching one formatting retry:', KEY, 'seat', SEAT, flush=True)
        panel.portable_stage.run_stage(prepared, timeout_seconds=1800)
        panel.portable_stage.verify_completion(prepared)
        panel.verify(out, verification_snapshot)
        if existing_hashes(out, manifest) != before:
            raise ValueError('one of the original eight valid ballots changed')
        shutil.copytree(retry_root, destination)
        rows = []
        rank = {'reject': 0, 'accept-w-rev': 1, 'strong-accept': 2}
        for item in manifest['targets']:
            votes = [ballot(out, item, seat) for seat in range(1, 4)]
            rows.append(item['key'] + '\t' + ','.join(votes) + '\t' + min(votes, key=rank.get))
        with (out / 'aggregate.tsv').open('x') as stream:
            stream.write('\n'.join(rows) + '\n')
        record['status'] = 'validated-and-aggregated'
        record['completion_path'] = str((destination / 'state/completion.json').relative_to(out))
        panel.write_json(record_path, record)
        print('\n'.join(rows))
    except BaseException as exc:
        record['status'] = 'failed'
        record['error'] = str(exc)
        panel.write_json(record_path, record)
        raise

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=pathlib.Path)
    parser.add_argument('--run', action='store_true', help='Launch exactly one provider call; default is offline check')
    args = parser.parse_args()
    main(args.output.resolve(), args.run)
