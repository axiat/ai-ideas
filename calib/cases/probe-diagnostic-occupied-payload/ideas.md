# Synthetic policy regression fixture

校准属性:synthetic policy regression；只检查 verdict policy，不是论文金标，也不提供外部 ground truth。

## I1
一句话故事:在 architecture、视频、训练 token、优化预算与下游评测完全 matched 的视频预训练中，检验 action conditioning 是否承重；若不承重，用 sensitivity-conditioned acquisition 将每任务 action-labeled demonstrations 从 50 条降到至多 5 条并匹配 Full AMPLIFY-50 full-stack performance。
主题:世界模型-训练目标
形态:瓶颈定位实验（绑定 action-label acquisition 修复臂）
简述:固定 Full AMPLIFY 的 forward+inverse stack、LIBERO 五个 suite 与 success-rate metric，只改变视频预训练的 conditioning：正确 action、在轨迹内打乱 action、无 action。诊断头条只主张“matched 视频预训练中 action conditioning 是否承重”，不命名残余收益的正向机制。正确/打乱/无 action 三臂使用同一冻结 source-video split、相同帧数、训练 token、optimizer steps 与下游 inverse-policy protocol；target-task action labels 只来自下述 5-demo 总预算。

主判据与正控:主要诊断集由 Full AMPLIFY-50 公布 success 位于 [0.20,0.85] 的非饱和 suite 构成，完整五 suite 仍全部报告。定义 Δ=SR(correct-action)-SR(shuffled-action)，按 task 与 seed 分层 bootstrap 95% CI；CI 完全落入 [-0.03,0.03] 才判 action conditioning 不承重，CI 下界高于 0.10 则判承重并杀死 idea，其余均为预注册中间带，只报 inconclusive。shuffled 与 action-free 的差也必须落入同一等价带，否则不作“不承重”结论。另用同一 architecture 的随机初始化 scratch arm 在冻结 source audit split 上做 correct-vs-shuffled action-prediction 正控；其相对 NLL 改善的 95% CI 下界须高于 10%，否则判 assay 无灵敏度，主实验无效。scratch audit 不读取 target-task labels。

最小否证实验:在读取任何三臂结果前，用冻结的 per-task rollout variance 做 bootstrap power simulation；correct/shuffled/action-free 三臂使用相同 paired seed IDs，从 N∈{6,8,12} 中选择同时对 ±0.03 等价判定和 Δ≥0.10 检出达到≥90% power 的最小 N；若 N=12 仍不足，fixture 作废且不得进入 panel。通过 power gate 后，在 LIBERO 上顺序使用 1×H100 跑三臂各 N 个 paired seeds。若上述 correct-vs-shuffled 95% CI 下界 >0.10，或 scratch 正控失败，头条即死。若通过等价判据，才运行同一个 repair：每任务先取 2 条 action-labeled demonstrations，同时用于 pilot、sensitivity 分层、模型选择与调参；只对预注册 sensitivity score 为正的 strata 再取 3 条/任务。每任务 action-label 总预算按 5 条计，包含 2+3 pilot/acquisition、选择与调参见过的全部 target-task labels；未用额度不得跨任务转移，也没有额外 labeled validation、budget sweep 或隐藏调参集。评测 rollout 不提供 action labels。

repair payoff 与唯一 baseline:唯一 payoff baseline 是相同 architecture、LIBERO 五 suite 与 success-rate metric 下的 Full AMPLIFY-50 full-stack forward+inverse 配置，即每任务 50 条 action-labeled demonstrations。repair 保持同一 Full AMPLIFY forward+inverse stack，只改 action-label acquisition；固定其公开 protocol 并在同一组 paired seeds 重跑。repair 成功须在每个 suite 上使 SR(repair-5)-SR(Full AMPLIFY-50) 的分层 bootstrap 95% CI 下界均不低于 -0.05。达到该预注册 matched-performance 容差才记为 10× action-label reduction；未达到只算诊断结果，不记 payoff。机制、2+3 acquisition rule、5-demo 上限、评测与 Full AMPLIFY-50 baseline 在两个 paired fixture 中完全相同。

为何可能新:matched correct/shuffled/action-free 视频预训练的判别实验在给定检索边界内没有 direct hit；是否能据此获得新的 10× action-label payoff 由 priorwork.md 中独立的 payload occupancy 事实决定。
