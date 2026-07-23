# Research Context

Last updated: 2026-07-03. This file is optional inspiration, not a constraint. `brainstorming_policy.md` defines divergence: at most 1–2 ideas per round may directly concern this stack; the rest must range beyond it.

## Core Direction

DSRL-style post-training or steering for a frozen VLA: freeze the π0.5 backbone and use SAC-family RL in latent-noise or action space for control. The focus is inference-time control and frozen-backbone modulation, not full fine-tuning.

## Current Stack and Workstreams

- Base model: π0.5, a VLA with a flow-matching action head; `openpi` + `jaxrl2`; simulated manipulation tasks.
- Reward design: VLM-based reward shaping with subgoal-completion rewards, ProcVLM-style procedure-grounded progress rewards, and VLLR-style generalizable dense rewards plus policy self-certainty intrinsic rewards.
- Validated uses of VLM signals: reward shaping, state augmentation for the SAC critic, and static task decomposition at episode granularity.

## Validated Findings

- π0.5 responds unreliably to subgoal-level text prompts. V1 and V1-extra experiments showed low SNR and closed-loop counterexamples, so the path that feeds VLM-generated subgoal text into the VLA was dropped. The VLA always receives the full task text.
- The VLM judge retains perception failures such as simulated visual penetration artifacts. Reward signals require auditing, multi-frame evidence, and temporal validation.

## Interest Keywords

World models (latent dynamics, interactive world models, model-based RL for robotics), VLA post-training (RL post-training, noise-space steering, adaptive chunk replanning), inference dynamics, frozen-backbone modulation, VLM-as-judge rewards, and long-horizon manipulation.

## Resource Assumptions

One researcher, approximately 40 effective hours per week, H100-class multi-GPU access, strong engineering ability, and an applied research preference.
