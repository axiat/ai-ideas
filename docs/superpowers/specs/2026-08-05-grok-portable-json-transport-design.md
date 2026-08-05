# Provider Portable JSON Transport

## Problem

The portable v2 runtime accepts one canonical JSON response envelope on
stdout. Grok native JSON places model output inside a provider-owned object.
A real session produced intermediate assistant narration and a final valid,
non-canonical envelope inside a Markdown fence labeled lowercase `json`. The
terminal message passes the strict parser in isolation, while the portable
attempt failed before import. The process-boundary stdout was not retained;
the remaining transport must therefore tolerate provider narration without
searching for arbitrary JSON.

Agy received the same portable request but followed the AwR role's legacy
file-output wording. It generated the requested content, called
`write_to_file` for `mirror/priorwork.md`, and was rejected by its own artifact
path policy. Portable v2 instead requires an unchanged mirror and one response
envelope on stdout. The request did not state that transport precedence
explicitly.

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
3. Encode `text` as strict UTF-8. Accept bare JSON or one unique terminal
   fence whose opener is an exact LF-delimited lowercase-`json` line and whose
   closing delimiter ends the string. Provider narration may precede the
   opener and is discarded as transport metadata. Reject any delimiter outside
   that pair, a non-line-start opener, CRLF, a different label, an incomplete
   close, or any byte after the close. Do not trim, normalize, locate an
   arbitrary JSON substring, or repair the response.
4. Parse the resulting bytes as the model response envelope. Reject duplicate
   keys, floating-point values, non-finite constants, invalid UTF-8, and
   non-NFC strings.
5. Validate the closed response schema and the two request-attestation hashes.
6. Serialize the validated model envelope as canonical UTF-8 JSON and hash,
   import, project, and receipt that canonical byte sequence.

Codex, Kimi, OpenCode, and agy retain their raw canonical-stdout decoder. Grok
transport metadata cannot supply or repair response fields; only the validated
terminal fenced body or complete bare `text` enters the portable response
contract.

Every portable request also carries binding-covered `transport_instructions`.
They state that `role.md` defines artifact content while the request defines
the output channel, the mirror is read-only, and stdout must contain exactly
one UTF-8/NFC canonical object matching `response_schema`, followed by one LF.
They prohibit narration, Markdown fences, extra bytes, and file changes. The
instructions enter the request binding before launch. Legacy v1 AwR continues
to supply explicit output paths and does not consume this portable request.

## Failure Handling

Malformed Grok outer JSON, duplicate outer keys, a missing or non-string
`text`, and a terminal reason other than `end_turn` fail before import or
publication. A terminal fence with another delimiter, a different language
label, CRLF, no exact close, or trailing bytes fails at the same point. Agy
file writes fail mirror validation even if stdout is otherwise valid. Empty,
prefixed, fenced, non-canonical, schema-invalid, or unattested agy stdout is
never recovered from its brain directory or another artifact path. Existing
process exit, timeout, stdout-size, mirror-artifact, response-schema, and
attestation checks remain active.

`fail_round` sleeps only when another round can run. A positive `ROUND_LIMIT`
already reached by the current round returns directly to the loop, which logs
the limit and exits.

## Verification

Automated checks cover:

- the Grok argv grammar and tracked registry identity;
- Grok outer JSON containing valid non-canonical inner JSON as bare text, one
  exact whole-text fence, or one unique terminal fence after provider
  narration;
- duplicate and non-line-start delimiters, CRLF, trailing bytes, a different
  fence language, and a missing closing marker;
- malformed and duplicate-key outer JSON, missing text, and unsuccessful stop
  reasons;
- unchanged strict raw-canonical behavior for other providers;
- response-schema and request-attestation rejection after Grok unwrapping;
- request-binding coverage for portable transport instructions and all three
  fake agy AwR stages;
- agy mirror-write rejection without artifact import or fallback recovery;
- immediate exit after a failed final round;
- the portable runtime, Hunt/AwR integration, product contract, shell syntax,
  and OpenSpec validation suites.

Real portable-stage qualification uses one small bounded Grok `grok-4.5`/high
request and one small bounded agy `gemini-3.6-flash-high`/high request. Each
qualification requires successful canonical import and projection without a
full Hunt or AwR sidecar round. Claude is not invoked.

## Scope

The change does not alter provider selection, model or reasoning defaults,
portable-mirror inputs, output schemas, hard-complete qualification, or failed
payload retention. It does not modify `ledger.tsv`.
