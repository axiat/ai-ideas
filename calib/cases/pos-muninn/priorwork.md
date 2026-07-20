# 查重快照时点:2026-01 底(RSS 2026 论文截止 01-30 前);所有记录以该时点可见文献为准

## I1
检索词:问题表述(diffusion policy real-time inference acceleration / trajectory diffusion planner speedup / caching diffusion robot control);方法机制(training-free caching denoiser output reuse / conformal prediction diffusion inference / sampler error propagation cache);相邻领域(consistency distillation visuomotor policy / one-step action generation / early-exit denoising)
API 检索:https://export.arxiv.org/api/query?search_query=(abs:"caching" OR abs:"cache") AND (abs:"diffusion policy" OR abs:"trajectory diffusion" OR (abs:"diffusion" AND cat:cs.RO)) AND submittedDate:[202001010000 TO 202601302359](8 篇逐条核对:机器人域缓存/复用加速已有 BAC 与 Sparse ActionGen,均启发式;无一给出误差传播模型或覆盖保证);https://export.arxiv.org/api/query?id_list=2312.00858,2411.19108,2506.13456,2601.12894,2405.07503,2406.01586,2205.09991,2303.04137
web 检索:diffusion policy cache acceleration github → BAC 项目页、Sparse ActionGen(OpenReview)、RTI-DP(初始化路线,非缓存)等,均无保证条款;diffusion caching guarantee conformal → 未检出;缓存 扩散策略 误差界(中文)→ 未检出
工业界/非论文占位:diffusion policy 部署加速的工程路线集中在蒸馏重训与去噪步数削减(3D-Diffusion-Policy/iDP3 等仓库);showlab/Awesome-Robotics-Diffusion 清单内加速条目均蒸馏或启发式,带保证的缓存插件未检出
最近工作:
- DeepCache | https://arxiv.org/abs/2312.00858 | 2023-12 | 视觉扩散 training-free 特征缓存,利用去噪时序冗余 | 感知/像素 metric,无轨迹语义;误差不做传播分析,无保证
- TeaCache | https://arxiv.org/abs/2411.19108 | 2024-11 | 视频扩散按 timestep embedding 估计输出波动的自适应缓存 | 自适应但启发式;误差度量在视觉空间;无覆盖保证
- Block-wise Adaptive Caching(BAC)| https://arxiv.org/abs/2506.13456 | 2025-06 | diffusion policy 的 training-free 块级缓存,调度器按特征相似度选更新步,推理 8→45Hz | 机器人域缓存最近占位:自称 lossless 但无定义、无误差传播模型、无任何保证;头条(可证偏差预算)未触
- Sparse ActionGen | https://arxiv.org/abs/2601.12894 | 2026-01 | rollout 自适应剪枝+跨步跨块激活复用,至 4× | 同轴占位;无保证;含环境感知剪枝器,决策空间与缓存不同
- Consistency Policy | https://arxiv.org/abs/2405.07503 | 2024-05 | 一致性蒸馏出低延迟视觉运动策略 | 蒸馏系:须重训,加速与保真耦合在训练里;无推理期保证
- ManiCM | https://arxiv.org/abs/2406.01586 | 2024-06 | 3D diffusion policy 一致性蒸馏一步推理 | 同上蒸馏系;点云条件动作空间
- Diffuser(Planning with Diffusion)| https://arxiv.org/abs/2205.09991 | 2022-05 | 轨迹扩散 planner 奠基 | 被加速对象,非竞争
- Diffusion Policy | https://arxiv.org/abs/2303.04137 | 2023-03 | 动作扩散视觉运动策略奠基 | 被加速对象,非竞争
最强反例:BAC(2506.13456)——同域、同 training-free、同缓存机制,已给 3× 加速与「lossless」经验主张。差异:其 lossless 是经验描述,无偏差定义、无误差传播模型、无覆盖保证、不评闭环安全后果;头条(采样器敏感系数传播 + conformal 有限样本保证 + 预算化决策)零占据。增量是否足以 clear-accept 由「保证本身 + 同加速下不劣」承担。
重叠判定:medium —— 「机器人域 diffusion 缓存加速」已被 BAC/Sparse ActionGen 多点占据(诚实同构);「缓存决策的分布无关偏差保证与误差传播模型」在视觉域与机器人域均零检出(API 时窗 8 篇 + web 逐条核对)。头条立在零占据的保证轴上,基础机制轴如实标已占。
实读篇数:8(2506.13456 与 2601.12894 实读机制与评估细节;其余实读摘要)
编号自查:是(全部经 arXiv API 实际命中并核对标题与日期)
