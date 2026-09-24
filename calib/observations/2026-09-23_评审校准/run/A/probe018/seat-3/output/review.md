# I4
Verdict: reject
CRITICAL: 0
MAJOR: 1
Headline: 位置频率与记忆正确性的机器人交互实验具有可检验增量，但当前设计尚未排除位置技能训练量差异对结果的解释（candidate.json「Minimal Falsification Experiment」；prior_work.md「Occupation Check」）。
Occupation: KAFT 已覆盖参数知识与外部上下文冲突及反事实监督修复，DisentQA 已覆盖配对上下文监督，MemER 已提出机器人训练顺序先验影响历史使用的解释；已读正文中剩余的具体差别是固定骨干下的位置频率干预及行动成功率交互，不能将总体机制或配对训练本身作为新发现（prior_work.md「Nearest Work」「Strongest Counterexample」）。
Experiment: MAJOR-1：固定总轨迹数和动作次数仍会使低频位置获得更少训练曝光，因此成功率交互可能来自位置技能不足；测试检查点配对控制了测试输入，首次动作响应提供了辅助诊断，但二者均未独立校准逐位置动作能力。需要可见目标或等价的逐位置执行能力对照，才能把剩余增量归因于先验影响记忆使用；修复阶段的位置均衡基线不能替代该控制（candidate.json「Minimal Falsification Experiment」）。
Estimand: 正确减错误记忆的成功率差之差，对齐训练频率是否调节记忆正确性总效果的窄问题，未发现该操作性估计量本身失配；其机制解释仍受 MAJOR-1 限制。应明确将效应定义为均匀条件减强偏置条件，并分别报告正确减空白、错误减空白，以解释收益变化及对应的终止阈值（candidate.json「Minimal Falsification Experiment」）。
Payoff: 可归属的新贡献应是机器人位置频率干预的机制证据，以及配对历史训练超过位置均衡微调的收益；反事实监督机制已有直接近邻。MemER 与 RoboMME 的观察支持研究动机，但不能证明该具体收益；10 点交互和 5 点修复收益属于候选阈值，未计作实验证据（prior_work.md「Nearest Work」「Payoff / Implementation Check」；candidate.json「Minimal Falsification Experiment」）。
Feasibility: 候选列出每个基础策略 12,000 条轨迹、300 个配对检查点、额外 3,000 条样本、三个修复对照、3 个种子及 4 张 A100／120 GPU 小时，并提供主张与修复的终止条件，构成有界实验方案；骨干和单次运行耗时未给出，因此总计算预算能否覆盖全部训练仍待核算，当前主要缺陷是归因控制（candidate.json「Minimal Falsification Experiment」）。
History: unavailable
Reason: 已有工作占据总体机制与修复思路，而剩余机器人交互的核心归因尚受 MAJOR-1 影响，冻结方案不足以支持从默认拒绝提升为接受（prior_work.md「Overlap」「Strongest Counterexample」；candidate.json「Minimal Falsification Experiment」；review_contract.md「Gates」）。