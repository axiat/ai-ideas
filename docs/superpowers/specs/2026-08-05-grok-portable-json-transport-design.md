# Grok Portable JSON Transport

## Problem

The portable v2 runtime accepts one canonical JSON response envelope on
stdout. The Grok command currently uses `--output-format plain`, which can
emit progress messages before the final response. The combined stream is not
JSON and fails as `malformed_output`. Grok's final response text can also use
valid but non-canonical JSON formatting, which would fail as
`noncanonical_output` if extracted directly.

After a failed final round, `fail_round` sleeps for `FAIL_SLEEP_MIN` before the
main loop observes `ROUND_LIMIT`. A one-round run can therefore wait 150
minutes even though no retry is possible.

## Transport

The Grok command grammar uses `--output-format json`. The tracked provider
registry records a new Grok grammar revision, and its byte-exact registry hash
is updated.

The portable runtime decodes Grok stdout in two layers:

1. Parse the Grok-owned outer JSON object with strict UTF-8 and duplicate-key
   rejection. Numeric usage and cost metadata remain allowed because they are
   provider transport metadata.
2. Require a successful terminal response with `stopReason=end_turn` and a
   string `text` field.
3. Parse `text` as the model response envelope. Reject duplicate keys,
   floating-point values, non-finite constants, invalid UTF-8, and non-NFC
   strings.
4. Validate the closed response schema and the two request-attestation hashes.
5. Serialize the validated model envelope as canonical UTF-8 JSON and hash,
   import, project, and receipt that canonical byte sequence.

Codex, Kimi, OpenCode, and agy retain their current raw canonical-stdout
contract. Grok transport metadata cannot supply or repair response fields;
only the validated `text` value enters the portable response contract.

## Failure Handling

Malformed Grok outer JSON, duplicate outer keys, a missing or non-string
`text`, and a terminal reason other than `end_turn` fail before import or
publication. Existing process exit, timeout, stdout-size, mirror-artifact,
response-schema, and attestation checks remain active.

`fail_round` sleeps only when another round can run. A positive `ROUND_LIMIT`
already reached by the current round returns directly to the loop, which logs
the limit and exits.

## Verification

Automated checks cover:

- the Grok argv grammar and tracked registry identity;
- a Grok outer JSON response containing valid non-canonical inner JSON;
- malformed and duplicate-key outer JSON, missing text, and unsuccessful stop
  reasons;
- unchanged strict raw-canonical behavior for other providers;
- response-schema and request-attestation rejection after Grok unwrapping;
- immediate exit after a failed final round;
- the portable runtime, Hunt/AwR integration, product contract, shell syntax,
  and OpenSpec validation suites.

One real Grok portable-stage smoke uses the configured `grok-4.5` model with a
small bounded request. It verifies successful import and projection without
running a full Hunt research round. Claude is not invoked.

## Scope

The change does not alter provider selection, model or reasoning defaults,
portable-mirror inputs, output schemas, hard-complete qualification, or failed
payload retention. It does not modify `ledger.tsv`.
