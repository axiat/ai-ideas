# Idea Evaluator Rubric

<!-- Integrated from ~/.claude/skills/idea-evaluator/ for the weekly idea routine on 2026-07-03. -->

---
name: idea-evaluator
description: >-
  Evaluates a preliminary research idea against a five-dimension framework
  (Higher, Faster, Stronger, Cheaper, Broader) plus idea-lifecycle and
  student-capability matching, paradigm-shift probing, and a fatal-flaws
  audit. Returns a reviewer-style verdict. Use when the user has a draft
  research idea and asks whether it is worth pursuing, asks to 'evaluate
  this idea', 'score this idea', 'assess feasibility', 'novelty check',
  'is this a good research direction', or before committing to a paper
  scope.
license: CC-BY-4.0
---

# Idea Evaluator

## Overview

This skill evaluates a preliminary research idea from the combined
perspective of a top-venue reviewer and an experienced advisor. It
scores the idea against five improvement dimensions from the
idea-generation guide (Higher, Faster, Stronger, Cheaper,
Broader), matches the idea's lifecycle against the user's actual
capability and available hours per week, probes whether the idea has
paradigm-shift potential, flags fatal flaws, and returns one of three
verdicts: Strong Accept, Accept with Revisions, or Reject and Pivot.

The goal is to kill weak ideas before the student invests months, and to
shape promising-but-underdeveloped ideas into stronger forms before
writing begins.

## When to use this skill

- The user has a draft idea and asks whether it is worth pursuing.
- The user asks for novelty check, feasibility assessment, or scoring.
- Before the user commits to a paper scope or starts implementation.
- The user is comparing two or three candidate ideas and needs a
  structured trade-off.
- The user suspects scope creep and wants an external check.
- The user mentions 'evaluate this idea', 'score this idea', 'assess
  feasibility', or 'is this a good research direction'.

## When NOT to use this skill

- The user has already implemented the idea and is writing the paper.
  Use `intro-drafter`, `tech-paper-template`, or
  `benchmark-paper-template` instead.
- The user explicitly wants brainstorming of new ideas from scratch.
  Use plain conversation.
- The user asks for review of an existing manuscript. Use
  `pre-submission-reviewer`.
- The user asks to evaluate a benchmark contribution specifically.
  Use `benchmark-paper-template`.

## Core procedure

### Step 1: First impression and paper-type positioning

Read the user's idea description. In one paragraph, state whether the
idea reads as Novel Problem, Novel Method, or New Setting. Is the
story compelling in one sentence? If you cannot write that sentence,
the idea itself is probably not yet clear enough for evaluation; ask
the user to restate.

### Step 2: Fatal-flaws audit (early gate)

See: references/fatal-flaws.md for the ten canonical fatal flaws,
each with a detection rule and a defense strategy.

Run the fatal-flaws audit **before** the scoring steps rather than
after them. Identify at most two fatal flaws. For each, state the
flaw, cite the detection rule, and recommend a concrete defense.

**Short-circuit rule.** If any fatal flaw is tagged CRITICAL in the
severity taxonomy (single-handedly causes rejection, unfixable
within the lifecycle), stop here and emit the verdict directly:

- Verdict: Reject and Pivot.
- Output sections 1 (First impression), 2 (Fatal flaws with the
  CRITICAL flaw), and 8 (Verdict with the flaw-driven rationale)
  only.
- Do **not** run the five-dimension scoring, paradigm-shift probe,
  feasibility check, or integrity gate. Those would be decoration
  on a rejection.

If no CRITICAL flaw is found, continue to Step 3.

### Step 3: Lifecycle and capability matching

See: references/lifecycle-capability-matching.md for the six-category
lifecycle matrix, capability self-assessment rubric, and mismatch
recovery strategies.

Map the idea onto one of six categories (Application, Foundational
Theory, Cross-Disciplinary, Frontier Exploration, Data-Intensive,
Innovative Technique). Match against the user's declared capability
(effective hours per week, skill depth, theoretical versus applied
strength). Output a mismatch flag if lifecycle is shorter than the
user's realistic execution window.

### Step 4: Five-dimension scoring

See: references/five-dimensions.md for each dimension's entry
strategies, scoring rubric, and worked examples.

Score the idea on each of:

- **Higher**: effectiveness and accuracy gains.
- **Faster**: efficiency and cost reduction.
- **Stronger**: robustness, noise tolerance, generalisation.
- **Cheaper**: data, annotation, or solution cost reduction.
- **Broader**: cross-domain transplantation or unification.

Score each 1-10 with explicit evidence from the user's stated
contribution. Identify the two or three dimensions where the idea
has the highest ceiling and recommend emphasising those in the paper.

### Step 5: Paradigm-shift probe

See: references/paradigm-shift-probe.md for the four probing principles
(First Principles, Elephant in the Room, Technology Cycle, Hamming's
Rule); the per-principle deep dives are in references/paradigm-*.md.

Test the idea against four questions:

1. Does it challenge a hidden assumption the field takes for granted?
2. Does it address an elephant-in-the-room problem everyone sees but
   nobody wants to touch?
3. Does it ride a technology-cycle shift (for example, LLMs making a
   previously impractical approach now feasible)?
4. If this problem solved itself, would the field change meaningfully?
   (Hamming's Rule)

Two or more yes answers means the idea has disruptive potential. Note
that, and load the matching references/paradigm-*.md deep dive to
sharpen the probe's rationale.

### Step 6: Feasibility check

Against the user's stated resources (hardware, data access, team size,
engineering skills, timeline), assess:

- Compute risk: does the experiment fit on stated hardware?
- Data risk: is the required data accessible, or does it need
  expensive annotation or private sources?
- Engineering risk: does the implementation match the user's skill
  stack?
- Timeline risk: does the estimated end-to-end duration (coding,
  experiments, writing, revision) fit within the idea's lifecycle?

Anchor the assessment in the idea's stated minimal falsification
experiment (data × compute × expected signal, where a
missing signal kills the idea). Judge whether that experiment is
executable under the stated resource baseline and whether its signal
can genuinely falsify the claim; narrative feasibility claims without
an executable falsification experiment count as a MAJOR flaw and cap
the verdict at Accept with Revisions.

Scope the lifecycle and timeline judgment to the falsification
experiment plus a reasonable first-paper cut of the idea, not the
idea's maximal vision. A vision whose full scope exceeds a
single-person budget is not by itself a MAJOR flaw when the
first-paper cut stands; state the assumed first-paper cut in the
feasibility table.

If any risk is high, flag it explicitly with a suggested mitigation.

### Step 7: Integrity gate

Before emitting the verdict, run the checks in the Integrity gate
section below.

### Step 8: Final verdict

Issue one of three verdicts:

- **Strong Accept**: execute now. The bar is defined solely by the
  clear-accept standard in `brainstorming_policy.md` (done
  well, the idea likely reaches clear accept, ≈6,6,8 or above, at an
  A-tier venue); zero CRITICAL flaws, at most one MAJOR, capability
  match green, lifecycle fit. Dimension scores are diagnostic
  evidence for that judgment, not a mechanical SA threshold.
- **Accept with Revisions**: pivot the scope per recommendations
  before starting. Some dimensions weak, fixable flaws, or lifecycle
  mismatch that can be shortened.
- **Reject and Pivot**: do not pursue this version. Dominated by a
  prior benchmark or method, unfixable capability mismatch, or more
  than one fatal flaw.

Emit the evaluation in the Output format below.

## Integrity gate

Each bullet is tagged with an enforceability class. [inspection]
means the LLM can verify the bullet from the produced output alone.
[attestation] means the LLM states it has done the check, but the
user remains responsible for verification. [user-attest] means the
bullet is a user-side rule the skill cannot confirm.

Before returning the verdict:

1. **[inspection]** Every dimension score cites specific evidence
   from the user's stated contribution; no score is "gut feeling".
2. **[inspection]** Feasibility claims reference the user's stated
   resources, not generic assumptions.
3. **[inspection]** Novelty claims either cite specific prior work
   or are labelled "unverified; literature check required".
4. **[inspection]** Fatal flaws are specific and actionable; "this
   might not work" is not a flaw statement.
5. **[inspection]** Verdict is consistent with scoring and with the
   SA bar in `brainstorming_policy.md` (clear-accept standard): zero
   CRITICAL flaws, at most one MAJOR, and dimension scores that
   plausibly support a clear accept. Dimension counts alone neither
   grant nor block Strong Accept.
6. **[inspection]** Every 8+ dimension used to support Strong Accept
   satisfies the SA net-increment rule: it identifies the nearest
   payoff occupant, or records a genuine zero-hit search boundary and
   the strongest current baseline under the same metric and setting;
   only the attributable gain is scored.
7. **[inspection]** Paradigm-shift claim cites which probing
   question was answered positively.
