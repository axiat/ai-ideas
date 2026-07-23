## I1
Search Terms: problem wording (regression head vs generative action head imitation learning / deterministic policy multimodal demonstrations); mechanism (diffusion policy ablation action head / L2 behavior cloning baseline); adjacent domain (energy-based policy / action chunking transformer / one-step policy distillation)
- Query: http://export.arxiv.org/api/query?search_query=ti:%22Diffusion+Policy%22+OR+ti:%22Behavior+Transformer%22+OR+ti:%22Implicit+Behavioral+Cloning%22&max_results=20
Nearest Work:
- Diffusion Policy: Visuomotor Policy Learning via Action Diffusion | https://arxiv.org/abs/2303.04137 | Systematically compares diffusion against LSTM-GMM, IBC, BC-RNN, and other non-diffusion heads | Directly tests whether regression or nongenerative heads are sufficient and finds that they degrade substantially on multimodal tasks.
- What Matters in Learning from Offline Human Demonstrations | https://arxiv.org/abs/2108.03298 | Systematic robomimic evaluation of BC, BC-RNN, and GMM heads | Measures deterministic and weakly generative heads on human multimodal data and finds deterministic heads lose performance.
- Implicit Behavioral Cloning | https://arxiv.org/abs/2109.00137 | Shows explicit regression fails systematically on multivalued mappings and that EBM performs better | Direct counterexample to the headline claim.
- Behavior Transformers: Cloning k modes with one stone | https://arxiv.org/abs/2206.11251 | Discrete-plus-offset action head for multimodal demonstrations | Further evidence that action multimodality needs explicit modeling.
- Learning Fine-Grained Bimanual Manipulation with Low-Cost Hardware (ACT) | https://arxiv.org/abs/2304.13705 | Action chunking with a CVAE head | Retains a generative component and does not support pure regression.
- Consistency Policy | https://arxiv.org/abs/2405.07503 | Uses distillation for one-step inference | Existing answer to latency: remove sampling steps, not generative modeling.
- One-Step Diffusion Policy | https://arxiv.org/abs/2410.21257 | One-step generation through distribution-matching distillation | Another established route for the same forcing constraint.
- π0: A Vision-Language-Action Flow Model | https://arxiv.org/abs/2410.24164 | Flagship VLA with a flow-matching action head | The generative head is central and no ablation shows its contribution is negligible.
Strongest Counterexample: Diffusion Policy (2303.04137) — Its controlled comparison already evaluates regression, GMM, EBM, and diffusion heads and finds nongenerative heads substantially worse on multimodal tasks. The headline claim was tested directly with the opposite result, leaving no clear-accept distinction.
Overlap: high — Diffusion Policy (2303.04137) and the robomimic study (2108.03298) directly cover whether nongenerative heads suffice and report evidence against the claim.
Papers Read: 8
arXiv ID Check: yes — Every ID resolved through the arXiv API and its title was opened and checked.
## Crack Evidence Verification
- https://arxiv.org/abs/2303.04137 | Verification: contradicts — The paper contains no ablation showing an L2 head matching diffusion; its results show large gains over LSTM-GMM, IBC, and other nongenerative heads on multimodal tasks.
- https://arxiv.org/abs/2410.24164 | Verification: contradicts — π0 contains no ablation showing a negligible flow-head contribution; flow matching is a core method component.
