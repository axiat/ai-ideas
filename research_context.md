# Research Context

Last updated: 2026-09-04. This file is optional inspiration, not a constraint. `brainstorming_policy.md` defines divergence: at most 1–2 ideas per round may directly concern the stack described here; the rest must range beyond it.

## Available Platform

- Humanoid robots in the group: EngineAI PM01-EDU and T800, with an existing layered controller. Locomotion and whole-body control are available as execution infrastructure, not research targets by default.
- VLA foundation model available locally: GR00T N1.6 (~3B). Backbone freezing is a design choice, not a constraint — LoRA/adapters and full fine-tuning are both in scope on the budget below; from-scratch pretraining is not.

## Resource Assumptions

One researcher, 1×H100 80G, direct access to the humanoid platform for real-robot experiments. The feasibility baseline in `brainstorming_policy.md` still applies to every candidate's minimal falsification experiment.

## Inspiration Sparks

Starting points only — candidates may depart freely, and most of the batch should range beyond these:

- Execution failure detection and recovery for VLA on humanoids: monitor visual/proprioceptive mismatch during long-horizon execution and trigger retry, re-grasp, or replan; compare external recovery modules against recovery-internalized fine-tuning.
- Proprioception/force injection into a vision-dominant VLA for contact-rich manipulation (insertion, door opening, carrying), measuring the marginal gain curve of the new modality.
- Loco-manipulation and whole-body VLA: operating while the base moves, coordinating a frozen locomotion controller with the manipulation policy, or unifying navigation and manipulation in one action space.
- Sample-efficient real-robot post-training: residual RL, offline-to-online fine-tuning, or human preference feedback on the real platform.
- Continual learning for deployed VLA: acquiring new tasks without catastrophic forgetting of pretrained skills.

## Interest Keywords

Humanoid loco-manipulation, VLA post-training (RL, residual, preference-based), multimodal state injection (proprioception, force, tactile), failure detection and recovery, continual learning, sim-to-real data efficiency, long-horizon mobile manipulation.