8. **[attestation]** Lifecycle prediction is reasoned from the
   field's recent pace; the user should sanity-check against their
   own knowledge of the subfield before acting on it.

If any [inspection] check fails, downgrade the verdict and mark
the corresponding output section as "needs user attention". For
[attestation] bullets, the skill states the check was run and the
user confirms the result.

## Output format

### 1. First impression
- Paper type: <Novel Problem or Novel Method or New Setting>
- One-sentence story: <...>

### 2. Fatal-flaws audit (early gate)
| # | Flaw | Severity | Defense |
|---|---|---|---|
| 1 | ... | CRITICAL or MAJOR | ... |

*If any CRITICAL flaw is present, skip sections 3-7 and go to
section 8 with verdict Reject and Pivot.*

### 3. Lifecycle and capability match
| Aspect | User's input | Assessment |
|---|---|---|
| Idea category | ... | ... |
| Lifecycle | ... months | ... |
| Weekly effective hours | ... | ... |
| Fit | ... | Green or Yellow or Red |

### 4. Five-dimension radar
| Dimension | Score 1-10 | Evidence | Lift suggestion |
|---|---|---|---|
| Higher | ... | ... | ... |
| Faster | ... | ... | ... |
| Stronger | ... | ... | ... |
| Cheaper | ... | ... | ... |
| Broader | ... | ... | ... |

### 5. Paradigm-shift probe
| Probe | Yes or No | Rationale |
|---|---|---|
| First Principles | ... | ... |
| Elephant in the Room | ... | ... |
| Technology Cycle | ... | ... |
| Hamming's Rule | ... | ... |

Disruptive potential: <none, possible, strong>.

### 6. Feasibility
| Risk | Level | Mitigation |
|---|---|---|
| Compute | ... | ... |
| Data | ... | ... |
| Engineering | ... | ... |
| Timeline | ... | ... |

### 7. Integrity gate result
- Gate 1 through 8: <pass or fail>

### 8. Verdict
**<Strong Accept or Accept with Revisions or Reject and Pivot>**

Top three actions to take first:
1. ...
2. ...
3. ...

---

## [Reference] fatal-flaws.md

# Fatal flaws in research ideas

## Table of contents

1. What counts as fatal
2. Ten canonical fatal flaws
3. Detection rules
4. Defense strategies
5. Severity escalation logic

## 1. What counts as fatal

A fatal flaw is a problem that, if left unaddressed, causes rejection
from a top venue regardless of how well the rest of the paper is
executed. The bar is deliberately high: not every weakness is fatal.
Writing an idea off as fatally flawed stops the student from
pursuing it, so this section uses a conservative definition.

A fatal flaw has three signatures: it is observable in the Idea
description (not only discovered during experiments); it cannot be
fixed by stronger baselines or better writing alone; and reviewers
will flag it in the first review round.

The canonical count is at most two fatal flaws per idea. If the list
exceeds two, the idea's direction itself is wrong and a pivot is
required.

## 2. Ten canonical fatal flaws

### F1: No novelty versus the closest prior work

The idea replicates or barely varies a published baseline in the same
subfield. Reviewers frame this as "dominated by prior work X".

### F2: Wrong venue fit

The idea's contribution matches a different venue. A systems
contribution submitted to ICML is often rejected even if the work is
strong; a theory contribution submitted to VLDB meets the same fate.

### F3: Baseline is not the real baseline

The chosen baseline is weak or outdated. Beating a 2023 baseline in
2026 convinces no one; reviewers demand the current year's SOTA.

### F4: No compelling motivation

The idea's usefulness in the real world is unclear. The paper cannot
answer "who cares and why now"; reviewers call this "not motivated".

### F5: Capability mismatch

The student cannot execute the idea within its lifecycle. The idea is
valid, but the student lacks the skills, time, or resources to
complete it, causing a missed deadline or an incomplete paper.

### F6: Unverifiable claim

The idea depends on an empirical claim that cannot be verified from
the planned experiments. Example: "our method works across domains"
but no cross-domain experiments are in scope.

### F7: Ethical or data-access blocker

The idea needs data or human subjects the student cannot access.
IRB approval missing; proprietary data; privacy constraint.
Infrastructure makes execution impossible.

### F8: Overly ambitious scope

The idea promises too much in the contribution list. Example: "we
propose a benchmark, a new method, theoretical analysis, and a
deployed system" in a single paper. Each contribution is undercut by
the others; reviewers flag this as "unfocused".

### F9: Solution hunting for a problem

The idea begins with a technique the student wants to use and
searches for a problem it fits. Often produces papers where the
problem feels contrived and the experiments are not decisive.

### F10: No failure case considered

The idea treats the method as a silver bullet. No discussion of
where it fails, under what conditions, or what the limitations are.
Reviewers flag this as overclaiming.

## 3. Detection rules

### F1 detection

- Ask: what does this idea add over the single closest prior work?
- Red flag: the student cannot name a specific contribution in one
  sentence, or the contribution is "we use a bigger model" or "we
  combine two existing methods".

### F2 detection

- Ask: what is the target venue, and what are the top three papers
  from that venue's most recent edition?
- Red flag: the idea's contribution type does not appear in those
  three papers.

### F3 detection

- Ask: what is the strongest public result on the target benchmark
  as of the most recent three months?
- Red flag: the student's baseline is more than 12 months old or
  does not cite a 2026 result.

### F4 detection

- Ask: if this problem were solved tomorrow, who would benefit, and
  how much?
- Red flag: the answer is "other researchers studying this narrow
  problem" without naming an external beneficiary.

### F5 detection

- Run the matching logic in references/lifecycle-capability-matching.md.
- Red flag: two or more mismatch flags fire.

### F6 detection

- Ask: what experiment, if it produced a specific result, would prove
  the main claim?
- Red flag: the student cannot design such an experiment, or the
  experiment is out of scope.

### F7 detection

- Ask: is all required data currently accessible? Is IRB approval in
  place if human subjects are involved?
- Red flag: any required resource is missing and cannot be secured
  within the idea's lifecycle.

### F8 detection

- Count the contribution bullets in the proposed Introduction.
- Red flag: more than four, or the bullets span distinct paper types
  (benchmark + method + theory + system).

### F9 detection

- Ask: did the student encounter the problem in practice, or did they
  start from a technique they wanted to apply?
- Red flag: the student cannot name a concrete real-world failure
  that motivated the work.

### F10 detection

- Ask: under what conditions does the method fail, and what is the
  plan for the Limitations section?
- Red flag: the student cannot name two failure modes.

## 4. Defense strategies

For each flaw, a concrete defense. If the defense cannot be executed
within the idea's lifecycle, the flaw remains fatal.

| Flaw | Defense |
|---|---|
| F1 | Position against the closest prior work in one sentence. Name a specific axis on which the new idea dominates |
| F2 | Either switch the venue target to match the contribution type, or reshape the contribution to fit the original venue |
| F3 | Identify the latest state-of-the-art and add it as the primary baseline. If unavailable, document the recency cutoff and justify |
| F4 | Name a concrete external beneficiary (a user, a deployed system, a policy question) in the first paragraph of the Introduction |
| F5 | Follow recovery strategies in the lifecycle-capability-matching reference. Narrow scope, partner, reframe category, or pivot |
| F6 | Design the decisive experiment explicitly and put it at the top of the experiments plan. If infeasible, pivot the claim |
| F7 | Secure access before proceeding. For IRB, file early. For proprietary data, secure a partnership |
| F8 | Cut the contribution list to two or three items. Split remaining items into a follow-up paper |
| F9 | Restart from the problem side. Interview users, run a pilot study, or document a real failure that motivates the technique |
| F10 | Preregister two failure modes and a Limitations section before starting experiments. Include failure cases in the case-study section |

## 5. Severity escalation logic

After listing flaws and defenses, convert each flaw into a severity
tag using the following logic.

- **CRITICAL**: the flaw cannot be defended within the idea's lifecycle
  given current resources, or two or more MAJOR flaws are present.
- **MAJOR**: the flaw requires 2-4 weeks of dedicated work to defend.
- **MINOR**: the flaw can be addressed in under a week of writing or
  literature work.

Verdict implications:

- **Any CRITICAL flaw**: verdict is Reject and Pivot. Do not proceed
  with this version of the idea.
- **Two or more MAJOR flaws**: verdict is Accept with Revisions.
  Defend all flaws before starting experiments.
- **At most one MAJOR flaw and any MINOR flaws**: compatible with
  Strong Accept, subject to other evaluation steps.

---

## [Reference] five-dimensions.md

# Five-dimension scoring framework

## Table of contents

