# Review criteria and interpretation

ICLR asks reviewers to assess the stated objective, support for the claims,
and the new knowledge or value contributed. It explicitly permits valuable
contributions without state-of-the-art performance. Additional experiments
should validate the existing contribution within a limited scope. Reviewers
should identify the reasons that determine the recommendation and distinguish
other suggestions. These principles support claim-specific validation,
residual-contribution analysis and bounded revision conditions in this change.
[ICLR 2026 Reviewer Guide](https://iclr.cc/Conferences/2026/ReviewerGuide)

NeurIPS distinguishes general, theoretical, use-inspired, conceptual and negative
contributions. New combinations, evaluation insights and theoretical results can
be original; theory need not include experiments. Negative results require
substantive, surprising knowledge, while a mitigation method can remain future
work. These criteria support evaluating a diagnostic contribution on the knowledge
it adds and retaining a substantive value requirement.
[NeurIPS 2026 Reviewing Guidelines](https://neurips.cc/Conferences/2026/ReviewerGuidelines)

This harness evaluates research proposals. Its permission to use a checkable
argument and an executable falsification plan before running experiments is an
idea-stage adaptation. Neither guide establishes that an unsupported prediction
is evidence or that an unfinished empirical paper should be accepted.

The implementation retains the repository's minimum-vote aggregation, SA gates
and automatic retry budget. ICLR describes reviewer discussion, revisions and
recommendations to area chairs, so this implementation does not reproduce the
complete conference decision process. Its SA label is an internal prioritization
judgment and does not estimate TPAMI or conference acceptance probability.
[ICLR 2026 Reviewer Guide](https://iclr.cc/Conferences/2026/ReviewerGuide)

The A/B study compares two complete instruction and output-format packages on
the same supplied packets. The old arm already contains the earlier clarification
about unrun experiments. The six real packets and four stipulated synthetic
packets provide a small challenge set; same-model seats are repeated judgments,
and their verdict frequencies are not human-review error rates. Input defects
identified independently must remain part of the interpretation.
