## I1
检索词:问题表述(memory benchmark robot manipulation / long-horizon history-dependent policy evaluation);方法机制(memory-augmented VLA / recurrent policy occlusion counting);相邻领域(POMDP memory RL benchmark / lifelong robot learning)
API 检索:http://export.arxiv.org/api/query?search_query=ti:%22MemoryVLA%22+OR+ti:%22Memory+Gym%22+OR+ti:%22Past-Token+Prediction%22&max_results=20
最近工作:
- LIBERO: Benchmarking Knowledge Transfer for Lifelong Robot Learning | https://arxiv.org/abs/2306.03310 | 终身学习基准,测知识迁移 | 同为操作基准,但不隔离、不度量记忆;任务本身近 Markov
- CALVIN: A Benchmark for Language-Conditioned Policy Learning for Long-Horizon Robot Manipulation Tasks | https://arxiv.org/abs/2112.03227 | 长时程语言条件操作基准 | 任务链长但各步可从当前帧恢复,不构成记忆测试
- RoboCasa: Large-Scale Simulation of Everyday Tasks for Generalist Robots | https://arxiv.org/abs/2406.02523 | 大规模日常任务仿真 | 测广度与泛化,不测历史依赖
- Evaluating Real-World Robot Manipulation Policies in Simulation | https://arxiv.org/abs/2405.05941 | real-to-sim 评测协议 | 评测方法学近邻,与记忆维度无关
- Memory Gym: Towards Endless Tasks to Benchmark Memory Capabilities of Agents | https://arxiv.org/abs/2309.17207 | RL 记忆基准(网格/像素游戏) | 概念前驱:隔离记忆能力;但非操作域、非 VLA,任务形态完全不同
- Recurrent Model-Free RL Can Be a Strong Baseline for Many POMDPs | https://arxiv.org/abs/2110.05038 | POMDP 下记忆架构横评 | 横评思路的 RL 前驱,不涉操作任务与 VLA 骨干
- MemoryVLA: Perceptual-Cognitive Memory in Vision-Language-Action Models for Robotic Manipulation | https://arxiv.org/abs/2508.19236 | 记忆增强 VLA 方法论文 | 证明记忆模块有收益,但单一方法+自选任务,无标准化基准
- Learning Long-Context Diffusion Policies via Past-Token Prediction | https://arxiv.org/abs/2505.09561 | 长上下文 diffusion policy 方法 | 指出历史利用难点,方法论文,无记忆分类评测
最强反例:MemoryVLA(2508.19236)—— 它做到"给 VLA 加记忆模块并在自选任务显示收益";本 idea 头条(四类记忆隔离的标准化基准 + 统一骨干上的变体矩阵横评)未被它或任何近邻覆盖,差异足以支撑 clear-accept。
重叠判定:low —— 记忆增强方法密集,但"隔离记忆类型的操作评测基准"未被占据。
实读篇数:8
编号自查:是(全部经 arXiv API 实际命中并打开核对标题)
