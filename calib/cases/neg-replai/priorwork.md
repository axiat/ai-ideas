## I1
Search Terms: problem wording (audio contact moment labeling manipulation pretraining); mechanism (audible interaction representation learning egocentric); adjacent domain (audio-visual self-supervised learning)
- Query: http://export.arxiv.org/api/query?id_list=2209.13583,2110.07058&max_results=5
Nearest Work:
- Learning State-Aware Visual Representations from Audible Interactions (RepLAI) | https://arxiv.org/abs/2209.13583 | Uses audio transients in Ego4D and EPIC to locate interaction moments, anchors video-representation pretraining on them, and evaluates manipulation-related downstream tasks | The headline mechanism is identical: audio supplies free interaction/contact timestamps that supervise video representations.
- Ego4D: Around the World in 3,000 Hours of Egocentric Video | https://arxiv.org/abs/2110.07058 | Supplies synchronized audio and the same data foundation | Data source only; no method occupation.
- The Sound of Pixels | https://arxiv.org/abs/1804.03160 | Classic audio-visual self-supervised alignment | Conceptual predecessor for using audio as free supervision for vision.
Strongest Counterexample: RepLAI (2209.13583) — It already implements audio-transient interaction localization and uses it to pretrain video representations on the same data sources. The remaining distinction is only replacing the downstream task with manipulation BC, below clear-accept scale.
Overlap: high — RepLAI directly covers the headline mechanism.
Papers Read: 3
arXiv ID Check: yes — Every ID resolved through the arXiv API and its title was opened and checked.
