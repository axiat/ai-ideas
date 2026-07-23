#!/usr/bin/env python3
import json
import os
import pathlib
import sys


prompt = sys.argv[1]
payload = json.loads(prompt)
stage = payload["stage"]
output = pathlib.Path("output")
output.mkdir(parents=True, exist_ok=True)

if stage == "generate":
    (output / "ideas.tsv").write_text(
        "I1\tConstraint-Driven Sparse World Models\t"
        "World Models - Architecture\n",
        encoding="utf-8",
    )
    (output / "ideas.md").write_text(
        "Assumption-Removal Attempt: complete I1\n\n"
        "## I1\n"
        "One-Sentence Story: Constraint-Driven Sparse World Models\n"
        "Theme: World Models - Architecture\n"
        "Form: remove-load-bearing-assumption\n"
        "Summary: Gate latent updates with model confidence.\n"
        "Minimal Falsification Experiment: Compare dense and gated updates "
        "on 128 held-out episodes using one H100; kill the idea if success "
        "drops by more than two points or latency improves by less than "
        "thirty percent.\n"
        "Why It May Be Novel: Independent research must test occupation.\n"
        "Assumption to Remove: Dense latent updates are required.\n"
        "Why It Can Be Removed Now: Confidence is calibrated online.\n"
        "Forcing Constraint: Deployment latency limits dense updates.\n"
        "Crack Evidence: https://example.com/one | Stable skipped updates.\n"
        "Crack Evidence: https://example.com/two | Bounded latent drift.\n",
        encoding="utf-8",
    )
elif stage == "history-compare":
    pack = payload["retrieval_payload"]
    relations = []
    for lineage in pack["lineages"]:
        match = lineage["matches"][0]
        relations.append(
            {
                "relation": "distinct",
                "candidate_id": match["candidate_id"],
                "lineage_id": match["lineage_id"],
                "facet": match["facet"],
                "evidence_id": match["evidence_id"],
                "material_difference": "The supplied propositions differ.",
                "confidence": 0.8,
            }
        )
    response = {
        "status": "complete_no_match",
        "comparator_version": "history-comparator-v1",
        "relations": relations,
        "expansion_request": None,
    }
    (output / "history-comparison.json").write_text(
        json.dumps(
            response,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
elif stage == "review":
    candidate_id = payload["candidate"]["candidate_id"]
    reason = "The bounded fixture leaves one major issue."
    (output / "verdict.tsv").write_text(
        f"{candidate_id}\taccept-w-rev\t1\t"
        f"{reason}\n",
        encoding="utf-8",
    )
    (output / "review.md").write_text(
        f"# {candidate_id}\n"
        "Verdict: accept-w-rev\n"
        "CRITICAL: 0\n"
        "MAJOR: 1\n"
        "Headline: The bounded candidate remains plausible.\n"
        "Occupation: The supplied prior work leaves one gap.\n"
        "Experiment: The falsification is bounded but incomplete.\n"
        "Estimand: The supplied estimand is aligned.\n"
        "Payoff: One attributable payoff remains possible.\n"
        "Feasibility: One researcher and one H100 suffice.\n"
        "History: unavailable\n"
        f"Reason: {reason}\n",
        encoding="utf-8",
    )
elif stage == "meta":
    mounted = {
        item["path"]: item["text"]
        for item in payload["mounted_inputs"]
    }
    batch = json.loads(mounted["failure_batch.json"])
    (output / "failure-distillation.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mappings": [
                    {
                        "source_id": item["source_id"],
                        "failure_code": "unmapped",
                        "theme": "unmapped",
                    }
                    for item in batch["items"]
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
else:
    raise SystemExit(64)
