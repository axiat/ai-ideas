# Research Context（供每周 idea routine 使用）

> 更新日期：2026-07-03。本文件只是可选灵感来源，**不构成约束**——发散规则见 `brainstorming_policy.md`：每轮至多 1-2 个 idea 与以下方向直接相关，其余应跳出此栈自由发散。

## 核心方向

DSRL 式的 frozen VLA 后训练/引导：backbone（π0.5）冻结，在 latent noise / action 空间用 RL（SAC 系）做 steering。关注 inference-time control 与 frozen backbone modulation，而非全量微调。

## 当前技术栈与工作线

- 基座：π0.5（flow-matching action head 的 VLA），openpi + jaxrl2 代码栈，仿真 manipulation 任务。
- 奖励设计：VLM-based reward shaping —— subgoal 完成检测触发奖励、progress value（ProcVLM 式 procedure-grounded progress reward、VLLR 式 generalizable dense reward + policy self-certainty intrinsic reward）。
- VLM 信号用途（已验证的定位）：reward shaping、SAC critic 的 state augmentation、episode 级静态任务分解。

## 已验证的关键结论（避免生成与之矛盾的 idea）

- π0.5 对 subgoal-level text prompt 响应不可靠（V1/V1-extra 实验：SNR 低，closed-loop 反例），"VLM 输出 subgoal text 喂给 VLA" 的通路已被砍掉；VLA 始终输入 full task text。
- VLM judge 存在 sim 视觉穿模等感知遗留问题，reward 信号需做 audit / 多帧 / temporal 校验。

## 兴趣关键词

World model（latent dynamics、可交互世界模型、model-based RL for robotics）、VLA 后训练（RL post-training、noise-space steering、adaptive chunk replanning）、inference dynamics、frozen backbone 调制、VLM-as-judge 奖励、长时序 manipulation。

## 资源设定（评审 feasibility 用）

单人研究者，每周约 40 有效小时，H100 级多卡 GPU，工程能力强，偏 applied。
