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
    # Host projects ideas.tsv from markdown; model/agent writes MD only.
    direction = ""
    story = "Constraint-Driven Sparse World Models"
    summary = "Gate latent updates with model confidence."
    experiment = (
        "Compare dense and gated updates on 128 held-out episodes using one "
        "H100; kill the idea if success drops by more than two points or "
        "latency improves by less than thirty percent."
    )
    if (pathlib.Path("input") / "direction_constraint.json").is_file():
        direction = (
            "Direction Axis: memory-representation-update\n"
            "Target Failure: dynamic-scene-change\n"
            "Direction Evidence: The repair arm attributes recovery to "
            "corrected 3D memory.\n"
        )
        story = "Repair-Aware 3D Memory Updates for Dynamic VLA Scenes"
        summary = "Repair persistent 3D memory after dynamic scene changes."
        experiment = (
            "Compare stale and repair-aware 3D memory on dynamic scene "
            "changes; kill the idea if repaired VLA decisions do not recover."
        )
    (output / "ideas.md").write_text(
        "Assumption-Removal Attempt: complete I1\n\n"
        "## I1\n"
        + f"One-Sentence Story: {story}\n"
        + "Theme: World Models - Architecture\n"
        + direction
        + "Form: remove-load-bearing-assumption\n"
        + f"Summary: {summary}\n"
        + f"Minimal Falsification Experiment: {experiment}\n"
        + "Why It May Be Novel: Independent research must test occupation.\n"
        + "Assumption to Remove: Dense latent updates are required.\n"
        + "Why It Can Be Removed Now: Confidence is calibrated online.\n"
        + "Forcing Constraint: Deployment latency limits dense updates.\n"
        + "Crack Evidence: https://example.com/one | Stable skipped updates.\n"
        + "Crack Evidence: https://example.com/two | Bounded latent drift.\n",
        encoding="utf-8",
    )
elif stage == "history-compare":
    pack = payload["retrieval_payload"]
    status = os.environ.get(
        "HISTORY_STAGE_COMPARATOR_STATUS",
        "complete_no_match",
    )
    relations = []
    for index, lineage in enumerate(pack["lineages"]):
        match = lineage["matches"][0]
        if status == "uncertain":
            relation = "uncertain"
        elif status in {
            "complete_match",
            "conflicting_evidence",
        } and index == 0:
            relation = {
                "duplicate_search": "same_core_idea",
                "evolution_search": "same_lineage_revision",
                "failure_pattern_search": "same_failure_mechanism",
            }[pack["intent"]]
        else:
            relation = "distinct"
        relations.append(
            {
                "relation": relation,
                "candidate_id": match["candidate_id"],
                "lineage_id": match["lineage_id"],
                "facet": match["facet"],
                "evidence_id": match["evidence_id"],
                "material_difference": "The supplied propositions differ.",
                "confidence": 0.8,
            }
        )
    response = {
        "status": status,
        "comparator_version": "history-comparator-v1",
        "relations": relations,
        "expansion_request": (
            {
                "record_ids": [
                    pack["lineages"][0]["matches"][0][
                        "candidate_id"
                    ]
                ]
            }
            if (
                status == "uncertain"
                and pack["lineages"]
                and pack["expansion_round"]
                < pack["hard_limits"]["max_expansion_rounds"]
            )
            else None
        ),
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
    verdict = os.environ.get(
        "HISTORY_STAGE_REVIEW_VERDICT",
        "accept-w-rev",
    )
    ballot = {
        "strong-accept": {
            "critical": 0,
            "major": 0,
            "headline": "The bounded candidate clears the review gates.",
            "occupation": "The supplied prior work leaves the claim open.",
            "experiment": "The bounded falsification is decisive.",
            "reason": "The bounded fixture supports strong acceptance.",
        },
        "accept-w-rev": {
            "critical": 0,
            "major": 1,
            "headline": "The bounded candidate remains plausible.",
            "occupation": "The supplied prior work leaves one gap.",
            "experiment": "The falsification is bounded but incomplete.",
            "reason": "The bounded fixture leaves one major issue.",
        },
        "reject": {
            "critical": 1,
            "major": 1,
            "headline": "The bounded candidate fails a critical gate.",
            "occupation": "The supplied prior work occupies the claim.",
            "experiment": "The falsification cannot resolve the conflict.",
            "reason": "The bounded fixture contains a critical issue.",
        },
    }[verdict]
    reason = ballot["reason"]
    # Host projects verdict.tsv from review.md.
    (output / "review.md").write_text(
        f"# {candidate_id}\n"
        f"Verdict: {verdict}\n"
        f"CRITICAL: {ballot['critical']}\n"
        f"MAJOR: {ballot['major']}\n"
        f"Headline: {ballot['headline']}\n"
        f"Occupation: {ballot['occupation']}\n"
        f"Experiment: {ballot['experiment']}\n"
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
