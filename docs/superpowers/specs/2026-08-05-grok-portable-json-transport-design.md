# Grok Portable JSON Transport

## Problem

The portable v2 runtime accepts one canonical JSON response envelope on
stdout. The Grok command currently uses `--output-format plain`, which can
emit progress messages before the final response. The combined stream is not
JSON and fails as `malformed_output`. An observed native-JSON response placed
valid, non-canonical inner JSON inside one whole-text Markdown fence labeled
lowercase `json`. Passing those fence markers to the strict JSON parser also
fails as `malformed_output`.

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
3. Encode `text` as strict UTF-8. Accept bare JSON or one exact whole-text
   fence whose prefix is three backticks plus `json` plus LF and whose suffix
   is LF plus three backticks. Strip only those markers. Reject surrounding
   text, a different fence label, or a missing closing marker; do not trim,
   search, or repair the response.
4. Parse the resulting bytes as the model response envelope. Reject duplicate
   keys, floating-point values, non-finite constants, invalid UTF-8, and
   non-NFC strings.
5. Validate the closed response schema and the two request-attestation hashes.
6. Serialize the validated model envelope as canonical UTF-8 JSON and hash,
   import, project, and receipt that canonical byte sequence.

Codex, Kimi, OpenCode, and agy retain their current raw canonical-stdout
contract. Grok transport metadata cannot supply or repair response fields;
only the validated `text` value enters the portable response contract.

## Failure Handling

Malformed Grok outer JSON, duplicate outer keys, a missing or non-string
`text`, and a terminal reason other than `end_turn` fail before import or
publication. A fence with surrounding text, a different language label, or no
exact closing marker fails at the same point. Existing process exit, timeout,
stdout-size, mirror-artifact, response-schema, and attestation checks remain
active.

`fail_round` sleeps only when another round can run. A positive `ROUND_LIMIT`
already reached by the current round returns directly to the loop, which logs
the limit and exits.

## Verification

Automated checks cover:

- the Grok argv grammar and tracked registry identity;
- Grok outer JSON containing valid non-canonical inner JSON as either bare
  text or one exact whole-text lowercase-`json` fence;
- leading or trailing text around a fence, a different fence language, and a
  missing closing marker;
- malformed and duplicate-key outer JSON, missing text, and unsuccessful stop
  reasons;
- unchanged strict raw-canonical behavior for other providers;
- response-schema and request-attestation rejection after Grok unwrapping;
- immediate exit after a failed final round;
- the portable runtime, Hunt/AwR integration, product contract, shell syntax,
  and OpenSpec validation suites.

A real Grok portable-stage qualification uses the configured `grok-4.5` model
with one small bounded request. Qualification requires successful canonical
import and projection without a full Hunt research round. Claude is not
invoked.

## Scope

The change does not alter provider selection, model or reasoning defaults,
portable-mirror inputs, output schemas, hard-complete qualification, or failed
payload retention. It does not modify `ledger.tsv`.
