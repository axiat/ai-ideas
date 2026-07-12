# 查重快照时点:2026-01 底(ICML 2026 投稿前);所有记录以该时点可见文献为准

## I1
检索词:问题表述(memory benchmark robot manipulation / partially observable manipulation benchmark / history-dependent VLA evaluation);方法机制(memory-augmented VLA episodic memory / recurrent policy occlusion counting / unified backbone memory variant comparison);相邻领域(POMDP memory RL benchmark / memory gym endless tasks / lifelong robot learning)
API 检索:http://export.arxiv.org/api/query?search_query=all:%22RoboMME%22&max_results=10(无同名占位);http://export.arxiv.org/api/query?id_list=2502.10550,2501.18564,2511.11478,2508.19236,2510.20328,2309.17207,2303.01859,2511.09516
web 检索:memory benchmark robot manipulation POMDP 2025 → MIKASA-Robo/MemoryBench/LIBERO-Mem/MEMBOT;memory-augmented vision-language-action model 2025 → MemoryVLA/MemER/MAP-VLA/EchoVLA(均方法论文);MIKASA-Robo GitHub → 仓库与任务清单实读
工业界/非论文占位:HuggingFace hub 检索 "robot memory benchmark"、"MIKASA memory"(datasets/models/spaces)→ 未检出;ManiSkill3/RoboCasa 官方任务集无记忆类目(任务短时程或当前帧可解);唯一开源占位为 GitHub CognitiveAISystems/MIKASA-Robo(ManiSkill3 生态第三方)
最近工作:
- MIKASA-Robo: Memory, Benchmark & Robots | https://arxiv.org/abs/2502.10550 | 2025-02(v2 2025-06)| ManiSkill3 上 32 个隔离记忆的桌面操作任务,四类记忆分类学(object/spatial/sequential/capacity),主评 RL 基线(PPO-MLP vs LSTM、SAC、TD-MPC2、DT 等),v2 §6.4 将 Octo/OpenVLA 作现成基线在选定任务上测 | 与本 idea 的重叠:头条前半(隔离记忆类型的操作任务族+分类学)被实质覆盖,类目 3/4 语义重合(procedural 为本 idea 独有);未做"同一 VLA 骨干 × 多记忆表征"矩阵;任务短时程、缺高质量演示,面向 RL 而非模仿/VLA
- SAM2Act(+MemoryBench) | https://arxiv.org/abs/2501.18564 | 2025-01 | 记忆架构操作策略,附带 MemoryBench 评测集 | 仅 3 个空间记忆任务、无分类学,单一记忆类型占位
- LIBERO-Mem(+Embodied-SlotSSM) | https://arxiv.org/abs/2511.11478 | 2025-11(AAAI 2026)| 面向 VLA 的物体级非马尔可夫任务套件 + 单一 slot-SSM 方法 | VLA 记忆基准雏形:无四类分类学、无变体矩阵,单方法配套评测
- MemoryVLA | https://arxiv.org/abs/2508.19236 | 2025-08 | 感知-认知双记忆 VLA 方法,评 SimplerEnv/LIBERO/MIKASA-Robo/真机 | 方法论文,自选任务无横评;其采用 MIKASA-Robo 佐证后者已是记忆评测的事实参照
- MemER | https://arxiv.org/abs/2510.20328 | 2025-10(ICLR 2026)| 关键帧经验检索分层记忆,3 个自设计真机长时程任务 | 方法论文,自选任务
- MAP-VLA | https://arxiv.org/abs/2511.09516 | 2025-11 | 记忆增强软提示 + 轨迹检索,冻结 VLA,LIBERO+真机 | 方法论文,无标准化评测
- Memory Gym | https://arxiv.org/abs/2309.17207 | 2023-09 | 2D 网格 POMDP 无尽任务测 RL agent 记忆 | 概念前驱:隔离记忆能力;非操作域、非 VLA
- POPGym | https://arxiv.org/abs/2303.01859 | 2023-03 | 15 个部分可观测 RL 环境 + 13 种记忆基线横评 | "统一横评记忆架构"形态的 RL 域先例,证明该形态可独立成文;非操作、非 VLA
最强反例:MIKASA-Robo(2502.10550)—— 11 个月前已做出"隔离记忆类型的操作任务族+四类分类学",与本 idea 头条前半结构同构;未覆盖的维度是"长时程+高质量演示可模仿的任务设计"与"统一 VLA 骨干上 10+ 记忆增强变体的系统横评"(其主体为 RL 基线,v2 仅把 2 个现成 VLA 当基线测)。差异集中在评测对象(VLA/模仿 vs RL)与横评矩阵(变体 × 记忆类型),该空缺维度即本 idea 主体贡献;分类学与任务族形态已被占,差异是否足以支撑 clear-accept 取决于对空缺维度的定价。
重叠判定:medium —— 分类学+隔离任务族已被 MIKASA-Robo 占据(非单篇覆盖头条全部,但结构同构度高);"统一 VLA 骨干变体矩阵+长时程演示评测"在本时点无人做,方法侧(MemoryVLA/MemER/MAP-VLA)全部单方法+自选任务。
实读篇数:8(MIKASA-Robo 实读 v2 全文含 §6.4 VLA 基线表;其余实读摘要与方法节)
编号自查:是(全部经 arXiv API 实际命中并打开核对标题)
