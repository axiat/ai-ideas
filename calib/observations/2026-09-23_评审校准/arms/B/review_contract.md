# Bounded Review Contract

Review one candidate against its supplied prior-work evidence. Use `strong-accept`, `accept-w-rev`, or `reject`.

## Gates

- Judge this preliminary candidate at the idea stage. Apply disqualifying gates first, then assess Strong Accept, then Accept with Revisions; otherwise Reject. Every positive recommendation needs supplied evidence. A verdict cap is only an upper limit.
- Any CRITICAL finding requires Reject. CRITICAL is a specifically supported failure of the central contribution or execution premise with no credible repair within the first-paper scope and resources. Two or more MAJOR findings forbid Strong Accept; they neither create CRITICAL automatically nor award AwR. Count independent failure conditions. Repeated descriptions of one factual gap count once. Distinct necessary evidence or a defect surviving repair of another remains separate even if one broad redesign could address both.
- A direct occupying result caps novelty unless the remaining attributable difference independently supports clear accept. Identify exactly what the nearest work covers: phenomenon, mechanism, target-setting result, or payoff. Broad high overlap alone leaves the verdict undecided; evaluate the supported residual contribution. Zero search hits alone do not establish value. Keep the narrower assumption-removal requirements below.
- Missing or weak prior-work coverage is MAJOR and forbids Strong Accept.
- Feasibility is judged from the minimal falsification experiment and its reasonable first-paper scope: data access/scale, compute, expected signal, relevant controls, and kill condition. A missing or non-executable experiment is MAJOR. A larger eventual vision adds no independent defect.
- A mismatched estimand is MAJOR. Match validation to the claim: causal/mechanism claims require identification against substantive alternatives; descriptive measurement and formal analysis need support for their own claims, without an unclaimed internal causal explanation. Performance claims require capable baselines. Faithfully adapt the nearest method and justified strong/simple alternatives; a new protocol need not have an existing public SOTA ranking. Control non-treatment differences and report resource/information tradeoffs caused by the treatment. Preserve relevant baseline capabilities.
- An ordinary measurement-only probe is at most borderline. The existing exception permits a strong, estimand-aligned prior for a surprising, net-new finding that independently supports clear accept, with executable discriminating validation. An important new explanation or correction of a specific evaluation conclusion can supply that knowledge payoff without an unrelated repair. Published anomalies are only hypothesis priors; repetition of a known failure or a larger guessed effect is insufficient. A repair used to support clear accept must itself supply a new, attributable payoff over its nearest occupier.
- Strong Accept requires a substantial clear-accept case, approximately 6,6,8 or better, with the first-paper experiment feasible for a team of typically 2–3 researchers (up to ~10 with justification) without pretraining-scale compute.
- Accept with Revisions requires a supported residual contribution of independent research value below the SA standard. Enumerated revisions and completion conditions must credibly yield at least borderline work within the specified minimal experiment, first-paper scope and resource budget, or the supported contribution ceiling must already be borderline. Name both retained value and blockers. Easy controls, cheap experiments, or useful engineering alone are insufficient. Clarification and defensible scope reduction are allowed; inventing another decisive contribution requires a new evaluation.
- Reject applies when neither positive standard is supported or a disqualifying flaw exists. Occupation justifies rejection when the remaining difference lacks independent value. Insufficient positive value may justify Reject with zero CRITICAL and even one MAJOR. The current verdict establishes no academic conclusion about every future formulation and grants no automatic retry eligibility.
- Completed experiments strengthen a case. Unrun experiments alone are neither MAJOR nor grounds to cap the verdict. Require observations, analysis, derivation or argument appropriate to the contribution and an executable falsification plan. Expected gains and kill thresholds remain predictions until observed; unsupported payoff assumptions still fail.
- A transferred mechanism needs zero target-setting hits, a nontrivial adaptation forced by the setting, and an evidence-backed prospect of a new, attributable payoff sufficient for clear accept to reach Strong Accept. Support that prospect with relevant observations, a checkable derivation or analysis, or a causal argument appropriate to the actual claim, paired with feasible falsification, capable baselines, relevant attribution controls and a kill condition. Causal explanations require identification against substantive alternatives; formal guarantees or descriptive results require validation of their stated claims. Existing results strengthen the case; unrun experiments alone are neither MAJOR nor grounds to cap the verdict. Unsupported gains or validation that cannot distinguish the claimed contribution fail this gate.
- An assumption-removal candidate needs low overlap, two directly supporting crack-evidence verifications, an external forcing constraint, and a decisive bounded experiment. Missing conditions return it to ordinary calibration.
- A verified internal-history summary can support duplication, lineage, or failure-pattern findings. It cannot establish academic novelty.
- State actual decision-making findings and their consequences in Reason, supported by the supplied artifacts. Use Occupation for the specific covered claim and residual difference, Experiment/Estimand for validity, and Payoff for independently supported value. Additional flaws need their own evidence. Distinguish missing support from contrary evidence. An additional experiment affects the verdict only if it addresses a named load-bearing uncertainty.

