# 查重快照时点:2026-01 底(RSS 2026 论文截止 01-30 前);所有记录以该时点可见文献为准

## I1
检索词:问题表述(off-policy RL massively parallel simulation robot / fast stable RL humanoid dexterous / PPO default sim-to-real);方法机制(update-to-data ratio scaling model size / norm bounded critic stability / large batch off-policy learning rate);相邻领域(replay ratio regularization reset / distributional critic reward scaling / parallel Q-learning)
API 检索:https://export.arxiv.org/api/query?search_query=abs:"off-policy" AND (abs:"update-to-data" OR abs:"replay ratio" OR abs:"UTD") AND submittedDate:[202001010000 TO 202601302359](8 篇逐条核对:全在加大或稳住高 UTD 方向——稳高更新比、大 batch 预训练、经验回放机理);https://export.arxiv.org/api/query?search_query=all:"CrossQ"(命中低 UTD 反向先例 1902.05605 及其 scaling 后续 2502.07523,均入最近工作;词面扫描不含 off-policy 字样故首查漏检,已补);https://export.arxiv.org/api/query?id_list=2505.22642,2512.01996,2405.16158,2410.09754,2502.15280,1902.05605,2502.07523,2307.12983,1707.06347
web 检索:off-policy massively parallel humanoid → FastTD3/FastSAC 一系与 Raffin 博客;low update-to-data ratio large model RL → 未检出;SAC 大规模并行 稳定(中文)→ 未检出
工业界/非论文占位:IsaacLab/rsl_rl 官方栈默认且仓库配置仅带 PPO,「PPO 是 sim-to-real 事实标准、常是框架唯一支持」有公开记录;Raffin 2025 博客(araffin.github.io/post/sac-massive-sim)系统记录 SAC 上大规模并行的失效点(动作分布与边界失配等)与调参修法——方向是调稳现有配方,非 UTD 反转+规模化
最近工作:
- FastTD3 | https://arxiv.org/abs/2505.22642 | 2025-05 | 并行仿真+大 batch+分布 critic+调参的 TD3,HumanoidBench 单 A100 3 小时内解题 | 快轴占位:~0.2M 小网络封渐近性能;未扩模型、未反转 UTD
- Learning Sim-to-Real Humanoid Locomotion in 15 Minutes | https://arxiv.org/abs/2512.01996 | 2025-12 | FastSAC/FastTD3 配方 15 分钟人形 sim-to-real,单 4090 | 快轴最近占位:同小模型路线,稳定靠调参与 minimalist reward;头条(降 UTD 扩模型统一配方)未触
- BRO | https://arxiv.org/abs/2405.16158 | 2024-05 | 强正则化扩 critic + 乐观探索,样本效率 SOTA | 稳/扩轴占位:高 replay ratio 计算换样本,墙钟慢;与本赌注方向相反
- SimBa | https://arxiv.org/abs/2410.09754 | 2024-10 | 简单性偏置架构(输入归一/残差/LayerNorm)扩参数 | 稳/扩轴:未动 UTD,未做大规模并行吞吐
- SimbaV2 | https://arxiv.org/abs/2502.15280 | 2025-02 | 超球归一约束 weight/feature 范数 + 分布价值估计 + 奖励缩放,SAC 基座扩模型 | 最近机制近邻:范数约束同源;但依赖较多梯度步、未上大并行吞吐;gradient 范数一环缺位
- Parallel Q-Learning | https://arxiv.org/abs/2307.12983 | 2023-07 | 大规模并行仿真下扩 off-policy Q-learning | 早期并行 off-policy 尝试,locomotion 上未匹配 PPO(工业博客有记录);无规模化稳定机制
- CrossQ | https://arxiv.org/abs/1902.05605 | ICLR 2024(v1 2019)| UTD=1 + BatchNorm 去 target 网络,以 REDQ/DroQ 几分之一计算达同级样本效率 | 低 UTD 反向先例的源头:直接先占「低 UTD 配归一化可收敛」命题;止于小网络、DMC 级任务,无并行吞吐、无机器人 sim-to-real
- Scaling Off-Policy RL with Batch and Weight Normalization(CrossQ 后续)| https://arxiv.org/abs/2502.07523 | 2025-02 | CrossQ 加 weight norm 探索更高 UTD 的 scaling | 方向重新转回加大 UTD,把 sub-1 极端角留空;仍无并行吞吐与机器人域
- PPO | https://arxiv.org/abs/1707.06347 | 2017-07 | on-policy 事实标准 | 被挑战的默认,非竞争
最强反例:机制轴 CrossQ(1902.05605)——「低 UTD + 归一化」命题的先占者,UTD=1 已证可行;差异:UTD=1 与 2/1024 差近三个数量级、小网络对扩模型、样本效率目标对墙钟+渐近双目标、无并行吞吐无 sim-to-real,且其 scaling 后续(2502.07523)转向加大 UTD。payoff 轴 FastSAC/FastTD3 一系(2512.01996 + 2505.22642)——已达 15 分钟人形 sim-to-real;差异:小网络封渐近与高维(灵巧操作),稳定靠调参与奖励设计非规模化范数机制。净增量记分须以两占据者为 baseline:只计 sub-1 极端 UTD 下扩模型的渐近解封、灵巧高维增益与联合范数机制的归因。
重叠判定:medium —— 目标轴(快速 off-policy 机器人 RL)与 payoff 轴(分钟级人形 sim-to-real)已被 FastTD3/FastSAC 一系占据;机制轴「低 UTD + 归一化」已被 CrossQ 占据(UTD=1)。零占据缩窄为「sub-1 极端 UTD × 模型/吞吐规模化 × 机器人 sim-to-real」的联合(API 时窗扫描 + CrossQ 线补检 + web 均核对)。头条立在该联合上,三条已占轴如实标注。
实读篇数:9(2505.22642、2512.01996、2502.15280、1902.05605 实读机制细节;其余实读摘要)
编号自查:是(全部经 arXiv API 实际命中并核对标题与日期)