1. Origin and purpose
2. Dimension 1: Higher (Effectiveness)
3. Dimension 2: Faster (Efficiency)
4. Dimension 3: Stronger (Robustness and Generalisation)
5. Dimension 4: Cheaper (Data or Solution Cost)
6. Dimension 5: Broader (Cross-Domain and Unification)
7. Scoring rubric
8. Summary table
9. Common scoring failures

## 1. Origin and purpose

This framework comes from the methodology for generating
incremental research ideas given a strong baseline. The insight is:
do not hunt for new problems with a solution in hand; instead, take a
well-defined baseline and ask on which of five axes it can be
improved. Each axis has canonical entry strategies and concrete
examples.

The same framework flips naturally into an evaluation lens: given an
idea, score how strongly it advances each axis versus the current
baseline. Strong ideas usually dominate on one axis and hold their own
on at least one more. Weak ideas are vague on four or more axes.

Scores that support Strong Accept follow the single SA net-increment
rule in `brainstorming_policy.md`. When the same payoff is occupied,
name the nearest payoff occupant and score only the attributable gain
over it. For a genuine zero hit, record the search boundary, name the
strongest current baseline under the same metric and setting, and score
only the attributable gain over that baseline. Published or deployed
payoffs are not credited again.

## 2. Dimension 1: Higher (Effectiveness)

### Definition

Improves accuracy, quality, or effectiveness metrics over the
strongest current baseline.

### Canonical entry strategies

- **Information or modality augmentation**. Does the baseline ignore
  an input signal that would help? Example: Text-to-SQL models using
  only schema plus query may improve significantly when fed data
  distribution or domain documents.
- **Feedback-driven refinement**. Can execution feedback (compiler
  errors, runtime exceptions, test failures) drive iterative
  self-correction? Example: LLM code-generation success rates jump
  when errors from the execution environment are returned to the
  agent for self-reflection.
- **Error-driven root-cause analysis**. Run the baseline, cluster
  failures, identify the dominant failure mode, and build a module
  that targets it. Example: if Text-to-SQL failures concentrate on
  complex JOIN operations, design a sub-agent that decomposes JOIN
  logic.

### How to score

- 9-10: the idea proposes a principled new input signal or feedback
  loop that no prior work exploits, with a plausible path to
  multi-point accuracy gains.
- 6-8: the idea combines known signals or applies a known feedback
  mechanism in a new setting; expected gains are modest but real.
- 3-5: the idea is a standard prompt-engineering or fine-tuning
  variant; gains will likely be within noise of stronger baselines.
- 1-2: the idea does not obviously advance effectiveness at all.

### Grounded example

Alpha-SQL (ICML 2025): introduces MCTS-based inference-time search
for Text-to-SQL. Higher score: 8. Evidence: principled search
mechanism that no prior open-source Text-to-SQL work had applied;
multi-point accuracy gains on BIRD. Lift: pair with schema
augmentation for additional signal.

## 3. Dimension 2: Faster (Efficiency)

### Definition

Reduces wall-clock time, token cost, memory footprint, or compute
budget while preserving effectiveness.

### Canonical entry strategies

- **Caching and experience reuse**. Cache successful trajectories so
  repeated tasks do not replan from scratch. Example: an LLM agent
  that retrieves and reuses past successful plans for similar tasks.
- **Parallelisation and decoupling**. Break a long serial pipeline into
  independent sub-tasks handled by specialised agents running in
  parallel. Example: a multi-agent data-analysis framework with one
  agent per step (ingestion, transformation, visualisation).
- **Early exit and dynamic routing**. Route simple cases to cheap
  models, escalate only hard cases. Example: a cascade that uses a
  small model for filtering and only invokes a large model on
  ambiguous instances.

### How to score

- 9-10: the idea identifies a large, well-quantified efficiency gap
  and proposes a principled mechanism that closes it (more than 3x
  speedup with no accuracy loss).
- 6-8: the idea targets a known efficiency bottleneck with a plausible
  mechanism; gains are 1.5x-3x.
- 3-5: the idea mentions efficiency but lacks a specific mechanism;
  gains will likely be marginal.
- 1-2: the idea does not address efficiency.

### Grounded example

LEAD (VLDB 2026): eliminates per-iteration full-dataset inference in
iterative instruction-tuning data selection. Faster score: 9.
Evidence: zero-additional-inference overhead is a principled gap
closed; replaces expensive utility-score computation with training-
loss signal already computed.

## 4. Dimension 3: Stronger (Robustness and Generalisation)

### Definition

Maintains performance under noise, out-of-distribution inputs, or
cross-domain transfer.

### Canonical entry strategies

- **Noise tolerance and fault tolerance**. Handle malformed or
  ambiguous inputs gracefully. Example: intent-clarification modules
  that ask users for disambiguation instead of failing silently.
- **Exception recovery**. Detect API failures, unexpected outputs, or
  tool crashes and retry with alternatives. Example: an agent that
  falls back to a simpler tool when the primary one returns an
  unexpected response.
- **Decoupled representations**. Separate general reasoning from
  domain-specific knowledge so the same reasoning module transfers
  zero-shot to new domains.

### How to score

- 9-10: the idea directly addresses a known brittleness (ambiguity,
  OOD, domain shift) with a principled mechanism and plans
  cross-domain evaluation.
- 6-8: the idea mentions robustness with a concrete mechanism but
  evaluates on a single domain.
- 3-5: the idea pays lip service to robustness without a mechanism.
- 1-2: the idea does not address robustness.

### Grounded example

An agent framework that decouples planning from domain-specific APIs
and ships cross-domain evaluation: Stronger score 8. Evidence: clear
zero-shot transfer story and principled decoupling.

## 5. Dimension 4: Cheaper (Data or Solution Cost)

### Definition

Reduces data annotation cost, training cost, or deployment cost while
preserving effectiveness.

### Canonical entry strategies

- **LLM-based data synthesis**. Use a strong foundation model to
  synthesise labelled training data or simulate agent trajectories,
  reducing human annotation demand.
- **Active learning with human in the loop**. Select the smallest set
  of samples for human review that maximises model improvement,
  rather than annotating the whole corpus.
- **Knowledge distillation**. Distill a large model's reasoning paths
  into a small deployable model, preserving accuracy at lower
  inference cost.

### How to score

- Apply the SA net-increment rule in `brainstorming_policy.md`; these
  bands score attributable cost reduction over the required occupant
  or same-metric, same-setting baseline, not an already published
  payoff.
- 9-10: the idea produces a high-quality dataset at a fraction of
  naive-annotation cost, or deploys at an order-of-magnitude lower
  cost than the baseline.
- 6-8: the idea reduces cost by a factor of 2-5 through a known
  mechanism.
- 3-5: cost reduction is mentioned but not quantified.
- 1-2: the idea does not consider cost.

### Grounded example

StatQA's reverse-synthesis pipeline (NeurIPS 2024 D&B) reduced
annotation cost by generating questions from verified answers,
side-stepping manual question writing. Cheaper score 8.

## 6. Dimension 5: Broader (Cross-Domain and Unification)

### Definition

Transplants a mature idea from one domain to another, or unifies a
fragmented set of tasks under a single framework.

### Canonical entry strategies

- **Cross-domain transplantation**. Take a mature technique from one
  field and apply it to another. Example: database query optimisers
  bring cost estimation and plan caching to LLM-agent planning.
- **Generalisation and unification**. Unify a family of tasks (Text
  to SQL, Text to Python, Text to Chart) under a single data-agent
  framework with one shared underlying structure.

### How to score

- 9-10: transplants a mature technique in a non-obvious direction with
  a plausible mechanism; or unifies three or more previously
  fragmented task families.
- 6-8: transplants a known idea; or unifies two task families.
- 3-5: suggests a connection to another domain without concrete
  mechanism.
- 1-2: the idea is siloed.

### Grounded example

AFlow (ICLR 2025): transplanted search-based workflow generation from
neural-architecture-search-style methods to agent pipeline design.
Broader score 8. Evidence: crossed a domain boundary with a principled
mechanism.

## 7. Scoring rubric

For each dimension, assign an integer 1-10 based on the tiers above.
Then aggregate as follows:

These aggregations are diagnostic heuristics. The verdict itself is set
by the fatal-flaws logic (Step 5: any CRITICAL → Reject; two or more
MAJOR → cap at Accept with Revisions) and the clear-accept SA bar in
`brainstorming_policy.md`, not by dimension counts alone.

- Top dimension at 8+ and a second dimension at 6+: this is the
  paper's thesis. Emphasise these two in the Introduction.
- Three or more dimensions at 5 or below: the idea is thin — find the
  dimension where it really shines and sharpen it, or pivot.
- All five dimensions at 4 or below: this is the arithmetic shadow of a
  thin idea, not a Reject rule. Re-read first (§9 Deflation); if the idea
  really is weak, the Reject is set by the value assessment or a CRITICAL
  flaw — never by the tally alone.
