# Provider Portable JSON Transport

## Problem

The portable v2 runtime accepts one canonical JSON response envelope on
stdout. Grok native JSON places model output inside a provider-owned object.
A real session produced intermediate assistant narration and a final valid,
non-canonical envelope inside a Markdown fence labeled lowercase `json`. Grok's
CLI reducer concatenates those assistant chunks without a separator. The final
message passes the strict parser in isolation, while the portable attempt failed
before import. The process-boundary stdout was not retained; the remaining
transport must therefore isolate one unique terminal fence after accumulated
prefix bytes without searching for a JSON suffix.

Agy received the same portable request but followed the AwR role's legacy
file-output wording. It generated the requested content, called
`write_to_file` for `mirror/priorwork.md`, and was rejected by its own artifact
path policy. Portable v2 instead requires declared-file integrity and one
response envelope on stdout. Declared files must retain their exact
type, link count, mode, bytes, and hash; undeclared non-scratch files are
forbidden; bounded `.tmp` scratch and empty directories are ignored. The
request did not state that transport precedence explicitly.

After that precedence was bound, a live agy request emitted the expected
canonical stdout envelope, while its language server also wrote a schema cache
under the runtime-supplied `TMPDIR=.tmp`. Treating `.tmp` as empty-only caused
the otherwise valid attempt to fail as `unexpected_artifact`. Provider runtime
scratch therefore needs a small ignored allowance without widening the
declared input or output set.

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
3. Encode `text` as strict UTF-8. With no exact `b"```json\n"` opener,
   preserve the complete text for strict bare-JSON parsing; narration followed
   by bare JSON therefore fails. Grok's CLI reducer concatenates assistant
   chunks without a separator, so a fenced opener may begin at any byte rather
   than at a line start. Fenced text must contain exactly two triple-backtick
   sequences, exactly one opener, and no CR byte. Its closing delimiter must be
   preceded by LF and end the string. Discard only the accumulated prefix and
   the two markers. Reject every additional delimiter, a different label or
   case, an incomplete close, or any byte after the close. Do not trim,
   normalize, locate a JSON suffix, or repair the response.
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
the output channel, and instruct the model not to create, modify, or delete
mirror files. The stdout member
is selected by provider before the base-request binding is computed. Grok's
member requires the final assistant response itself to be exactly one
lowercase-`json` LF fence
containing one canonical UTF-8/NFC `response_schema` object, with no bytes
outside that fence, one trailing LF immediately before the terminal close, and
no triple-backtick sequence in an earlier assistant response. Every non-Grok
provider, including agy, must emit the raw canonical object followed by one LF,
without narration, a fence, or extra bytes. For agy this overrides the legacy
AwR role's output-location and file-writing statements. The host still verifies
the closed declared-file path set after provider exit. Every declared entry
must remain a regular single-link file with its exact original `st_mode`, byte
count, and SHA-256, using a stable read for the content checks. Directory
presence alone is not an output. For stdout portable attempts, the
runtime-created `.tmp` remains a real directory and may contain ignored
scratch under descriptor-relative, no-follow traversal. Nested entries are
limited to real directories and regular single-link files; the whole tree is
limited to 32 files, 64 entries, and 1 MiB of stable-read file bytes. Scratch
never enters imports, projections, completions, or declared-file identity. A
missing or replaced `.tmp`, link, special file, unreadable or unstable entry,
traversal race, or exceeded limit rejects the attempt. The remaining mirror is
walked through the same descriptor-relative, no-follow discipline. The walker
skips only the exact validated root `.tmp`, verifies each child and the root
against their final namespace identities, and ignores stable empty directories.
The host then validates canonical stdout, the closed schema, and exact
attestation, and never recovers an envelope from provider brain state or a
role-named artifact file. Legacy v1 AwR continues to supply explicit output
paths and does not consume this portable request.

## Attempt Lifecycle

Provider processes start in a new session. When the provider command returns,
the host terminates the process group and waits for the provider process before
reading the mirror or accepting stdout. Background descendants in that group
cannot keep mutating the attempt during validation.

All validation finishes while the attempt remains disposable. Before creating
or reusing a durable import, cleanup recursively repairs directory permissions,
removes the exact attempt tree, and verifies that it is absent. Cleanup failure
returns `attempt_cleanup_failed` and leaves imports, projections, and
completion receipts untouched.

Legacy file-output attempts with `forbid_extra_files=true` enumerate mirror
entries through directory descriptors, never follow links, and fail closed on
open/stat errors or final child/root namespace identity drift. Every
non-directory entry must be a regular single-link file. Unreadable directories,
hidden files, symlinks, special files, and raced replacements cannot be
silently omitted from the extra-file decision. This legacy check does not add
content or mode immutability for ordinary declared inputs.

