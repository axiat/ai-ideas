# Brainstorming and Acceptance Policy

This policy is mandatory for the weekly idea routine and overrides the default verdict calibration in `rubric.md`.

## Divergence Requirements

- `research_context.md` is optional inspiration, not a constraint. At most 1–2 ideas per round may directly concern the current DSRL or π0.5 stack. The rest must range freely across World Models, VLA, and embodied AI.
- Priority: pure novelty that changes the problem definition or foundational mechanism, with Transformer-level ambition; then problem discovery with initial investigation; then incremental improvements to existing directions.
- At most one evolution or recheck idea may appear per round; the two share a slot. Evolution may repair only an `accept-w-rev`, `overlap=low` row with an experimental-design failure; an occupied or novelty-capped story is irreparable. Recheck may resubmit unchanged either an AwR idea with weak prior-work research or a `reject`, `category=evidence-incomplete` row where unanimous Strong Accept votes were reduced only by a hard evidence gate. Begin with `Evolved from:` or `Recheck:` and state the eligibility condition. A story gets one recheck; another failure makes it permanent. Every resubmission receives fresh prior-work research and review and inherits no votes. `novelty-dead` rejects, including direct hits, `overlap=high`, and CRITICAL findings, cannot return.
- Each round's 10 raw candidates must include at least one attempt at `Form: remove-load-bearing-assumption`, the fifth valid form. All five structured fields are mandatory for a complete attempt. Use exactly one marker: `Assumption-Removal Attempt: complete I1` or `Assumption-Removal Attempt: incomplete — <candidate>; blocked by: <field>`. Completion satisfies the raw-candidate quota only; selection for deep research depends solely on quality. An incomplete marker is not an idea and does not enter the ledger. Leave it incomplete rather than fabricate rhetorical compliance or hollow evidence; `Crack Evidence Verification` and reviewers evaluate rhetorical candidates under the ordinary standard.

## Theme Vocabulary

World Models - Architecture / World Models - Training Objectives / VLA - Architecture / VLA - Training Paradigms / Action Representation / Data Engines / Evaluation and Diagnostics / Efficiency and Systems / Safety and Robustness / Cross-Domain Transfer / Human-Robot Interaction and Deployment

Before generation, count ledger rows by theme. At least two ideas in the round must use one of the three lowest-inventory themes; the threshold is the third-lowest count and includes ties. In the hunt loop, the orchestrator mechanically validates this rule and the vocabulary.

## Divergence Lenses

The orchestrator randomly draws one lens from this list plus three blank cards. A blank card injects no lens. Lenses are starting points when needed, not constraints: candidates may depart from them, and lens adherence is not mechanically scored.

Proposition lenses place novelty in a claim about the world rather than an enumerable mechanism × domain cell, making single-paper occupation less likely but still requiring adversarial research. Axis-transfer lenses move a mechanism along an enumerable axis and are usually occupied or incremental; the pool therefore weights proposition lenses more heavily.

- **Explain an accepted phenomenon:** Give a competing causal explanation for a robust fact, failure, scaling law, or emergence pattern and design an experiment that distinguishes it from the prevailing explanation. Frame the headline as a proposition such as “X is caused by Z rather than Y,” not a pairing.
- **Remove a load-bearing assumption:** Identify a component or assumption treated as necessary and remove or reverse it. This is the fifth valid form and requires structured fields plus at least two lines of `Crack Evidence:`. Empty rhetoric is evaluated under the ordinary standard.
- **Name an unnamed problem or estimand:** Formalize a real phenomenon that is assumed to exist but has not been named or measured. Novelty lies in the problem definition, not a mechanism.
- **Change the evaluation object:** Evaluate data, environments, or metrics, or identify and quantify an ignored confounder. The headline must claim that an existing metric measures the wrong quantity, rather than merely repeating evaluation on another dataset.
- **Unify or separate:** Give one mechanism to two problems normally treated separately, or decouple a system normally treated as one.
- **Move one axis (use cautiously):** Move a sensor modality, data source, timescale, scale axis, learning signal, output representation, closed-loop experience (deployment feedback, RL post-training, human-in-the-loop, or test-time adaptation), or classic CS principle. This is usually an incremental near transfer and should be used only when it forces a new proposition, such as removing an assumption or overturning an explanation; otherwise expect at most `accept-w-rev`.

## Valid Idea Forms

Ideas may be preliminary and need not contain a complete method or experimental program. Five forms are valid:

