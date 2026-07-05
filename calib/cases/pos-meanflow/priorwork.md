## I1
检索词:问题表述(one-step visuomotor policy / fast action generation inference latency);方法机制(mean velocity field policy / flow matching single step action);相邻领域(consistency distillation image generation / one-step generative model)
API 检索:http://export.arxiv.org/api/query?search_query=ti:%22Consistency+Policy%22+OR+ti:%22One-Step+Diffusion+Policy%22+OR+ti:%22Mean+Flows%22&max_results=20
最近工作:
- Diffusion Policy: Visuomotor Policy Learning via Action Diffusion | https://arxiv.org/abs/2303.04137 | 确立扩散式动作生成范式 | 多步采样的源头基线,未涉一步化
- π0: A Vision-Language-Action Flow Model for General Robot Control | https://arxiv.org/abs/2410.24164 | flow-matching VLA | 同用流生成动作,推理仍多步
- Consistency Policy: Accelerated Visuomotor Policies via Consistency Distillation | https://arxiv.org/abs/2405.07503 | 蒸馏得一步视觉运动策略 | 目标相同(一步),机制为两阶段蒸馏
- One-Step Diffusion Policy: Fast Visuomotor Policies via Diffusion Distillation | https://arxiv.org/abs/2410.21257 | 分布匹配蒸馏一步化 | 同上,蒸馏路线
- Boosting Continuous Control with Consistency Policy | https://arxiv.org/abs/2310.06343 | RL 连续控制的 consistency 一步化 | 蒸馏/自举路线在 RL 侧的版本
- Mean Flows for One-step Generative Modeling | https://arxiv.org/abs/2505.13447 | 图像生成的平均速度场,单阶段一步 | 机制源头,未涉动作生成与机器人
- BiKC: Keypose-Conditioned Consistency Policy for Bimanual Robotic Manipulation | https://arxiv.org/abs/2406.10093 | 双臂操作的 consistency 变体 | 蒸馏路线的域内变体
- Learning Long-Context Diffusion Policies via Past-Token Prediction | https://arxiv.org/abs/2505.09561 | diffusion policy 训练改进 | 相邻改进方向,不涉一步化
最强反例:Consistency Policy(2405.07503)—— 它做到"一步视觉运动策略",但依赖教师+蒸馏两阶段;本 idea 头条(单阶段免蒸馏的平均速度场动作头)未被覆盖,训练范式与理论对象的差异足以支撑 clear-accept。
重叠判定:low —— 一步化赛道拥挤但全走蒸馏,单阶段平均速度场在动作域零命中。
实读篇数:8
编号自查:是(全部经 arXiv API 实际命中并打开核对标题)
