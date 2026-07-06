## I1
检索词:问题表述(regression head vs generative action head imitation learning / deterministic policy multimodal demonstrations);方法机制(diffusion policy ablation action head / L2 behavior cloning baseline);相邻领域(energy-based policy / action chunking transformer / one-step policy distillation)
API 检索:http://export.arxiv.org/api/query?search_query=ti:%22Diffusion+Policy%22+OR+ti:%22Behavior+Transformer%22+OR+ti:%22Implicit+Behavioral+Cloning%22&max_results=20
最近工作:
- Diffusion Policy: Visuomotor Policy Learning via Action Diffusion | https://arxiv.org/abs/2303.04137 | 系统对比扩散头与 LSTM-GMM、IBC、BC-RNN 等非扩散头 | 直接覆盖"回归/非生成式头是否足够"的头条问题,结论相反:多模态任务上非生成式头显著劣化
- What Matters in Learning from Offline Human Demonstrations | https://arxiv.org/abs/2108.03298 | robomimic 系统评测 BC/BC-RNN/GMM 头 | 已系统测过确定性与弱生成式头的边界,多模态人类数据上确定性头掉点
- Implicit Behavioral Cloning | https://arxiv.org/abs/2109.00137 | 证明显式回归头在多值映射上系统性失败,EBM 更优 | 头条主张的直接反例
- Behavior Transformers: Cloning k modes with one stone | https://arxiv.org/abs/2206.11251 | 面向多模态演示的离散+偏移动作头 | 动作多模态需专门建模的又一证据
- Learning Fine-Grained Bimanual Manipulation with Low-Cost Hardware (ACT) | https://arxiv.org/abs/2304.13705 | 动作分块 + CVAE 头 | 保留生成式成分(CVAE),未支持纯回归
- Consistency Policy | https://arxiv.org/abs/2405.07503 | 蒸馏一步化解决采样延迟 | 延迟问题的既有解法:删采样步数而非删生成式建模
- One-Step Diffusion Policy | https://arxiv.org/abs/2410.21257 | 分布匹配蒸馏一步生成 | 同上,forcing constraint 所指的延迟已有专门赛道在解
- π0: A Vision-Language-Action Flow Model | https://arxiv.org/abs/2410.24164 | flow-matching 动作头的旗舰 VLA | 方法核心即生成式头,无"贡献可忽略"的消融
最强反例:Diffusion Policy(2303.04137)—— 其对照实验已系统对比回归/GMM/EBM 头与扩散头,多模态任务上非生成式头显著劣化;"回归头足够"的头条主张被该篇基线表直接评测过且结论相反,差异不足以支撑 clear-accept(不够)。
重叠判定:high —— 头条问题("非生成式头是否足够")已被 2303.04137 与 2108.03298 的系统评测直接覆盖,且结论与主张相反。
实读篇数:8
编号自查:是(全部经 arXiv API 实际命中并打开核对标题)
裂缝证据核验(仅删承重假设形态,逐条覆盖该 idea 自报的 URL):
- https://arxiv.org/abs/2303.04137 | 核验:不符 —— 论文无"L2 头与扩散头持平"的消融;结果表显示扩散头在多模态任务上大幅优于 LSTM-GMM/IBC 等非扩散头
- https://arxiv.org/abs/2410.24164 | 核验:不符 —— π0 未报告"flow 头贡献可忽略"的消融;flow-matching 动作头是其方法核心