## Output

The response contains exactly one artifact, `review-markdown`. The host
derives `output/verdict.tsv` from that markdown as exactly one four-field row:

```text
candidate-id<TAB>verdict<TAB>MAJOR-count<TAB>one-sentence reason
```

The `review-markdown` response artifact is materialized as
`output/review.md` and contains:

```text
# <candidate-id>
Verdict: <verdict>
CRITICAL: <count>
MAJOR: <count>
Headline: <one sentence>
Occupation: <closest work and attributable difference>
Experiment: <bounded falsification assessment>
Estimand: <alignment assessment>
Payoff: <net-new attributable payoff>
Feasibility: <first-paper resource assessment>
History: <verified relation and evidence IDs, or scoped no-match receipt, or unavailable>
Assessment: <one strict JSON object as specified below>
Reason: <decision>
```

Every factual claim names the supplied artifact or evidence ID that supports it. Unsupported content is omitted.

## Assessment contract

The host binds the current output format using frozen `review_protocol.json`.
Emit all 13 nonempty lines above in order. `Assessment:` contains one JSON
object on one physical line with exactly these keys:

```json
{"coverage":"not-covered","coverage_evidence":[{"source":"candidate","quote":"exact candidate text"},{"source":"prior-work","quote":"exact prior-work text"}],"causes":[{"code":"design-invalid","evidence":[{"source":"candidate","quote":"exact text supporting the design finding"}]}]}
```

The example shows shape only. Use quotations copied exactly from the supplied
candidate's `candidate_markdown` or `prior_work.md`; placeholder quotes fail
validation. Each reference has only `source` and `quote`. Sources are exactly
`candidate` and `prior-work`. The host checks every quote against the frozen
text. Select concise evidence so the entire Assessment line remains within
4096 UTF-8 bytes. Quotes establish provenance; your explanation must still
show how they support the finding.

`coverage` concerns the central claim and decisive contribution within the
supplied research record:

- `covered`: prior work covers the decisive contribution and the remaining
  difference retains no independent research contribution. Cite the candidate
  claim and the actual covering result. A general mechanism alone is insufficient.
- `not-covered`: the record supports at least one central difference that has
  not been fully covered. Its significance and feasibility remain separate
  judgments. This is scoped to supplied evidence, without universal novelty certainty.
- `unknown`: the supplied evidence cannot establish either conclusion.
- `disputed`: supplied evidence directly conflicts about coverage of the same
  decisive contribution and the conflict remains unresolved. Complementary
  design and value flaws do not create such a conflict.

For `covered` and `not-covered`, `coverage_evidence` must contain at least one
candidate quote and one prior-work quote. For `covered`, a prior-work quote
must also include the actual http(s) URL of the covering source from that
record. For `disputed`, provide at least two distinct references and explain
the conflict in Occupation. `unknown` may have an empty coverage_evidence list.

`causes` contains the findings that prevent this seat from recommending SA.
Each entry has exactly `code` and `evidence`. Allowed codes:

- `contribution-covered`: the decisive contribution is fully occupied.
- `value-insufficient`: the supported residual value falls below the relevant
  positive standard; specify whether it supports AwR or fails both standards.
- `evidence-insufficient`: a named load-bearing claim lacks adequate support.
- `design-invalid`: the proposed validation cannot identify its stated claim.
- `infeasible`: a named first-paper resource or execution dependency fails.
- `unknown`: the record cannot establish a determinate decision cause.

Each known cause needs at least one exact supporting reference. Codes are
unique within the list. `unknown` is the only cause when used and may have
empty evidence. It requires `coverage=unknown` or `coverage=disputed`.
Cause codes group decision reasons; their count is independent of MAJOR count.
Two independent design failures can use one `design-invalid` entry with both
references while still requiring `MAJOR: 2` and separate explanations.

- SA requires `coverage=not-covered` and empty causes, while preserving the
  existing CRITICAL/MAJOR rules. A remaining non-disqualifying MAJOR belongs
  in the review text; causes lists only findings that block SA.
- AwR requires `coverage=not-covered` and nonempty causes, excluding `unknown`
  and `contribution-covered`. Its positive residual-value requirement still applies.
- Reject requires nonempty causes. `coverage=covered` and
  `contribution-covered` must occur together. A new difference can still merit
  Reject for insufficient value, evidence, design or feasibility.

Reject missing fields, extra keys, duplicate JSON keys, unsupported values,
inconsistent verdict/coverage/causes, and quotes absent from the frozen inputs.
The host treats these as invalid output; they cannot become a neutral ballot
or select the older protocol. Academic uncertainty must be expressed using
the valid fields above.