- No single dimension reaches 7: the clear-accept SA bar in
  `brainstorming_policy.md` requires a standout dimension (around 8), so
  an idea with nothing at 7+ cannot meet it. That is the bar applied, not
  a separate count veto — it removes SA and flags a vague idea to sharpen,
  but does not by itself choose between Accept-with-Revisions and Reject.

## 8. Summary table

| Dimension | Core goal | Entry strategies | Illustrative example |
|---|---|---|---|
| Higher | Accuracy and effectiveness gains | Information or modality augmentation; feedback-driven refinement; error-driven root-cause analysis | MCTS inference in Alpha-SQL |
| Faster | Efficiency and cost reduction | Caching and experience reuse; parallelisation and decoupling; early exit and dynamic routing | Zero-inference data selection in LEAD |
| Stronger | Robustness and generalisation | Noise and fault tolerance; exception recovery; decoupled representations | Intent-clarification agents |
| Cheaper | Data or solution cost | LLM-based synthesis; active learning; knowledge distillation | Reverse-synthesis in StatQA |
| Broader | Cross-domain and unification | Cross-domain transplantation; abstraction and unification | Workflow search in AFlow |

## 9. Common scoring failures

- **Inflation**: scoring every dimension 7 or 8 "to be generous"
  destroys the signal. Use 5 as the default and justify upward.
- **Deflation**: scoring every dimension 3 or 4 signals that you did
  not understand the idea; re-read.
- **Uncited evidence**: a score without a specific sentence from the
  user's idea pointing at why is not a real score.
- **Generic lift suggestions**: "try a different prompt" is not a
  lift. Name the entry strategy from this file.
- **Score and verdict mismatch**: the SA bar is the clear-accept
  standard in `brainstorming_policy.md`, not a dimension count. If
  every dimension is 7 or below, re-examine whether the idea truly
  meets clear-accept and adjust either the scores or the verdict —
  but dimension counts alone neither grant nor block Strong Accept.

---

## [Reference] lifecycle-capability-matching.md

# Idea lifecycle and student capability matching

## Table of contents

1. Why lifecycle matters
2. The six-category lifecycle matrix
3. Capability self-assessment rubric
4. Matching logic
5. Mismatch detection and recovery
6. Worked examples

## 1. Why lifecycle matters

Every research idea has an expiry date. In fast-moving subfields like
LLM agents or Text-to-SQL, a purely applied idea may have a 3-to-6-
month lifecycle: if the student cannot finish coding, experiments,
writing, and submission in that window, another group likely will,
and the paper becomes hard to publish. In slower-moving subfields
like foundational theory, the lifecycle stretches past 12 months, but
the methodological depth required also stretches.

Lifecycle is not the same as difficulty. A short-lifecycle idea can be
executed by a skilled coder in 3 months; a long-lifecycle idea may
require 18 months of theoretical work before the first paper. The
mismatch question is always: can this student finish this idea within
its lifecycle?

The canonical rule: speed is not an excuse for low
quality. Fast iteration and submission means efficient work with full
quality control, not corner-cutting.

## 2. The six-category lifecycle matrix

| Category | Lifecycle | Suited student profile | Rationale |
|---|---|---|---|
| Application research | Short (3-6 months) | Strong coder, fast executor, can ship experiments quickly | Competitive, quickly obsolete fields; time-to-submission is decisive |
| Foundational theory | Long (6-12 months) | Strong mathematical base, deep thinker | Proofs and model building require sustained work |
| Cross-disciplinary | Medium (6-9 months) | Student with prior non-CS background | Domain expertise plus CS skills enables novel connections; often HCI venues |
| Frontier exploration | Short-to-medium (3-9 months) | Both theory and experiment capable, self-directed | New subfields require quick experiments and deep analysis |
| Data-intensive | Medium (6-12 months) | Strong data analysis, solid engineering | Data pipelines and model iteration are the critical path |
| Innovative technique | Long (12+ months) | Deep base, willing to challenge existing methods | Requires cross-field innovation; longer cycles are accepted |

## 3. Capability self-assessment rubric

Ask the user (or infer from their input) the following four inputs.
Record each on the scale shown.

### 3.1 Weekly effective hours

Effective hours mean focused, deep-work hours without meetings or
context switches. A rough calibration:

- Under 10 hours per week: top-venue publication is unlikely within a
  typical idea lifecycle unless execution is exceptional.
- 10-25 hours per week: feasible for medium-lifecycle ideas with
  disciplined scope management.
- 25-40 hours per week: feasible for short-lifecycle fast-moving
  ideas.
- 40+ hours per week: feasible for all lifecycles, but still bounded
  by the skill-depth axis below.

### 3.2 Skill depth

Rate strongest relevant skill on a 1-5 scale:

- Coding and experiment engineering (Python, PyTorch, deployment).
- Theoretical and mathematical maturity (proofs, derivations, model
  formulation).
- Data engineering (pipelines, annotation tools, cleaning at scale).
- Systems programming (low-level optimisation, concurrency).
- Domain knowledge (the target application area).

A student with 5 in coding and 2 in theory should prefer Application
or Data-intensive categories. A student with 2 in coding and 5 in
theory should prefer Foundational theory or Innovative technique.

### 3.3 Theoretical versus applied preference

On a 1-5 scale from pure-applied (1) to pure-theoretical (5). This is
a preference signal, not an ability signal: a student may be capable
of theory but happier shipping systems. Respect the preference; it
drives sustainability over the lifecycle.

### 3.4 Infrastructure access

Binary flags:

- Access to at least four large GPUs (A100 or equivalent): yes or no.
- Access to proprietary data or annotation budget: yes or no.
- Advisor or team weekly review cadence: yes or no.

Infrastructure access shortens lifecycle fit for certain categories
(large-GPU access is often required for data-intensive and
application research at the frontier).

## 4. Matching logic

Given a proposed idea and a capability profile:

1. Classify the idea into one of the six categories.
2. Look up the lifecycle range for that category.
3. Compute realistic execution time from capability:
   - coding + debugging time (estimate from scope)
   - experiment time (estimate from data and compute availability)
   - writing time (default: 3-6 weeks for a first draft)
   - revision time (default: 2-4 weeks)
4. Compare realistic execution time against lifecycle. If realistic
   time exceeds lifecycle, flag a mismatch.

## 5. Mismatch detection and recovery

### Detection flags

- **Lifecycle-short mismatch**: realistic execution exceeds 1.3x the
  lifecycle midpoint. High risk.
- **Capability-depth mismatch**: the category demands a skill at level
  4 or 5 that the student rates at 2 or below. Medium-to-high risk.
- **Infrastructure mismatch**: the category requires GPU or data
  access the student lacks. High risk.
- **Preference mismatch**: the category conflicts with the student's
  theoretical-applied preference. Medium risk; watch for drop-off.

### Recovery strategies

- **Narrow the scope**. Cut the idea to a well-scoped subproblem that
  fits the capability. Example: instead of a full Text-to-SQL Agent,
  tackle only schema-linking under ambiguity.
- **Partner with a collaborator**. Pair the student with someone whose
  skills complement the missing depth.
- **Change category**. Reframe the idea to fit a more compatible
  category. Example: reframe a foundational-theory idea as a
  data-intensive empirical study.
- **Extend the lifecycle**. Only feasible in slow-moving subfields;
  dangerous in fast-moving ones.
- **Switch the idea entirely**. If three or more mismatch flags
  exist, the idea is a poor fit. Better to pivot than to push.

## 6. Worked examples

### Example A: fit

- Idea: incremental improvement of Text-to-SQL accuracy via
  MCTS-based inference-time search.
- Category: Application research (short lifecycle, 3-6 months).
- Student capability: 5 in coding, 3 in theory, 20 weekly hours, four
  RTX 3090 GPUs available.
- Matching: coding (5) meets the demand; compute (four GPUs) is
  sufficient for BIRD-scale benchmarks; 20 weekly hours is tight but
  feasible with focused scope.
- Verdict: Green. Recommend a single-metric success criterion and a
  4-month timeline.

### Example B: mismatch

- Idea: a new theoretical framework for query-planner convergence
  under distribution shift.
- Category: Foundational theory (long lifecycle, 6-12 months).
- Student capability: 5 in coding, 2 in theory, 15 weekly hours,
  strong applied preference.
- Matching: theory (2) is below demand; preference is applied, not
  theoretical. Two mismatch flags.
- Verdict: Yellow. Reframe as an empirical study of planner behaviour
  under distribution shift, moving to Data-intensive category.
  Alternatively, partner with a theory-strong collaborator.

### Example C: high-risk fit

- Idea: a cross-disciplinary HCI study of how domain experts interact
  with AI-generated charts.
- Category: Cross-disciplinary (medium, 6-9 months).
- Student capability: background in statistics, 3 in coding,
  25 weekly hours, no IRB approval yet, no participant pool.
