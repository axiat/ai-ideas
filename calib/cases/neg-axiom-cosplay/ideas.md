## I1
One-Sentence Story: Remove the generative action head from imitation-learning policies: claim that action distributions need no generative model, direct L2 regression is sufficient, and iterative sampling is historical baggage.
Theme: VLA - Architecture
Form: remove-load-bearing-assumption
Summary: Diffusion and flow action heads require 10-100 sampling steps, increasing inference latency and implementation complexity. The wager is that generative modeling is not load-bearing: large-scale pretrained visual representations already make observations discriminative enough, while data cleaning and task decomposition can remove residual action multimodality. If true, mainstream VLA sampling stacks can be removed wholesale.
Assumption to Remove: Action distributions require a generative model with iterative denoising or flow integration. Diffusion Policy, π0, RDT, and other mainstream methods assume this component.
Why It Can Be Removed Now: Pretrained visual representations are substantially more discriminative; combined with data cleaning and task decomposition, they may reduce residual action multimodality enough for deterministic regression.
Forcing Constraint: Edge deployment. Iterative sampling does not meet high-frequency real-time control budgets; a 10ms control cycle cannot accommodate 10-100 denoising steps.
Crack Evidence: https://arxiv.org/abs/2303.04137 — The Diffusion Policy appendix reportedly shows an L2 regression head matching the diffusion head on robomimic, with diffusion helping only on long-horizon real-robot tasks.
Crack Evidence: https://arxiv.org/abs/2410.24164 — π0 reportedly shows that the flow head contributes little to success and that most gains come from the pretrained VLM backbone.
Minimal Falsification Experiment: On robomimic Lift and Can, compare an L2 regression head with Diffusion Policy, the strongest baseline, under the same visual backbone using 1×H100 for one day. If the regression head trails the diffusion head by no more than 5 points, the generative head is not load-bearing.
Why It May Be Novel: No work has yet been shown to make and test the systematic claim that generative action heads can be removed wholesale. This remains a hypothesis for independent verification.
