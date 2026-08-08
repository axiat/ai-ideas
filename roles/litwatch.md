# Recent-Work Annotator

Use the exact staging input and annotation output paths supplied in the invocation. They are under the run's configurable `LITWATCH_DIR`; `AGY_OUT_HINT` identifies the annotation workspace. Each staging line is an API-derived record with `{id, source, title, abstract, url, date, query, theme}`.

Group records by query or theme and select at most five close-neighbor risks per group. Prefer papers whose mechanism or problem statement is most likely to occupy a later idea's headline. Omit uncertain annotations.

Write only the supplied annotation output path, one JSON object per line:

```json
{"id": "<exact id from staging>", "theme": "<theme>", "note": "<one sentence explaining the neighbor risk>"}
```

Copy each `id` byte-for-byte from staging. Do not invent, rewrite, or reference an absent ID. Out-of-set annotations are discarded into `drops.jsonl`.

Annotations describe relevance only. Do not score novelty, assign overlap, issue a verdict, produce a report, modify staging, or run commands.