- Matching: coding (3) is adequate; infrastructure (no IRB, no pool)
  is blocking. One high-severity infrastructure mismatch.
- Verdict: Yellow. Secure IRB and recruit participants before writing
  any code; otherwise the 9-month window will be consumed by
  logistics.

---

## [Reference] paradigm-elephant.md

# Elephant-in-the-Room hunting

## Table of contents

1. Core idea
2. Technique A: real-world pain-point extraction
3. Technique B: corner-case cataloguing
4. Worked example: large-scale schema linking
5. Worked example: multi-agent collaboration for complex tasks
6. What to ignore

## 1. Core idea

Every research community has problems it knows are important but
systematically avoids. Sometimes the problems are too hard for the
current toolset. Sometimes they do not fit the publication conventions.
Sometimes everyone assumes someone else will eventually solve them. If
a researcher can formalise one of these elephants into a tractable
academic problem, the reframing often becomes a new subfield.

The hunting technique is to look outside the academic-benchmark bubble
and inside the corner cases where current methods fail silently.

## 2. Technique A: real-world pain-point extraction

### Procedure

1. Identify three practitioners (industry engineers, deployed-system
   operators, or domain experts) who use the field's outputs in
   production.
2. Ask them: what is the single biggest pain-point they hit every
   week that the academic literature does not address?
3. For each pain-point, ask whether it is a research problem or an
   engineering detail.
4. For research-grade pain-points, ask: can this be formalised as a
   benchmark, a new problem setting, or a measurable task?

### Signals a pain-point is research-grade

- Multiple practitioners independently raise it.
- Current academic work implicitly assumes the problem does not
  exist.
- A rigorous formalisation would require new evaluation metrics, not
  just a new dataset in an existing format.
- The pain-point has been stable for at least two years; it is not a
  passing artefact of one tool version.

## 3. Technique B: corner-case cataloguing

### Procedure

1. Run the field's current best method on inputs drawn from the long
   tail of real-world data.
2. Catalogue the failure modes: classify each failure by root cause.
3. Identify failure modes that are common, systematic, and not
   addressed in the literature.
4. For each, ask: is this a Prompt-Engineering patch problem, or a
   fundamental limitation of the current paradigm?
5. For fundamental limitations, propose a reframing that addresses
   the root cause.

### Signals a corner case is an elephant

- Frequent: appears in at least 10 percent of real-world inputs.
- Systematic: the same class of input consistently fails.
- Unaddressed: searching the literature surfaces no direct work on
  this failure mode.
- Root-level: quick fixes do not stick; each patch exposes the next
  layer of the same failure.

## 4. Worked example: large-scale schema linking

- **Academic benchmark focus**: Text-to-SQL evaluation on single-
  table or small-schema problems with clean column names.
- **Practitioner pain-point**: real databases have thousands of
  tables with inconsistent or cryptic naming; even DBAs do not know
  which tables to use for a given question.
- **Formalisation**: large-scale schema linking or schema retrieval
  as a first-class research problem, with its own benchmarks and
  methods.
- **Impact**: an entire subfield of retrieval-augmented Text-to-SQL
  opens up, previously invisible under the clean-schema assumption.

## 5. Worked example: multi-agent collaboration for complex tasks

- **Academic focus**: single-agent LLMs judged on short, well-bounded
  tasks.
- **Practitioner pain-point**: complex real business workflows need
  coordination across specialist roles; single agents spiral into
  long chains, hallucinations, or loops.
- **Corner-case analysis**: failure is not random; it correlates with
  tasks requiring sustained state, cross-function expertise, or
  rollback recovery.
- **Formalisation**: multi-agent collaboration frameworks or Standard
  Operating Procedure driven agent workflows, each with their own
  evaluation protocols.
- **Impact**: a new class of papers around agent orchestration rather
  than agent intelligence.

## 6. What to ignore

- **Engineering pain-points**: "my deployment tool is slow" is
  valuable to the practitioner but is not research-grade.
- **One-off failures**: a single viral bug is not an elephant.
- **Already-formalised problems**: if the literature already has a
  subfield for this, the elephant has been captured. Move on.
- **Pain-points that are solvable by the next model release**: if
  scaling alone is expected to solve it, the reframing is premature.
- **Elephants outside the user's reach**: some elephants require
  institutional infrastructure (industry partnerships, proprietary
  data) a PhD student cannot access alone. Record on the Hamming's
  list and defer.

---

## [Reference] paradigm-examples.md

# Canonical disruptive examples

## Table of contents

1. How to read these examples
2. Example 1: Transformer architecture
3. Example 2: Adaptive Query Processing
4. Example 3: Agent-driven data cleaning
5. Example 4: Multi-agent collaboration workflows
6. Lessons across examples

## 1. How to read these examples

