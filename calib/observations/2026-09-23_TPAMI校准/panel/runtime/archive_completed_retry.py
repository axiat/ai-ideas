#!/opt/homebrew/bin/python3.11
"""Offline verification and archival only; this script never launches a provider."""
import datetime
import json
import pathlib
import shlex
import shutil
import sys
import panel
import retry_format

out = pathlib.Path(sys.argv[1]).resolve()
manifest_raw = (out / 'manifest.json').read_bytes()
failed_raw = (out / 'format-retry.json').read_bytes()
manifest, retry = json.loads(manifest_raw), json.loads(failed_raw)
stage = panel.portable_stage
runtime = pathlib.Path(manifest['runtime_root'])
source = pathlib.Path(retry['runtime_root'])
destination = out / 'reviews/probe03-I1/seat-1'
receipt_path = out / 'format-retry-archive.json'
assert not destination.exists() and not receipt_path.exists() and not (out / 'aggregate.tsv').exists()
assert retry['status'] == 'failed' and retry['retry_number'] == 1
assert retry['error'] == 'Codex executable or inherited default configuration changed'
current = panel.cli_identity(manifest['codex_wrapper_argv_prefix'][0])
for field in ('executable_path', 'executable_sha256'):
    assert current[field] == manifest['cli_identity'][field]
for name, digest in manifest['repo_sha256'].items():
    assert panel.sha((panel.REPO / name).read_bytes()) == digest, name
for name, digest in manifest['frozen_sha256'].items():
    assert panel.sha((out / name).read_bytes()) == digest, name
assert panel.sha(panel.PROMPT.encode()) == manifest['prompt_sha256']
assert panel.sha((runtime / 'bin/codex').read_bytes()) == manifest['wrapper_sha256']
wrapper = runtime / 'format-retry/bin/codex'
assert panel.sha(wrapper.read_bytes()) == retry['local_cli_binding']['wrapper_sha256']
expected_argv = manifest['codex_wrapper_argv_prefix'] + ['-m', 'gpt-6-astra', '-c', 'model_reasoning_effort=xhigh']
assert retry['local_cli_binding']['argv_prefix'] == expected_argv
assert wrapper.read_text() == '#!/bin/sh\nexec ' + shlex.join(expected_argv) + ' "$@"\n'
assert retry_format.existing_hashes(out, manifest) == retry['unchanged_artifact_sha256']
assert panel.sha((out / retry['original_response_archive']).read_bytes()) == retry['original_response_sha256']

pre_raw, pre = stage._load_canonical_file(source / 'state/preflight.json', 65536, 'invalid_preflight')
_, completion = stage._load_canonical_file(source / 'state/completion.json', 65536, 'invalid_completion')
stage._validate_public_preflight(pre)
stage._validate_public_completion(completion)
material = dict(completion)
completion_id = material.pop('completion_id')
assert completion_id == stage._completion_id(material)
assert completion['preflight_sha256'] == panel.sha(pre_raw)
for key in ('stage', 'seat_id', 'provider', 'provider_validation', 'authority',
            'execution_request_profile_hash', 'max_output_tokens',
            'output_token_cap_binding', 'output_token_cap_semantics'):
    assert completion[key] == pre[key], key
assert pre['stage'] == 'review' and pre['seat_id'] == 'probe03-I1-seat-1'
target = next(t for t in manifest['targets'] if t['key'] == 'probe03-I1')
input_raws = {name: path.read_bytes() for name, path in panel.inputs_for(out, target).items()}
assert pre['input_sha256s'] == {name: panel.sha(raw) for name, raw in input_raws.items()}
role_raw = (out / 'frozen/review_role.md').read_bytes()
prompt = (out / 'frozen/serialized_prompt.txt').read_text() + retry['prompt_addition']
assert retry['prompt_addition'] == retry_format.CLARIFICATION
assert pre['role_sha256'] == panel.sha(role_raw)
assert pre['serialized_prompt_sha256'] == panel.sha(prompt.encode())
schema = stage._response_schema('review')
assert pre['response_schema_sha256'] == panel.sha(stage._canonical_bytes(schema))
request, binding = stage._provider_request('codex', 'review', pre['seat_id'], prompt,
    pre['input_sha256s'], pre['role_sha256'], schema, pre['max_output_tokens'],
    pre['output_token_cap_semantics'], role_text=role_raw.decode(),
    declared_input_texts={name: raw.decode() for name, raw in input_raws.items()})
assert panel.sha(request.encode()) == pre['provider_request_sha256']
assert binding == pre['provider_request_binding_sha256']
envelope_path = source / 'state/imports' / (completion['model_envelope_sha256'] + '.json')
envelope = envelope_path.read_bytes()
assert panel.sha(envelope) == completion['model_envelope_sha256']
projected = stage._project_outputs(pre, envelope, input_raws)
assert set(projected) == set(completion['outputs']) == {p.name for p in (source / 'output').iterdir()}
for name, raw in projected.items():
    descriptor = completion['outputs'][name]
    assert raw == (source / 'output' / name).read_bytes()
    assert descriptor['sha256'] == panel.sha(raw) and descriptor['byte_count'] == len(raw)

shutil.copytree(source, destination)
rows = []
rank = {'reject': 0, 'accept-w-rev': 1, 'strong-accept': 2}
for target in manifest['targets']:
    votes = [retry_format.ballot(out, target, seat) for seat in range(1, 4)]
    rows.append(target['key'] + '\t' + ','.join(votes) + '\t' + min(votes, key=rank.get))
with (out / 'aggregate.tsv').open('x') as stream:
    stream.write('\n'.join(rows) + '\n')
assert (out / 'manifest.json').read_bytes() == manifest_raw
assert (out / 'format-retry.json').read_bytes() == failed_raw
assert retry_format.existing_hashes(out, manifest) == retry['unchanged_artifact_sha256']
panel.write_json(receipt_path, {
    'status': 'offline-verified-archived-and-aggregated', 'provider_calls': 0,
    'completion_id': completion_id, 'retry_response_sha256': completion['model_envelope_sha256'],
    'original_failure_receipt_sha256': panel.sha(failed_raw),
    'original_failure_receipt_preserved': True, 'original_eight_ballots_unchanged': True,
    'explicit_cli_model': 'gpt-6-astra', 'explicit_cli_reasoning': 'xhigh',
    'retry_start_global_cli_identity': retry['current_global_cli_identity'],
    'archive_time_global_cli_identity': current,
    'global_config_mtime_utc': datetime.datetime.fromtimestamp(
        pathlib.Path(current['config_path']).stat().st_mtime, datetime.timezone.utc).isoformat(),
    'validated': ['CLI binary', 'both wrappers', 'repository sources', 'frozen inputs',
                  'role and augmented prompt', 'reconstructed provider request and response attestation',
                  'canonical preflight and completion ID', 'raw response and all host projections',
                  'original invalid response and eight existing ballots'],
    'interpretation': 'The post-call failure was the full global configuration hash check. '
        'This call explicitly bound model and reasoning in unchanged CLI arguments; '
        'changing global defaults does not override those command-line values. '
        'No model call or global configuration change was performed during archival.',
    'archive_script_sha256': panel.sha(pathlib.Path(__file__).read_bytes()),
    'aggregate_sha256': panel.sha((out / 'aggregate.tsv').read_bytes())})
print('\n'.join(rows))
print('Offline archival receipt:', receipt_path)
