#!/usr/bin/env python3
"""Read-only comparison of configuration against original A/B launch bytes."""
from pathlib import Path
import hashlib
import json
import re
import tomllib
import runner

def main():
    manifest=runner.read(runner.ROOT/'manifest.json')
    original=runner.read(runner.ORIGINAL/'manifest.json')
    path=Path(manifest['provider']['config_path']);raw=path.read_bytes();normalized=raw;removed=[]
    blocks=list(re.finditer(rb'(?m)^\[projects\.[^\n]+\]\n(?:(?!\[)[^\n]*\n?)*',normalized))
    patterns=[re.compile(re.escape(str(root.resolve()))+r'/run/(A|B)/probe[0-9]+/seat-[123]/state/attempt-[^/]+/mirror') for root in (runner.ROOT,runner.ORIGINAL)]
    for block in reversed(blocks):
        entries=tomllib.loads(block.group().decode()).get('projects',{})
        if len(entries)!=1:continue
        name,settings=next(iter(entries.items()))
        if settings=={'trust_level':'trusted'} and any(p.fullmatch(name) for p in patterns):
            removed.append('projects.'+name+'.trust_level');normalized=normalized[:block.start()]+normalized[block.end():]
    blocks=list(re.finditer(rb'(?m)^\[desktop\]\n(?:(?!\[)[^\n]*\n?)*',normalized))
    for block in reversed(blocks):
        desktop=tomllib.loads(block.group().decode()).get('desktop',{})
        if set(desktop)=={'external-agent-import-sync-enabled'} and type(desktop['external-agent-import-sync-enabled']) is bool:
            removed.append('desktop.external-agent-import-sync-enabled');normalized=normalized[:block.start()]+normalized[block.end():]
    normalized=normalized.rstrip(b'\n')+b'\n'
    digest=hashlib.sha256(normalized).hexdigest()
    identity=runner.config_identity(path)
    receipt={'mode':'read-only; no configuration value written or restored','raw_sha256':hashlib.sha256(raw).hexdigest(),
      'normalized_sha256':digest,'original_AB_run_config_sha256':original['provider']['config_sha256'],
      'matches_original_after_exact_owned_metadata_removal':digest==original['provider']['config_sha256'],
      'removed_metadata_keys':sorted(removed),'terminal_line_feed_normalization':True,
      'same_effective_config_as_followup_seal':identity['effective_config_sha256']==manifest['provider_config_identity']['effective_config_sha256'],
      'raw_configuration_saved':False}
    runner.write(runner.ROOT/'configuration-final-receipt.json',receipt)
    print(json.dumps({key:receipt[key] for key in ('matches_original_after_exact_owned_metadata_removal','same_effective_config_as_followup_seal','raw_configuration_saved')}))
    assert receipt['matches_original_after_exact_owned_metadata_removal'] and receipt['same_effective_config_as_followup_seal']

if __name__=='__main__':main()