Each example below traces a published or widely-adopted line of work
back to the disruptive reframing at its root. For each example, the
relevant principle (First Principles, Elephant in the Room,
Technology Cycle, Hamming's Rule) is identified. The purpose is to
show what a disruptive reframing looks like in the wild and to
calibrate paradigm-shift analysis against real precedents.

Examples 2, 3, and 4 also appear in the
[advanced disruptive-innovation handbook](handbook/02_Idea_Generation/2.3_%E8%BF%9B%E9%98%B6_%E5%A6%82%E4%BD%95%E5%81%9A%E9%A2%A0%E8%A6%86%E5%BC%8F%E5%88%9B%E6%96%B0.md)
as illustrations for the principle they anchor.

## 2. Example 1: Transformer architecture

- **Context**: circa 2017, the machine-translation community's
  strongest models used complex CNN or RNN/LSTM architectures with
  attention as a secondary mechanism.
- **Consensus assumption**: sequential recurrence is required to
  model long-range dependencies.
- **Reframing (First Principles)**: what if attention alone is
  sufficient? The Google team dropped all recurrence and offered a
  pure-attention architecture, the Transformer.
- **Outcome**: the basis of essentially all subsequent large language
  models and the dominant architecture across NLP, vision, and
  speech.
- **Principle**: First Principles (assumption audit on recurrence)
  plus Technology Cycle (GPU compute enabled large all-attention
  models).

## 3. Example 2: Adaptive Query Processing

- **Context**: traditional database query optimisation.
- **Consensus assumption**: a query plan must be fully determined
  before execution; the optimiser uses static statistics.
- **Reframing (First Principles)**: what if statistics change during
  execution? Let the plan adapt to observed intermediate cardinalities.
- **Outcome**: a multi-decade research programme on runtime re-
  optimisation, progressive cardinality estimation, parametric
  query plans.
- **Principle**: First Principles (assumption audit on static plans)
  plus Elephant in the Room (production systems always saw plan
  failures due to estimation errors; academic world avoided this).

## 4. Example 3: Agent-driven data cleaning

- **Context**: traditional data cleaning uses standalone scripts
  (rules, heuristics, small models) independently of downstream use.
- **Consensus assumption**: cleaning is a preprocessing step
  decoupled from downstream tasks.
- **Reframing (Return to Purpose)**: the goal of cleaning is correct
  downstream analysis. Let an LLM agent observe downstream task
  performance and iteratively rewrite cleaning strategies.
- **Outcome**: agent-driven data cleaning becomes a research
  direction separate from classical cleaning; task-conditioned
  cleaning collapses the preprocessing step into a feedback loop.
- **Principle**: First Principles (return-to-purpose) plus Technology
  Cycle (LLM agents make the feedback loop tractable).

## 5. Example 4: Multi-agent collaboration workflows

- **Context**: single-agent LLM systems for complex business tasks.
- **Corner-case observation**: single agents loop, hallucinate, or
  spiral on multi-role, long-horizon tasks such as building a full
  web application end-to-end.
- **Consensus assumption**: more prompt engineering or longer
  context will fix multi-step failures.
- **Reframing (Elephant in the Room)**: single-agent failure is not
  a prompt-tuning problem; it is a coordination problem. Design a
  multi-agent framework with role separation and standard operating
  procedures.
- **Outcome**: a research programme around agent orchestration,
  multi-agent frameworks, agentic workflows, coordination protocols.
- **Principle**: Elephant in the Room (avoided hard problem of long-
  horizon multi-role tasks) plus Technology Cycle (LLM agent
  availability).

## 6. Lessons across examples

- All four examples combine two of the four principles. Single-
  principle reframings are often weaker; the strongest disruptive
  work triangulates between assumptions, pain-points, technology
  shifts, and important problems.
- Three of the four required a Technology Cycle element to be
  actionable. Without the shift, the reframing is either premature
  (the technology does not yet exist) or too late (the community has
  already absorbed it).
- None of the four is a one-paper contribution. Each opened a
  research programme that spanned years. A paradigm-shift analysis
  should aim for programme-scale reframings, not individual-paper
  tweaks.
- All four were risky at the time. The Transformer paper faced
  scepticism; Adaptive Query Processing was initially dismissed as
  impractical; agent-driven cleaning is still debated; multi-agent
  frameworks are still proving their value. Disruptive work lives
  with reception risk.

---

## [Reference] paradigm-first-principles.md

# First Principles thinking

## Table of contents

1. Core idea
2. Technique A: assumption audit
3. Technique B: return to purpose
4. Worked example: Adaptive Query Processing
5. Worked example: Agent-driven data cleaning
6. Common misuses

## 1. Core idea

Do not start from the literature. Start from the physical or
mathematical essence of the problem. Existing solutions inherit
assumptions from the moment they were designed, and those assumptions
may have quietly stopped being true. First Principles thinking
strips a problem back to what is intrinsically required and asks
whether the path from requirement to current solution still holds.

The technique is deceptively simple: identify an assumption, test
whether it still holds, and, if it does not, propose a new solution
that does not require it.

## 2. Technique A: assumption audit

### Procedure

1. Write down the three most widely-held assumptions in the subfield.
   State each as a single declarative sentence.
2. For each assumption, write its original justification (why was it
   adopted in the first place?).
3. For each assumption, ask what has changed since that justification
   was established.
4. Rank the assumptions by fragility. An assumption whose justification
   depends on a condition that no longer holds is a candidate.
5. Pick the most fragile assumption and imagine a system that does
   not make it. What becomes possible?

### What a good assumption looks like

- Specific. "Data distribution is static" is better than "things are
  fixed".
- Historical. The audit names when and why the assumption was
  adopted.
- Testable. You can point to a concrete paper or system that still
  relies on it.
- Fragile. A recent technology or context shift weakens the
  justification.

## 3. Technique B: return to purpose

### Procedure

1. Ask: what is the user-facing goal of this task? Not the metric,
   the goal.
2. Ask: what are the intermediate steps the field has built to reach
   the goal?
3. Ask: for each intermediate step, is it required, or is it an
   accident of how the field grew?
4. Collapse any intermediate step that is not strictly required and
   ask what the result looks like.

### What return-to-purpose surfaces

- Steps that exist because they were needed under the old hardware
  regime, but are not needed now.
- Steps that exist because the original tools could only produce
  outputs at a certain level of abstraction.
- Steps that exist because specialist roles historically split the
  task.

## 4. Worked example: Adaptive Query Processing

- **Assumption**: "Database query plans must be fully determined
  before execution." Justification: the cost model expects static
  statistics and static workloads. Originally adopted in the 1970s.
- **What changed**: workloads and data distributions became dynamic;
  modern storage can observe intermediate results cheaply.
- **Reframing**: what if the query plan can change during execution
  based on observed data? This breaks the static-plan assumption and
  creates the Adaptive Query Processing research programme.
- **Impact**: decades of downstream work on runtime re-optimisation,
  parametric query plans, learned cardinality.

## 5. Worked example: Agent-driven data cleaning

- **Goal revisit**: the actual goal is to make downstream analysis or
  modelling correct. Data cleaning is an intermediate step.
- **Step audit**: traditional cleaning writes scripts independent of
  the downstream task. This was necessary when downstream tasks could
  not observe cleaning decisions.
- **Reframing**: what if an LLM agent observes the downstream
  task's feedback and iteratively rewrites cleaning strategies? The
  intermediate step collapses into a closed loop with the downstream
  task.
- **Impact**: Agent-driven Data Cleaning becomes a new direction
  rather than a tweak to existing cleaning pipelines.

## 6. Common misuses

- **Assumption theatre**: naming an assumption and pretending to
  break it, while the reframing still secretly relies on the same
  assumption. Check whether your reframing depends on the assumption
  anywhere in its pipeline.
- **Straw assumptions**: auditing assumptions that nobody in the
  field still holds. The audit should target live consensus, not
  historical views.
- **Unbounded reframing**: proposing a reframing that requires
  technology that does not exist yet. If the reframing needs
  something we will have in five years, record it on the Hamming's
  Rule list and move on; do not treat it as actionable today.
- **Skipping the justification audit**: if you cannot state why the
  assumption was originally adopted, you cannot judge whether the
  justification still holds.

---

## [Reference] paradigm-hamming.md

# Hamming's Rule: the personal top-10 problem list

## Table of contents

1. Origin and why it matters
2. Building the list
3. The prepared-mind practice
4. How often to refresh
5. Sustained-problem selection
6. The template

## 1. Origin and why it matters

Richard Hamming, in the talk "You and Your Research", argued that
every researcher should carry a running list of the 10 to 20 most
important problems in their field, regardless of whether the
researcher currently has any idea how to solve them. The list is the
scaffolding for prepared-mind thinking: when a new technique, paper,
or idea crosses the researcher's desk, the first question becomes,
"does this unlock one of my top-10 problems?"

Without the list, novel techniques get absorbed into incremental work
on whatever the researcher happens to be doing. With the list, novel
techniques route toward the highest-leverage targets the researcher
has pre-committed to.

## 2. Building the list

### Initial pass

1. Brainstorm 15-20 candidate problems in the researcher's broad
   area. Ignore solvability; include problems the researcher has no
   current approach for.
2. Cut the list to 10-12 by applying three filters:
   - **Importance**: would solving this change the subfield in a
     non-trivial way?
   - **Durability**: will this problem still be important in five
     years, or is it a passing artefact of current tooling?
   - **Personal connection**: does the researcher care about this
     problem enough to think about it while walking or showering?
3. Write each surviving problem as one declarative sentence, not a
   vague phrase.

### Good problems on the list

- Each one is specific enough that progress is recognisable.
- Each one is important enough that an advisor would nod.
- Each one is unsolvable by today's methods (at least not obviously).
- Each one is in the subfield the researcher works in, not adjacent
  subfields they do not know well.

### Problems that should not be on the list

- "Make LLMs better". Too vague.
- "Publish three papers this year". Outcome goal, not problem.
- "Beat GPT-4 on MMLU". Metric chasing, not a durable problem.
- "Replicate paper X with my own code". Exercise, not research.

## 3. The prepared-mind practice

Once the list exists, the daily practice is:

- Every time a new paper, algorithm, or technology crosses the
  researcher's desk, ask: does this help with any problem on the
  list?
- Keep a running notebook of matches. Most matches will be false
  positives, but occasionally a match is real.
- When a real match appears, it is usually obvious in retrospect; the
  technique unlocks a step in a problem the researcher has already
  been mulling.
- Dedicate at least a few hours a week to working on top-three
  problems directly, even if they seem hopeless.

## 4. How often to refresh

- Every six months, review the list.
- Cross off problems that have been solved by the community.
- Add new problems that have emerged.
- Move problems up or down based on current field pace.
- Delete problems the researcher no longer cares about.

The list should feel alive. A list that has not changed in two years
is either remarkable foresight or a signal that the researcher has
stopped paying attention to the field.

## 5. Sustained-problem selection

A crucial rule: do not only work on problems the researcher knows how
to solve. Those problems produce incremental papers (which are
valuable, see `references/five-dimensions.md` in the `idea-evaluator`
skill) but they will not produce landmark work.

Reserve 10-20 percent of research time for a top-five problem the
researcher does not know how to solve. Most attempts will fail
visibly. Some will make partial progress. One in 10 might crack open
and define the researcher's career.

Avoid the opposite failure mode: spending 100 percent of time on
unsolvable top-10 problems with no incremental output. That produces
an unpublishable PhD. The balance is roughly 70-20-10 between
incremental, broader, and disruptive work (see the portfolio section
of `idea-evaluator`'s `paradigm-shift-probe.md`).

## 6. The template

```markdown
# My top-10 problems, as of <YYYY-MM-DD>

1. <single-sentence problem statement>. Last touched: <date or never>.
2. ...
3. ...
4. ...
5. ...
6. ...
7. ...
8. ...
9. ...
10. ...

## Problems added this refresh

- ...

## Problems removed this refresh

- ... (reason: solved, obsolete, or no longer personally interesting)

## This month's attention allocation

- Top-three problem I spent the most hours on: #<n>
- Matches from other-field papers that seemed promising: <list>
```

Keep this file under version control. The history is a record of how
the researcher's taste evolved, and it is surprising how often an
early entry, seemingly unconnected at the time, turns out to anchor a
late-career project.

---

## [Reference] paradigm-shift-probe.md

# Paradigm-shift probe

## Table of contents

1. Incremental versus disruptive
2. The four probing principles
3. Scoring the probe
4. When to delegate to handbook 2.3
5. Risk and reward trade-off
6. Portfolio strategy for a PhD

## 1. Incremental versus disruptive

Most published ideas are incremental: they take an existing baseline
and push it on one or two of the Higher, Faster, Stronger, Cheaper,
Broader dimensions. Incremental work is the backbone of a PhD career
and can absolutely reach top venues.

Disruptive work reshapes the problem itself rather than its solution
it challenges a hidden assumption, surfaces a previously ignored
problem, or rides a technology-cycle shift that makes a new approach
possible. Disruptive ideas carry higher reward (a strong disruptive
paper defines the subfield for years) and higher risk (most disruptive
attempts fail or take years to pay off).

The probe is not a gate. Even a strictly incremental idea can justify
a top-venue paper. The probe's purpose is twofold:

- Identify disruptive potential early, so the student can choose to
  pursue it consciously rather than accidentally reducing it to
  incremental work mid-project.
- Flag ideas that look disruptive but are actually incremental (or
  vice versa), so the Introduction framing matches the reality.

## 2. The four probing principles

### 2.1 First Principles

Does the idea challenge an assumption the field takes for granted?

Signal that yes: the idea starts with "everybody assumes X, but what
if X is wrong" and produces a specific X. Examples of X include
assumptions like "iterative methods require full-dataset inference
every round" (LEAD broke this), or "visualisation quality reduces to
aesthetics or correctness alone" (VisJudge-Bench broke this).

Signal that no: the idea starts with a known technique and proposes a
variant. No assumption is challenged, only a hyperparameter or a
training recipe is tweaked.

### 2.2 Elephant in the Room

Does the idea address a problem the community sees but avoids?

Signal that yes: the paper includes a section that says "this problem
has been known for years but nobody has touched it because X", and
the paper provides a path past X. Examples: agent evaluation beyond
toy tasks; reproducibility of RLHF across labs; domain shift in
production deployment.

Signal that no: the problem is new and small, rather than old and
large.

### 2.3 Technology Cycle

Does a recent technology shift enable this idea to work now, when it
could not before?

Signal that yes: the idea depends on capability that did not exist
two years ago. Example: LLM agents only became practical after
instruction-tuned base models matured; reinforcement-learning from
execution feedback only became tractable after cheap-inference
infrastructure. An idea that could have been done five years ago but
was not, is usually incremental.

Signal that no: the idea could have been attempted at any point in
the last decade with minor modifications.

### 2.4 Hamming's Rule

If this idea succeeded, would it change the community's top priority
list?

Signal that yes: the idea is a top-three problem in the student's
advisor's list; colleagues light up when they hear it; reviewers would
call it a landmark paper if done well.

Signal that no: the idea is niche, or its success improves a metric
that only a handful of people care about.

## 3. Scoring the probe

Answer each of the four principles as Yes, Partial, or No. Count Yes
as 2, Partial as 1, No as 0. Total score 0-8.

- **0-2**: pure incremental. Frame as incremental in the Introduction;
  emphasise Higher or Faster.
- **3-4**: incremental with disruptive seeds. Explicitly call out the
  disruptive aspect in the Introduction to capture reader attention.
- **5-6**: disruptive potential. Consider reading handbook 2.3 to
  deepen the framing.
- **7-8**: genuine paradigm-shift candidate. Definitely study handbook
  2.3; the Introduction should lead with First Principles framing.

## 4. When to delegate to handbook 2.3

Recommend the user read handbook 2.3 when:

- Probe score is 5 or higher.
- Probe score is lower but the user explicitly wants to pursue a
  disruptive angle.
- The idea has a First Principles signal with a concrete hidden
  assumption that the user can state in one sentence.
- The paper is targeting a venue that rewards paradigm-shift framing
  (ICLR Outstanding, NeurIPS Spotlight, VLDB Best Paper).

Do not delegate when the idea is genuinely incremental; forcing
disruptive framing on incremental work weakens the paper.

## 5. Risk and reward trade-off

Disruptive work has three distinct risk categories:

- **Execution risk**: the idea is right but the experiments fail. Higher
  for disruptive because the mechanism is unproven.
- **Reception risk**: the idea is right and the experiments succeed,
  but reviewers do not recognise the contribution. High for early
  disruptive work.
- **Timeline risk**: the idea requires more iterations than an
  incremental project; lifecycle fit tightens.

Against these, the rewards of a successful disruptive paper:

- Career-defining impact on a single paper.
- A new research programme around the framing.
- Citation compounding over 5+ years.

Rule of thumb: if the student has already published at least one
incremental top-venue paper, they have earned the credibility to
attempt a disruptive project. If not, anchor the first paper in
incremental territory and let disruptive come second.

## 6. Portfolio strategy for a PhD

A healthy PhD research portfolio across 3-5 years usually has:

- 60-70 percent incremental work: steady publications, builds the
  student's technical depth and writing muscle.
- 20-30 percent cross-disciplinary or Broader work: transplants a
  known idea to a new context; lower risk than disruptive, higher
  reward than incremental.
- 10-20 percent disruptive work: one or two ambitious projects over
  the PhD; not every one will land, but a single success pays for
  several failed attempts.

Use the paradigm-shift probe to classify each candidate project and
balance the portfolio explicitly. The worst outcome is four disruptive
projects that all fail to publish; the second worst is four
incremental projects that publish but fail to differentiate the
student from their peers.

---

## [Reference] paradigm-technology-cycle.md

# Technology Cycle Foresight

## Table of contents

1. Core idea
2. Technique A: hardware or platform shift adoption
3. Technique B: unlimited-resource thought experiment
4. Worked example: non-volatile memory databases
5. Worked example: free-token large language models
6. How to tell a real shift from hype

## 1. Core idea

Disruptive innovation often coincides with a generational shift in
the underlying hardware or platform. The wrong response to such a
shift is to port the old algorithms onto the new hardware. The right
response is to ask what architectural assumption the shift
invalidates, and design from scratch for a world where that
assumption no longer holds.

The method has two working techniques: watch for the shift that has
just happened, and imagine the shift that would happen if resources
became unbounded.

## 2. Technique A: hardware or platform shift adoption

### Procedure

1. Identify two or three technology shifts in the last 18 months
   that touch the subfield. Shifts can be hardware (GPU, NVM, RDMA),
   software (foundation models, serverless), or cost-curve
   (inference cost, storage cost).
2. For each shift, list the architectural assumptions it changes.
3. For each assumption, ask: does the current best method in the
   subfield still depend on this assumption?
4. If yes, the current method is due for a paradigm refresh. Propose
   a redesign that treats the shift as the new baseline.

### Signals a shift is generational

- Cost curve changes by an order of magnitude, not a factor.
- New capability emerges that was impossible before (not just more
  of the same).
- Production deployments start adopting the shift before academia
  publishes on it.
- At least two major vendors or open-source projects converge on the
  new technology.

## 3. Technique B: unlimited-resource thought experiment

### Procedure

1. Pick a specific resource: compute, memory, bandwidth, inference
   tokens, annotation budget.
2. Imagine the resource becoming 10000 times cheaper at zero latency.
3. Ask: how does the subfield's dominant paradigm change?
4. Look for the design decisions in current paradigms that only make
   sense because that resource was expensive.
5. Propose a reframing that treats the resource as free.

### What this surfaces

- Algorithms that exist only to save the now-abundant resource.
- Pipeline stages that exist only to compress intermediate data.
- Carefully engineered features that exist only because learning
  from raw input was prohibitive.

The classic precedent: deep learning's explosion after GPUs made
abundant compute available. Feature engineering, which was once
central, became peripheral.

## 4. Worked example: non-volatile memory databases

- **Shift**: byte-addressable non-volatile memory (NVM) arrives;
  storage is now persistent and byte-level addressable at near-DRAM
  speeds.
- **Port approach (wrong)**: run traditional disk-oriented databases
  on NVM with minor tuning.
- **First-principles approach (right)**: if storage is persistent and
  byte-addressable, does the database still need a Buffer Pool? Does
  it still need Write-Ahead Logging in its traditional form?
- **Reframing**: a new generation of databases designed for
  persistent memory, collapsing the page-based I/O model and
  rethinking crash recovery.

## 5. Worked example: free-token large language models

- **Thought experiment**: assume LLM tokens cost nothing and context
  windows are unbounded. What changes in data processing?
- **Consequences**:
  - Pre-processing pipelines that exist to fit data into limited
    context become unnecessary.
  - Fine-tuning becomes optional; prompting can absorb the role
    played by fine-tuning datasets.
  - Retrieval systems become simpler; the model can just read the
    corpus.
  - Evaluation pipelines can become interactive rather than batch.
- **Reframing**: research on unified, prompt-first data analysis
  frameworks; agent-driven analysis where the agent reads the whole
  corpus.

## 6. How to tell a real shift from hype

- **Real shift**: cost curve has actually moved; production systems
  have deployed the new capability for at least six months; multiple
  independent teams are exploring it.
- **Hype**: cost curve has moved in one vendor's roadmap slides;
  production systems have tried and reverted; exploration is
  concentrated in a single research group.
- **Premature shift**: the capability exists but at scale that only
  very large labs can access. Reframing is valid on paper but
  unpublishable by a typical student without institutional backing.
  Record on the Hamming's list.
- **Late shift**: the shift happened three or more years ago and the
  subfield has already absorbed it. Reframing opportunity is closed;
  focus on incremental gains within the new paradigm instead.

---

## [Reference] worked-examples.md

# Worked evaluations

## Table of contents

1. How to read these examples
2. Example A: Alpha-SQL (ICML 2025), incremental, Strong Accept
3. Example B: AFlow (ICLR 2025), cross-domain, Strong Accept
4. Example C: LEAD (VLDB 2026), new problem, Strong Accept
5. What each example illustrates

## 1. How to read these examples

Each example below is a retrospective evaluation of a published paper
as if the idea had been submitted for evaluation at the pre-writing
stage. The format matches the skill's output contract. The examples
ground the scoring rubric and the paradigm-shift probe in concrete
work. They are not predictions; the papers are already published and
accepted at their venues.

Sources: `handbook/06_Case_Studies/6.1_ICML_2025_Alpha-SQL*.md`,
`handbook/06_Case_Studies/6.2_ICLR_2025_AFlow*.md`,
`handbook/06_Case_Studies/6.3_VLDB_2026_LEAD*.md`.

## 2. Example A: Alpha-SQL (ICML 2025), incremental, Strong Accept

### Input (as if submitted for evaluation)

- Research area: Text-to-SQL with large language models.
- Core idea: introduce MCTS-style inference-time search to improve
  SQL generation accuracy on complex queries, without fine-tuning.
- Resources: strong coder, access to several A100 GPUs, 25 effective
  hours per week, five-month lifecycle target.

### Evaluation

**1. First impression.** Novel Method. One-sentence story: bring
search into the inference-time loop for an open-source Text-to-SQL
agent. Compelling for ICML because LLM agents are in scope and SQL
generation is a hot evaluation target.

**2. Lifecycle and capability match.**

| Aspect | Input | Assessment |
|---|---|---|
| Idea category | Application research | Lifecycle 3-6 months, fits five-month target |
| Weekly hours | 25 | Adequate for aggressive scope |
| Compute | Several A100 | Adequate for inference-only search |
| Fit | | Green |

**3. Five-dimension radar.**

| Dimension | Score | Evidence | Lift |
|---|---|---|---|
| Higher | 8 | MCTS is a principled search mechanism not used in open-source Text-to-SQL | Combine with schema augmentation |
| Faster | 3 | Search costs more tokens than direct generation | Add an early-exit for simple queries |
| Stronger | 5 | Search helps on hard queries but not on noisy inputs | Add intent-clarification |
| Cheaper | 4 | No new training data; but search adds inference cost | Cache search trajectories |
| Broader | 6 | Idea transfers to other constrained-generation tasks | Mention extensions |

**4. Paradigm-shift probe.**

| Principle | Answer | Rationale |
|---|---|---|
| First Principles | Partial | Challenges "more fine-tuning is the only way" |
| Elephant in the Room | No | Inference-time search is known in other contexts |
| Technology Cycle | Partial | Depends on instruction-tuned base models |
| Hamming's Rule | No | Important to SQL subfield but not community top-three |

Probe score: 2. Pure incremental; frame as Novel Method.

**5. Feasibility.** All risks green.

**6. Fatal flaws.** None at the fatal level. One MAJOR item: baseline
must include the latest closed-source model (otherwise F3).

**7. Integrity gate.** Pass.

**8. Verdict.** Strong Accept. Emphasise Higher and Broader in the
Introduction; preregister the Faster cost as a known limitation.

## 3. Example B: AFlow (ICLR 2025), cross-domain, Strong Accept

### Input

- Research area: LLM agent workflow generation.
- Core idea: cast workflow construction for code-generation agents as
  a search problem over symbolic-operator graphs, borrowing ideas
  from search-based neural architecture search.
- Resources: strong coder, GPU access, 30 hours per week, six-month
  lifecycle target.

### Evaluation

**1. First impression.** Novel Method with cross-domain framing. A
search-based view of agent workflows has not appeared in ICLR before.

**2. Lifecycle and capability match.** Green across all axes.

**3. Five-dimension radar.**

| Dimension | Score | Evidence | Lift |
|---|---|---|---|
| Higher | 7 | Accuracy gains on HumanEval when workflow is optimised | Add LiveCodeBench |
| Faster | 6 | Search overhead, but amortised over repeated tasks | Cache workflows |
| Stronger | 6 | Transfers across code-gen benchmarks | Test cross-model |
| Cheaper | 5 | No new data required | Use smaller operator library |
| Broader | 9 | Cross-domain transplantation from NAS to agent workflow design | Unify with tool-use agents |

**4. Paradigm-shift probe.**

| Principle | Answer | Rationale |
|---|---|---|
| First Principles | Partial | Challenges "prompt engineering is enough for agents" |
| Elephant in the Room | Partial | Workflow fragmentation is a known pain point |
| Technology Cycle | Yes | LLMs make operator-graph search meaningful |
| Hamming's Rule | Partial | Important for agent research |

Probe score: 5. Disruptive seeds present. Recommend reading handbook
2.3 for deepening the framing around "workflow as a first-class
object".

**5. Feasibility.** Green.

**6. Fatal flaws.** None. One MAJOR item: overclaim risk if the
operator library is too narrow (F10). Defense: preregister two
failure modes on unseen operator types.

**7. Integrity gate.** Pass.

**8. Verdict.** Strong Accept. Lead with Broader in the Introduction;
consider reframing as a new subfield (workflow-as-a-search-space).

## 4. Example C: LEAD (VLDB 2026), new problem, Strong Accept

### Input

- Research area: efficient LLM instruction tuning via data selection.
- Core idea: iterative data selection without any additional
  full-dataset inference, using the training loss already computed in
  the fine-tuning loop.
- Resources: strong coder, limited GPU budget, 20 hours per week, six-
  month target.

### Evaluation

**1. First impression.** Novel Problem / New Setting. The paper
defines the setting "zero-additional-inference iterative selection"
as the contribution, and provides the method.

**2. Lifecycle and capability match.**

| Aspect | Input | Assessment |
|---|---|---|
| Idea category | Data-intensive | Lifecycle 6-12 months; fits six-month tight |
| Weekly hours | 20 | Adequate if scope disciplined |
| Compute | Limited | This is fine because the method avoids full inference |
| Fit | | Green, slightly aggressive |

**3. Five-dimension radar.**

| Dimension | Score | Evidence | Lift |
|---|---|---|---|
| Higher | 7 | Matches or beats iterative selection baselines | Add safety-tuning benchmarks |
| Faster | 9 | Zero-extra-inference is an order-of-magnitude cost win | Quantify in wall-clock time |
| Stronger | 6 | Robust across instruction-tuning corpora | Add cross-domain test |
| Cheaper | 8 | Reuses training signal already computed | Release the selection code |
| Broader | 6 | Transfers to RLHF and safety tuning | Note in Discussion |

**4. Paradigm-shift probe.**

| Principle | Answer | Rationale |
|---|---|---|
| First Principles | Yes | Challenges "iterative methods require full inference" |
| Elephant in the Room | Partial | Selection cost is a known pain, not widely addressed |
| Technology Cycle | Partial | Instruction tuning only recently became a bottleneck |
| Hamming's Rule | Partial | Important to scale efficiency |

Probe score: 5. Disruptive seeds. Recommend reading handbook 2.3 for
deepening the First Principles framing.

**5. Feasibility.** Yellow on timeline; six months is aggressive for
a Data-intensive paper. Mitigation: preregister the main claim and
cut scope if experiments overrun at the four-month mark.

**6. Fatal flaws.** None at fatal level. One MAJOR: the central
training-loss-as-utility-signal claim must be rigorously tested
(F6). Defense: design the decisive ablation upfront.

**7. Integrity gate.** Pass.

**8. Verdict.** Strong Accept. Frame as Novel Problem in the
Introduction; lead with the First Principles probe result.

## 5. What each example illustrates

- Alpha-SQL is the canonical incremental pattern: high Higher score,
  low probe, Strong Accept with tight scope.
- AFlow shows the Broader-dominant pattern: a cross-domain
  transplant that earns its top-venue slot on novelty of framing.
- LEAD shows the New Problem pattern: a paradigm-shift probe at 5
  that is best served by a separate framing exercise before writing.

None of the three examples is Reject. A realistic evaluation corpus
includes many Reject outcomes, often driven by F1 (no novelty) or F5
(capability mismatch). Those examples are intentionally not
published; no archive source exists for them.
