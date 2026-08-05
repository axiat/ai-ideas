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

The complete Grok outer stdout remains under the existing 128 KiB capture
limit. The portable runtime then decodes it in two layers:

1. Parse the Grok-owned outer JSON object with strict UTF-8 and duplicate-key
   rejection. Numeric usage and cost metadata remain allowed because they are
   provider transport metadata.
2. Require a successful terminal response with `stopReason=end_turn` and a
   string `text` field.
3. Encode `text` as strict UTF-8. With no fence candidate, preserve the
   complete text for strict bare-JSON parsing. Fenced text must contain exactly
   two triple-backtick sequences and no CR byte. Its opener is an exact
   LF-delimited line-start lowercase-`json` marker; its exact line-start closer
   ends the string. Provider narration may precede the opener and is discarded
   as transport metadata. Reject every other delimiter, a non-line-start
   opener, a different label or case, an incomplete close, or any byte after
   the close. Do not trim, normalize, locate an arbitrary JSON substring, or
   repair the response.
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
the output channel, forbid mirror changes, and require exactly one UTF-8/NFC
canonical object matching `response_schema` on stdout, followed by one LF.
They prohibit narration, Markdown fences, and extra bytes. The instructions
enter the request binding before launch. For agy they override the legacy AwR
role's output-location and file-writing statements. The host still verifies
the closed declared-file path set after provider exit. Every declared entry
must remain a regular single-link file with its exact original `st_mode`, byte
count, and SHA-256, using a stable read for the content checks. Directory
presence alone is not an output, so the runtime-created `.tmp` directory may
remain empty. The host then validates canonical stdout, the closed schema, and
exact attestation, and never recovers an envelope from provider brain state or
a role-named artifact file. Legacy v1 AwR continues to supply explicit output
paths and does not consume this portable request.

## Failure Handling

Malformed Grok outer JSON, duplicate outer keys, a missing or non-string
`text`, and a terminal reason other than `end_turn` fail before import or
publication. Fenced text with any additional triple-backtick sequence, any CR
byte, a different language label or case, no exact close, or trailing bytes
fails at the same point. A closed-path-set mismatch, non-regular or multi-link
entry, exact-mode change, or stable byte-count/SHA mismatch fails mirror
validation even if stdout is otherwise valid. Directory presence alone is not
a file-path-set change, so the runtime-created `.tmp` may remain empty. This
post-exit validation is not an OS-enforced read-only mount. Empty, prefixed,
fenced, non-canonical, schema-invalid, or
unattested agy stdout is never recovered from its brain directory or another
artifact path. Existing process exit, timeout, stdout-size, mirror-artifact,
response-schema, and attestation checks remain active.

`fail_round` sleeps only when another round can run. A positive `ROUND_LIMIT`
already reached by the current round returns directly to the loop, which logs
the limit and exits.

## Verification

Automated checks cover:

- the Grok argv grammar and tracked registry identity;
- Grok outer JSON containing valid non-canonical inner JSON as bare text, one
  exact whole-text fence, or one unique terminal fence after provider
  narration;
- duplicate, inline, and indented extra delimiters, any CR byte, trailing
  bytes, a different fence language or case, and a missing closing marker;
- malformed and duplicate-key outer JSON, missing text, and unsuccessful stop
  reasons;
- unchanged strict raw-canonical behavior for other providers;
- response-schema and request-attestation rejection after Grok unwrapping;
- request-binding coverage for portable transport instructions and all three
  fake agy AwR stages;
- new-file, same-size role/input overwrite, and exact-mode drift rejection
  without artifact import or fallback recovery;
- immediate exit after a failed final round;
- the portable runtime, Hunt/AwR integration, product contract, shell syntax,
  and OpenSpec validation suites.

Real portable-stage qualification uses one small bounded Grok `grok-4.5`/high
request and one small bounded agy `gemini-3.6-flash-high`/high request. Each
qualification requires successful canonical import and projection without a
full Hunt or AwR sidecar round. Both live qualification gates remain pending.
Claude is not invoked.

## Scope

The change does not alter provider selection, model or reasoning defaults,
portable-mirror inputs, output schemas, hard-complete qualification, or failed
payload retention. It does not modify `ledger.tsv`.
