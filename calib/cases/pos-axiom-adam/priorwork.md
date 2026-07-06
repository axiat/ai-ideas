## I1
检索词:问题表述(optimizer choice RLHF RLVR post-training / SGD vs Adam reinforcement learning LLM);方法机制(memory-efficient optimizer large language model / adaptive learning rate momentum ablation);相邻领域(sparse parameter update fine-tuning / zeroth-order optimization LLM / RL loss landscape)
API 检索:http://export.arxiv.org/api/query?id_list=1705.08292,2302.06675,2402.16788,2406.16793,2403.03507,2305.17333,2306.09782,2402.03300
最近工作:
- The Marginal Value of Adaptive Gradient Methods in Machine Learning | https://arxiv.org/abs/1705.08292 | 系统对比自适应方法与 SGD 的泛化 | 质疑 Adam 必需性的经典源头,但限于监督学习与小规模,未触 transformer/RL
- Why Transformers Need Adam: A Hessian Perspective | https://arxiv.org/abs/2402.16788 | 用块异质 Hessian 解释 SGD 在 transformer 上失败 | 为该假设辩护的最强论证,但研究对象是 next-token 训练,未测 RL 阶段
- Symbolic Discovery of Optimization Algorithms (Lion) | https://arxiv.org/abs/2302.06675 | 搜索出的省显存优化器替代 Adam | 同瞄准优化器显存,但保留动量且面向预训练,未质疑自适应本身在 RL 的必要性
- Adam-mini: Use Fewer Learning Rates To Gain More | https://arxiv.org/abs/2406.16793 | 按 Hessian 块结构削减学习率数省显存 | 改良 Adam 而非删除,预训练/SFT 域
- GaLore: Memory-Efficient LLM Training by Gradient Low-Rank Projection | https://arxiv.org/abs/2403.03507 | 梯度低秩投影省优化器显存 | 同一 forcing constraint 的既有解法:压缩状态而非删掉自适应
- Fine-Tuning Language Models with Just Forward Passes (MeZO) | https://arxiv.org/abs/2305.17333 | 零阶优化即可微调 LLM | 后训练地形宽容的证据,限 SFT 域
- Full Parameter Fine-tuning for Large Language Models with Limited Resources (LOMO) | https://arxiv.org/abs/2306.09782 | SGD 型融合更新做 65B 全参微调 | 同上,SFT 域,未涉 RL 与组件级消融
- DeepSeekMath: Pushing the Limits of Mathematical Reasoning in Open Language Models | https://arxiv.org/abs/2402.03300 | GRPO 确立 RLVR 训练范式 | RLVR 实践源头之一,优化器沿用 AdamW,未讨论替代
最强反例:Why Transformers Need Adam(2402.16788)—— 它论证 SGD 在大规模 transformer 上失败的机制(块间 Hessian 异质),是"删 Adam"最强的反向证据;但其分析与实验全在 next-token 阶段,未覆盖 RL 目标下的损失几何。头条(RL 阶段可删自适应+动量)与它不冲突且正面互补,差异足以支撑 clear-accept。
重叠判定:low —— Adam 替代/省显存赛道拥挤但全部瞄准预训练与 SFT;"RLVR 阶段优化器组件是否必需"零命中。
实读篇数:8
编号自查:是(全部经 arXiv API 实际命中并核对标题)
裂缝证据核验(仅删承重假设形态,逐条覆盖该 idea 自报的 URL):
- https://arxiv.org/abs/1705.08292 | 核验:相符 —— 实读确认:多任务上自适应方法泛化劣于调好的 SGD,直接动摇"Adam 处处必需"
- https://arxiv.org/abs/2306.09782 | 核验:相符 —— 实读确认:SGD 型更新完成 65B 全参微调,后训练地形对朴素优化器宽容,与所称一致
