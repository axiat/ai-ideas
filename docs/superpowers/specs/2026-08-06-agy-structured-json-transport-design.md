# Agy Structured JSON Transport

## Problem

AwR v2 launches Agy with `--output-format text` and applies the raw canonical
stdout decoder used by Codex, Kimi, and OpenCode. Agy's text renderer appends
one LF after the final assistant text. When the assistant text already ends in
the required LF, process stdout ends in two LFs and the byte-exact decoder
returns `noncanonical_output`. The final assistant text controls neither the
renderer nor its framing, so text mode cannot distinguish model bytes from
CLI-added bytes without weakening the transport contract.

Agy 1.1.10 was probed with an explicit Gemini model, `--output-format json`,
and an inline `--json-schema`. The CLI returned one provider-owned JSON object
with `status="SUCCESS"`, the requested schema in `json_schema`, and the unique
schema-valid value in `structured_output`. Its human-facing `response`
contained intermediate model attempts and is unsuitable as an artifact
source. [Agy 1.1.8 introduced the structured output flags used by this
design](https://antigravity.google/changelog?plan=free).

## Decision

AwR v2 uses Agy structured JSON transport. Agy text output is not accepted as
a fallback. The minimum supported Agy version is 1.1.8. Provider resolution
and launch revalidation each fail before workload execution when the observed
Agy version is unavailable, malformed, or older than 1.1.8.

The tracked Agy grammar revision changes from `agy-portable-v1` to
`agy-portable-v2`. The execution profile and receipts therefore distinguish
text and structured transports. Codex, Kimi, Grok, and OpenCode retain their
current command grammars and decoders.

## Command and Request

The Agy command uses:

```text
agy --dangerously-skip-permissions --disable-slash-commands \
  --output-format json --add-dir MIRROR \
  --model MODEL [--effort EFFORT] \
  --json-schema CANONICAL_RESPONSE_SCHEMA \
  --print PROMPT
```

`CANONICAL_RESPONSE_SCHEMA` is the compact canonical JSON encoding of the
same response schema already covered by the portable request binding. It is
passed inline after removing only the canonical codec's one terminal LF, so no
schema file is added to the mirror. No trimming or whitespace normalization is
permitted. The diagnostic command record uses a fixed `RESPONSE_SCHEMA`
placeholder while the launch command uses the bound stage schema.

`prepare_stage()` freezes the exact canonical response-schema bytes alongside
the opaque prepared authority. `run_stage()` verifies their hash against the
preflight and output contract, compares the current stage schema with the
frozen bytes, and fails before provider rendering when they differ. The
provider request, inline argv schema, outer schema-echo comparison, and inner
value validation all consume the same frozen schema.

The Agy-specific transport instruction requires one structured final value
matching `response_schema`. It states that the CLI owns stdout framing and that
only a successful `structured_output` member is eligible for import. The role
continues to define artifact content, mirror writes remain forbidden, and the
two request-attestation hashes remain mandatory.

## Decoding and Publication

The complete Agy stdout remains subject to the existing 128 KiB capture limit.
The provider-specific decoder performs these checks in order:

1. Decode one UTF-8 JSON object with duplicate-key rejection. Provider metadata
   may contain finite floating-point values; non-finite constants remain
   invalid.
2. Require `status` to equal `SUCCESS` and require exactly one
   `structured_output` member whose value is a JSON object.
3. Require `json_schema` to be type-exactly identical to the exact stage
   response schema supplied on the command line. Canonical byte comparison
   distinguishes integer `1` from float `1.0` and Boolean `true`.
4. Ignore `response`, usage, duration, conversation identifiers, and other
   provider metadata as artifact sources.
5. Validate `structured_output` with the existing strict model-value rules:
   built-in JSON types, no float values, NFC strings, the closed stage schema,
   and exact request attestation.
6. Serialize the validated value on the host as canonical UTF-8 JSON with one
   trailing LF. This canonical byte sequence supplies the import bytes,
   envelope hash, projection, and completion receipt.

The outer object may contain additional provider metadata because those fields
cannot supply or alter the selected structured value. Security-relevant fields
remain fixed: duplicate top-level keys, a missing or non-success status,
missing or non-object `structured_output`, and a missing or mismatched
`json_schema` all reject the attempt.

## Version Gate

The host-owned Agy probe runs bounded `agy --version` with the existing
five-second and 32 KiB diagnostic limits. It requires exit zero, empty stderr,
and stdout matching exactly `MAJOR.MINOR.PATCH\n`, where each component is `0`
or a non-zero decimal integer without a leading zero. CRLF, additional lines,
prerelease/build suffixes, malformed UTF-8, and versions below 1.1.8 fail.
Provider profile construction and immediate launch revalidation both apply
this gate. The issued command intent already captures the executable byte
identity; executable drift invalidates the intent. The Agy catalog probe and
explicit non-Claude model requirement remain active.

## Failure Handling

Malformed or duplicate-key outer JSON, a non-success status, schema-echo
mismatch, absent structured output, invalid inner JSON types, response-schema
failure, and attestation mismatch produce no durable import, projected
artifact, or completion receipt. Outer transport failures use
`malformed_output`; closed inner-value violations use `schema_mismatch`; exact
attestation drift uses `provider_request_attestation_mismatch`; frozen schema
drift uses `response_schema_changed`. The adapter never searches `response`
for a JSON suffix, repairs malformed model text, consumes Markdown fences,
reads Agy brain state, or recovers a mirror artifact.

An unsupported Agy version fails preflight. Runtime failure never falls back
to text transport or another model. Provider selection and failover remain
controlled only by the declared AwR provider pool.

## Verification

Test-first coverage includes:

- Agy command argv, inline schema, minimum-version acceptance, old-version
  rejection, malformed-version rejection, and launch-time version gate;
- successful outer JSON import with canonical host bytes and matching schema;
- malformed and duplicate-key outer JSON, non-success status, absent or
  non-object `structured_output`, and missing or mismatched `json_schema`;
- inner duplicate keys where representable, non-NFC strings, floating-point
  values, closed-schema violations, and request-attestation mismatches;
- proof that noisy `response` text cannot become an artifact;
- unchanged strict raw canonical decoding for Codex, Kimi, and OpenCode and
  unchanged Grok transport handling;
- all-Agy AwR research, prior-work, and judge stages through the disposable
  mirror, import, projection, and receipt flow.

After offline suites pass, one bounded real Agy 1.1.8-or-newer stage with an
explicit Gemini model qualifies the provider transport. Qualification verifies
the selected model and effort, successful structured output, canonical import,
closed receipt hashes, attempt cleanup, and absence of Claude execution.

## Scope

This change repairs Agy portable transport and its version gate. History
retrieval token budgets, AwR queue scheduling, task-count limits, provider
throttling, artifact-content policy, and hard-complete authority remain
unchanged.