1. **New mechanism or new problem:** The problem is real, prior work does not occupy it, and at least one exploration path exists.
2. **Problem discovery with initial mathematical investigation:** Identify a bottleneck or anomaly and add an initial formulation, complexity analysis, or back-of-the-envelope derivation.
3. **Transfer of a classic computer-science principle:** Apply architecture or computer-organization mechanisms to World Model or VLA training or inference, including locality (caching or tiling), log-domain computation (logarithmic number systems or Mitchell approximation), pipelining, speculative execution, sparsity, memory hierarchies, or prefetching. This form is incremental by default and appears frequently in the death list. A classic principle is mechanism material, not pure novelty, unless the target domain exposes a unique failure mode, the transfer overturns an explanation, or all three mechanism-transfer Strong Accept exceptions below hold. Otherwise its expected ceiling is `accept-w-rev`.
4. **Bottleneck-localization experiment:** Use a small probe to locate a bottleneck and support later work. A pure diagnostic or probe is capped at borderline because its five-dimensional profile rarely reaches 8+ in any dimension. Strong Accept requires either a repair arm whose successful intervention improves the result or a strong prior for a surprising finding, plus the net-new payoff rule below. The same cap applies to diagnostic proposition lenses such as explaining a phenomenon or changing the evaluation object. Poster-scale work does not consume a pure-novelty slot.
5. **Remove a load-bearing assumption (Transformer-scale wager):** Identify a component or assumption treated as necessary and remove or reverse it. Preserve these exact fields for mechanical validation:
   - `Form: remove-load-bearing-assumption`
   - `Assumption to Remove:` the presumed-essential component or assumption and the mainstream methods that depend on it
   - `Why It Can Be Removed Now:` the new evidence, tool, or data that makes removal newly feasible
   - `Forcing Constraint:` an external pressure such as compute, latency, data cost, or deployment; elegance or curiosity does not qualify
   - `Crack Evidence:` at least two lines, each containing a URL and one sentence explaining how the assumption is weakening; these claims remain unverified until the research process reads them
   - `Minimal Falsification Experiment:` a decisive experiment where an absent signal proves the assumption load-bearing and kills the idea; a measurement-only probe is invalid

## Review Calibration

This section is the repository's sole definition of Strong Accept. It overrides conflicting language in `rubric.md`, role prompts, or sidecars. Calibrate to reviewers at NeurIPS, ICLR, CoRL, RSS, or ICRA.

- **Strong Accept** means a substantial probability of clear accept, approximately 6,6,8 or better. Oral or spotlight potential, approximately 8,8,6+ with no strong objection, is preferred but not required.
- **Accept with Revisions** means borderline accept with a low ceiling or limited value.
- Novelty and prior occupation carry the highest weight. Engineering completeness may be preliminary, but novelty uncertainty requires directed prior-work research and never passes by intuition.
- **Strong Accept net-new payoff rule:** Any load-bearing basis used to establish clear accept or remove a diagnostic ceiling—including a repair or application payload, an 8+ standout dimension, or a surprising prior—counts only when it is a new, attributable payoff of this idea. If the same payoff has already been demonstrated, use the nearest occupying work as the baseline. With a genuine zero hit, use the strongest current baseline for the same metric and setting within the recorded search boundary. Published or deployed gains cannot be counted again: an occupied payload breaks the ceiling only if the incremental payoff over its nearest occupier independently reaches clear accept; a published anomaly is only a hypothesis prior, and the new discriminating experiment or explanation must independently reach clear accept.
- **Fatal-flaw gate:** At least two MAJOR findings forbid Strong Accept, so at most one MAJOR is allowed. Any CRITICAL finding requires Reject and Pivot. This matches the severity escalation in `rubric.md`.
- **Feasibility baseline:** one researcher and 1×H100 80G. If the idea is otherwise compelling, reviewers may assess it with up to 8×A100 but must state that dependency. Lifecycle and feasibility apply to the `Minimal Falsification Experiment:` and a reasonable first-paper phase, not the maximal vision. If one researcher cannot complete either within the lifecycle, the ceiling is Accept with Revisions. A larger ultimate vision does not independently count as MAJOR; reviewers must state the first-paper scope.
- Feasibility depends only on the idea's `Minimal Falsification Experiment:` with data × compute × expected signal and an explicit kill condition. A missing or non-executable experiment is MAJOR and caps the verdict at Accept with Revisions. Narrative feasibility claims do not count.
- A known mechanism transferred to a new domain is below Strong Accept by default. It may break the cap only when all three conditions are evidenced: zero target-domain hits in independent research; nontrivial adaptation forced by target-domain constraints rather than dataset substitution; and a realized signal sufficient for clear accept. Missing any condition retains the cap.
- **Assumption-removal channel:** The wager's unverified status is not itself MAJOR only if its falsification experiment is cheap, decisive, and kills the wager when the signal is absent. Strong Accept additionally requires all four conditions: zero headline hits with `overlap=low` from independent research; at least two `supports` results under `Crack Evidence Verification` that directly show the assumption weakening; an explicit external `Forcing Constraint:`; and a decisive experiment executable by one researcher on 1×H100 within the lifecycle. This channel does not waive any existing gate: a direct hit, CRITICAL finding, at least two MAJOR findings, weak prior-work research, or missing experiment retains its normal consequence. It replaces only the novelty penalty for an aggressive, unverified mechanism. Missing fields or `contradicts`/`unreachable` crack evidence returns rhetorical compliance to the ordinary standard.
- Research has no time-pressure allowance. Do not compress literature research or comparisons to finish sooner. Difference claims require reading the abstracts and methods of close work, not titles or search snippets.
- Before grading any prospective Strong Accept, conduct an unusually careful directed search with multiple query families covering problem wording, mechanism, and adjacent domains; include web search, arXiv, and non-paper occupation such as industry tools and blogs. Read the 5–8 closest works and explain every difference. If similar work exists, the remaining difference must independently support clear accept or the idea is reduced.

## Retention Rules

- The report body contains only Strong Accept ideas. Retry succeeds only with at least one Strong Accept.
- Include at most two borderline Accept with Revisions ideas in an appendix labeled `For reference only`; they do not satisfy the target.