## Failure Handling

Malformed Grok outer JSON, duplicate outer keys, a missing or non-string
`text`, and a terminal reason other than `end_turn` fail before import or
publication. Fenced text with an additional triple-backtick sequence, more
than one exact opener, any CR byte, a different label or case, no LF before the
exact terminal close, or a trailing byte fails at the same point. A
non-line-start exact opener remains valid because it may be the reducer boundary
between assistant chunks.
Narration followed by bare JSON remains invalid, and no JSON suffix is located.
A closed-path-set mismatch, non-regular or multi-link entry, exact-mode change,
or stable byte-count/SHA mismatch fails mirror validation even if stdout is
otherwise valid. Unsafe or unbounded `.tmp` scratch fails at the same point.
The process-group and post-exit checks are not an OS-enforced read-only mount.
Empty, prefixed, fenced, non-canonical, schema-invalid, or unattested agy stdout
is never recovered from its brain directory or another artifact path. Existing
process exit, timeout, stdout-size, mirror-artifact, response-schema, and
attestation checks remain active. Attempt cleanup must succeed before any
validated payload becomes durable.

`fail_round` sleeps only when another round can run. A positive `ROUND_LIMIT`
already reached by the current round returns directly to the loop, which logs
the limit and exits.

## Verification

Automated checks cover:

- the Grok argv grammar and tracked registry identity;
- Grok outer JSON containing valid non-canonical inner JSON as bare text, one
  exact whole-text fence, or one unique terminal fence after an accumulated
  assistant prefix;
- a reducer-joined exact opener with no preceding LF, plus rejection of
  narrated bare JSON, duplicate, inline, and indented extra delimiters, any CR
  byte, trailing bytes, a different fence language or case, and a missing
  closing marker;
- malformed and duplicate-key outer JSON, missing text, and unsuccessful stop
  reasons;
- unchanged strict raw-canonical behavior for other providers;
- response-schema and request-attestation rejection after Grok unwrapping;
- provider-specific request-binding coverage for the Grok final-response fence,
  non-Grok raw canonical stdout, and all three fake agy AwR stages;
- new-file, same-size role/input overwrite, and exact-mode drift rejection
  without artifact import or fallback recovery;
- accepted bounded `.tmp` cache files plus rejection of missing/replaced roots,
  links, special files, unreadable entries, unstable growth, and all count or
  byte-limit violations, with no scratch import;
- process-group quiescence before mirror validation, permission-repairing
  cleanup before durable import, and `attempt_cleanup_failed` publication
  suppression;
- descriptor-relative legacy extra-file enumeration that rejects hidden files,
  symlinks, special files, unreadable directories, and raced replacements;
- descriptor-relative stdout mirror enumeration that rejects mode-zero hidden
  files, declared-path symlink/hardlink/FIFO replacement, and child/root
  namespace swap while retaining exact declared-file checks, stable empty
  directories, and the separately validated `.tmp` exception;
- immediate exit after a failed final round;
- the portable runtime, Hunt/AwR integration, product contract, shell syntax,
  and OpenSpec validation suites.

Real portable-stage qualification uses one small bounded Grok `grok-4.5`/high
request and one small bounded agy `gemini-3.6-flash-high`/high request. Each
qualification requires successful canonical import and projection without a
full Hunt or AwR sidecar round. Transport smokes before namespace-race commit
`e6c8586` succeeded. Grok recorded completion
`ec8e2d309f4617279fd5814840114b4f8a67c08f0094c7adee7511643de25b6e`,
model envelope
`857ab8bc500f8a04d33389ec76c2fce88dd021364c458cede59e48b28a49d16f`,
and projected judge
`7366478a991437b0589789a784f127f393dd6bde3130a434811b50fd86963d80`.
Agy recorded completion
`ed8a293bc94c7f060e960e683edef36f89be75e556f9e22757b193c0486a8e5a`,
model envelope
`0e216d395b06328ad67c4fa6173e1152f2672a717e2457adfee3b2225b615284`,
and projected judge
`81114067dedaf34af0503d23d24ed88e05f9634440f62619246865bd9982ffc2`.
No residual attempt directory remained. Both providers require one bounded
requalification on the current revision before their OpenSpec tasks complete.
Claude is not invoked.

## Scope

The change does not alter provider selection, model or reasoning defaults,
portable-mirror inputs, output schemas, hard-complete qualification, or failed
payload retention. It does not modify `ledger.tsv`.
