## I1
一句话故事:把「off-policy 补样本效率靠加大 UTD」整个反过来——每 1024 条新转移只做 2 步梯度,用大模型、大 batch、高吞吐补回收敛,配 weight/feature/gradient 三重范数约束压 bootstrap 误差累积,快与稳/可扩第一次同时拿到
主题:效率与系统
形态:纯新机制
简述:大规模并行仿真下机器人 RL 的事实标准仍是 PPO:on-policy 稳,但窄分布数据在高维状态-动作空间估不准值函数。off-policy 能吃宽分布数据,却两头受制:快的一支靠 ~0.2M 小网络换墙钟速度,封死渐近性能;稳/可扩的一支靠正则化扩模型,但依赖高梯度步数(高 replay ratio),墙钟慢。低 UTD 的可行性先声已有:CrossQ 证明 UTD=1 配 BatchNorm 即达 REDQ 级样本效率,但止于小网络与 DMC 级任务,其 scaling 后续又转头加大 UTD。赌注:照监督学习的 scaling 直觉,把低 UTD 推到极端(每 1024 条新转移 2 步梯度)并同时扩模型、大 batch、高吞吐,off-policy 即可在墙钟与渐近两端同时胜出,规模化下的稳定性由联合约束 weight/feature/gradient 范数保证(单一权重归一不够,须同时压特征与梯度)。若成立,高维任务(灵巧操作、人形)的 RL 训练从小时级进分钟级,off-policy 成为 sim-to-real 的可默认选项。
最小否证实验:1×H100;IsaacLab/MJX 并行仿真取 2-3 个高维任务(人形行走 + 灵巧手);SAC 骨干,处理组 = UTD 2/1024 + 模型×4 + 大 batch + 三重范数约束;对照 (a) FastTD3/FastSAC 原配方(小模型) (b) SimbaV2(高 UTD)同墙钟预算。判定:低 UTD 组须在同墙钟下最终回报不劣于两对照且训练不发散;若极低 UTD 下训练系统性发散或显著劣于高 UTD 对照(off-policy 收敛确实依赖密集更新),赌注死。数天。
为何可能新:UTD 文献主线在加大或稳住高 UTD(高 replay ratio 正则化、reset、模型增广);唯一反向先例 CrossQ 止于 UTD=1、小网络、无并行吞吐、无 sim-to-real,其 scaling 后续(batch+weight norm)重新转向加大 UTD,把 sub-1 极端角留空;「sub-1 极端 UTD × 模型/吞吐规模化 × 机器人 sim-to-real」的联合零检出;快轴先例未扩模型、扩模型先例未降 UTD。这是待验证假设。
