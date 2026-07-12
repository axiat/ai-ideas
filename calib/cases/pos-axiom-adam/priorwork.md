# 查重快照时点:2026-01 底(ICML 2026 投稿前);所有记录以该时点可见文献为准

## I1
检索词:问题表述(optimizer choice RLHF RLVR post-training / SGD vs Adam reinforcement learning LLM / do we need adam RL);方法机制(memory-efficient optimizer large language model / adaptive learning rate momentum ablation / optimizer state memory actor);相邻领域(sparse parameter update RL finetuning / RL loss landscape KL / Muon Shampoo post-training)
API 检索:http://export.arxiv.org/api/query?search_query=abs:%22reinforcement+learning%22+AND+abs:%22optimizer%22+AND+(abs:%22RLHF%22+OR+abs:%22RLVR%22)+AND+submittedDate:[202301010000+TO+202601312359]&max_results=30&sortBy=submittedDate(30 篇逐条核对,无一做 RL 阶段优化器组件比较);http://export.arxiv.org/api/query?id_list=1705.08292,2402.16788,2505.11711,2507.20534,2511.08567,1810.02525,2306.09782,2403.03507
web 检索:optimizer choice RLHF Adam vs SGD paper → 未检出先行工作;SGD GRPO RLVR optimizer ablation → 未检出;Muon reinforcement learning post-training → Kimi K2;schedule-free/Shampoo/SOAP RLHF post-training → 未检出(均预训练语境);RLHF 优化器 SGD 替代 显存(中文)→ 未检出;wandb report GRPO PPO SGD optimizer → 未检出
工业界/非论文占位:GitHub issue 检索 TRL(#3218/#2492:SGD 仅作省显存配置项出现)、OpenRLHF、verl → RL 阶段 SGD 替代实验报告未检出;OpenRLHF 长期提供 adam_offload(把 Adam 状态挪 CPU)——承认优化器显存成本的工程绕法,非组件删减;OpenRLHF Muon 支持 v0.10.2 晚于本时点;Thinking Machines 博客「LoRA Without Regret」(thinkingmachines.ai/blog/lora/,2025-09,已入 TRL 官方文档)论证 RL 每 episode 信息量低、rank-1 LoRA 可匹配全参——低容量论证在参数化维度,未触优化器组件
最近工作:
- Why Transformers Need Adam: A Hessian Perspective | https://arxiv.org/abs/2402.16788 | 2024-02 | 用块异质 Hessian 解释 SGD 在 transformer 上失败 | 为"Adam 必需"辩护的最强论证;研究对象是 next-token 训练,未测 RL 阶段
- RL Finetunes Small Subnetworks in LLMs | https://arxiv.org/abs/2505.11711 | 2025-05(NeurIPS 2025)| RL 微调内在稀疏更新(5-30% 参数),跨 7 算法 10 模型,无稀疏正则 | 头条裂缝叙事的直接实证前驱:给出稀疏观察,未做优化器删减
- The Path Not Taken: RLVR Provably Learns Off the Principals | https://arxiv.org/abs/2511.08567 | 2025-11 | RLVR 更新集中于 off-principal 低曲率方向,稀疏是表象 | 为"RL 优化几何不同"提供理论;未触优化器选择
- Kimi K2 技术报告 | https://arxiv.org/abs/2507.20534 | 2025-07 | RL 阶段生产使用 Muon("As in SFT, we employ the Muon optimizer") | 证明 RL 阶段非 Adam 可行的工业先例;方向是更强优化器而非删减自适应+动量
- Where Did My Optimum Go?(Henderson et al.)| https://arxiv.org/abs/1810.02525 | 2018-10 | 经典深度 RL policy gradient 的优化器选择实证 | "RL 阶段优化器选择"命题的经典前驱;非 LLM/RLVR,结论亦偏向自适应有效
- The Marginal Value of Adaptive Gradient Methods | https://arxiv.org/abs/1705.08292 | 2017-05 | 多任务上自适应方法泛化不如调好的 SGD | 质疑"Adam 处处必需"的源头;限监督学习小规模
- Full Parameter Fine-tuning with Limited Resources(LOMO)| https://arxiv.org/abs/2306.09782 | 2023-06 | SGD 型融合更新完成 65B 全参微调 | 后训练地形宽容的证据;SFT 域,无组件级消融
- GaLore: Gradient Low-Rank Projection | https://arxiv.org/abs/2403.03507 | 2024-03 | 梯度低秩投影压缩优化器状态 | 同一 forcing constraint 的既有解法:压缩状态而非删掉自适应;预训练/SFT 域
最强反例:Why Transformers Need Adam(2402.16788)—— 论证 SGD 在大规模 transformer 上失败的机制(块间 Hessian 异质),是"删 Adam"最强反向证据;但其分析与实验全在 next-token 阶段,未覆盖 RL 目标下的损失几何。头条(RL 阶段可删自适应+动量)与它不冲突且正面互补,差异足以支撑 clear-accept。
重叠判定:low —— 「RLVR 阶段优化器组件消融/纯 SGD 替代」在论文(API 时窗扫描 30 篇)、框架 issue(TRL/verl/OpenRLHF)、W&B、中英文社区均零检出;但裂缝叙事已被高度预热(同组前作 2505.11711 的稀疏观察、2511.08567 的几何解释、K2 的 RL 阶段 Muon 实践),属"直接重叠低、可预见性高"。
实读篇数:8(2402.16788 与 2505.11711 实读全文;其余实读摘要与方法节)
编号自查:是(全部经 arXiv API 实际命中并核对标题)
裂缝证据核验(仅删承重假设形态,逐条覆盖该 idea 自报的 URL):
- https://arxiv.org/abs/1705.08292 | 核验:相符 —— 实读确认:多任务上自适应方法泛化劣于调好的 SGD,直接动摇"Adam 处处必需"
- https://arxiv.org/abs/2306.09782 | 核验:相符 —— 实读确认:SGD 型更新完成 65B 全参微调,后训练地形对朴素优化器宽容,与所称一致
