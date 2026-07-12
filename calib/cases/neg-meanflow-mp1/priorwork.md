# 查重快照时点:2025-09-24(ICLR 2026 投稿前);所有记录以该时点可见文献为准

## I1
检索词:问题表述(one-step visuomotor policy / fast action generation inference latency / distillation-free one-step policy);方法机制(mean velocity field policy / MeanFlow action generation robot / flow matching single step action);相邻领域(consistency flow matching manipulation / shortcut model one-step generation / real-time diffusion policy deployment)
API 检索:http://export.arxiv.org/api/query?search_query=all:%22mean+flow%22+AND+all:%22policy%22&max_results=20;http://export.arxiv.org/api/query?id_list=2507.10543,2412.04987,2402.04292,2405.07503,2410.21257,2505.13447,2410.12557,2508.06269
web 检索:MP1 MeanFlow one-step robot manipulation → arXiv 2507.10543 + GitHub LogSSim/MP1;FlowPolicy consistency flow matching AAAI 2025 → arXiv 2412.04987(AAAI 2025 oral,proceedings 33617);one-step policy without distillation 2024 2025 → AdaFlow/Shortcut Models/Streaming Diffusion Policy
工业界/非论文占位:Physical Intelligence 博客 Real-Time Chunking(pi.website/research/real_time_chunking,2025-06,arXiv 2506.07339)以推理时调度做 flow/VLA 实时化(路线不同,记录在案);HuggingFace LeRobot 文档实机建议 DDIM 10-25 步或 consistency 蒸馏 1-3 步;专门"一步免蒸馏"工业博客未检出
最近工作:
- MP1: MeanFlow Tames Policy Learning in 1-step for Robotic Manipulation | https://arxiv.org/abs/2507.10543 | 2025-07-14,带代码(github.com/LogSSim/MP1)| MeanFlow Identity 直接学区间平均速度,3D 点云输入 1-NFE 出动作轨迹,免蒸馏免 consistency 约束,另加 Dispersive Loss;自报胜 DP3 与 FlowPolicy,6.8ms 推理 | 与本 idea 的重叠:头条机制(MeanFlow 平均速度场迁到机器人动作生成、单阶段免蒸馏一步)被单篇完整覆盖,早于本时点 72 天
- FlowPolicy: Enabling Fast and Robust 3D Flow-based Policy via Consistency Flow Matching | https://arxiv.org/abs/2412.04987 | 2024-12(AAAI 2025 oral)| consistency flow matching 从零训自洽速度场,单阶段免蒸馏一步 3D 策略;原文明确"without the aid of distillation" | 直接证伪"现有一步化路线全部依赖蒸馏"
- AdaFlow | https://arxiv.org/abs/2402.04292 | 2024-02 | 方差自适应 ODE 步长,单峰状态自动退化为一步生成,免蒸馏 | 更早的免蒸馏一步先例
- Consistency Policy | https://arxiv.org/abs/2405.07503 | 2024-05 | 教师扩散策略 consistency 蒸馏得 1-3 步 | 蒸馏路线代表,一步赛道占位
- One-Step Diffusion Policy(OneDP) | https://arxiv.org/abs/2410.21257 | 2024-10 | 分布匹配蒸馏一步化 visuomotor | 蒸馏路线
- One Step Diffusion via Shortcut Models | https://arxiv.org/abs/2410.12557 | 2024-10 | 步长条件化学"跳跃"(区间平均方向),单阶段免蒸馏一步(图像域) | 平均速度类机制的图像域近亲
- Mean Flows for One-step Generative Modeling | https://arxiv.org/abs/2505.13447 | 2025-05 | 平均速度场恒等式一步生成(图像) | 机制源头
- OM2P: Offline Multi-Agent Mean-Flow Policy | https://arxiv.org/abs/2508.06269 | 2025-08 | mean-flow 一步动作生成 + Q 监督,离线多智能体 RL | 平均速度场动作生成的又一域内占位
最强反例:MP1(2507.10543)—— 头条"平均速度场从图像生成迁到机器人动作生成、单阶段免蒸馏一步出动作"被其完整做出并开源;本 idea 剩余增量仅"瞬时速度约束"一个附加正则项与评测域选择。差异是否足以支撑 clear-accept:不够。
重叠判定:high —— 头条机制被 MP1 单篇直接占据;且"为何可能新"的两条依据(平均速度场未迁移到动作生成 / 一步策略全靠蒸馏)分别被 MP1 与 FlowPolicy/AdaFlow 证伪。
实读篇数:8(MP1 与 FlowPolicy 实读全文方法节,其余实读摘要与方法)
编号自查:是(全部经 arXiv API 实际命中并打开核对标题)
