# Synthetic policy regression fixture

校准属性:synthetic policy regression；文献事实来自冻结查重与 AMPLIFY 官方论文，末尾单条 occupancy 事实仅为受控 policy 输入，不作为论文事实或外部 ground truth 发布。

## I1
检索边界:冻结于 2026-07-17；问题表述(matched action-conditioned vs shuffled-action vs action-free video pretraining for control)、方法机制(action conditioning ablation / inverse dynamics / actionless motion prior)、相邻领域(latent action / video world-model transfer)。覆盖 arXiv/API、web 与公开项目页；两个 paired fixture 使用相同边界与同一组近邻。

最近工作:
- AMPLIFY: Actionless Motion Priors for Robot Learning from Videos | https://arxiv.org/abs/2506.14198 | forward dynamics 从 action-free videos 学 motion tokens，inverse policy 从 action-labeled demonstrations 学动作；没有 matched correct/shuffled/action-free conditioning 三臂。
- Learning Transferable Dynamics Priors from Action to World Modeling (A2World) | https://arxiv.org/abs/2606.29501 | action-conditioned world-model pretraining 后迁移控制；没有 shuffled-action matched control。
- What Makes Video World Model Latents Action-Relevant: Prediction over Reconstruction | https://arxiv.org/abs/2606.07687 | 研究 prediction-vs-reconstruction 与 action relevance；不是 matched conditioning occupancy 实验。
- GR-1 | https://arxiv.org/abs/2312.13139 | action-free web-video pretraining 后做机器人控制；没有 matched action-conditioning 判别。
- GR-2 | https://arxiv.org/abs/2410.06158 | action-free video-language pretraining 后接动作学习；没有 shuffled-action control。
- Learning to Act without Actions (LAPO) | https://arxiv.org/abs/2312.10812 | 从无动作视频恢复 latent actions；仍以 latent action structure 为核心，不回答显式 conditioning 是否承重。
- LAPA | https://arxiv.org/abs/2410.11758 | latent-action pretraining；没有本 fixture 的 matched 三臂与 2+3 acquisition payoff。
- iVideoGPT | https://arxiv.org/abs/2405.15223 | action-conditioned interactive video world model；组件消融不等于 correct/shuffled/action-free matched pretraining。
- UniPi | https://arxiv.org/abs/2302.00111 | video generation as policy 并用 inverse dynamics 抽取动作；没有本 fixture 的 conditioning 判别。

AMPLIFY 数值锚:
- 官方 Table 3 在 full-data LIBERO behavior-cloning setting 报告 AMPLIFY (Inverse only) 的 success rate：Long 0.76、90 0.83、Object 0.64、Spatial 0.83、Goal 0.92；LIBERO 每任务共有 50 条 expert demonstrations。此处固定该 50-demo full-data inverse-policy 配置为 AMPLIFY-50；在相同 AMPLIFY architecture、五 suite 与 success-rate metric 的 action-label payoff 比较中，它是 strongest current matched comparator。
- 官方 Figure 4 以 50 条/任务的 4%/10%/20%，即 2/5/10 条 action-labeled demonstrations，比较相同 forward/inverse setting 的 few-shot policy；官方 Table 16 给出同一 setting 的完整数字。Table 16 的 5-demo inverse-only success 为 Long 0.04、90 0.18、Object 0.09、Spatial 0.16、Goal 0.04，逐 suite 均低于 Table 3 的 AMPLIFY-50；这既证明非饱和 headroom，也给 scratch 正控与 5-vs-50 payoff comparison 提供同源锚。
- Figure 4/Table 16 同时显示 video-pretrained AMPLIFY 在 2/5/10 条 action data 下优于 inverse-only few-shot arm；这是“motion prior 在 low-data setting 有用”的支持结果，不是 correct-vs-shuffled conditioning 的反常或自相矛盾证据，本 fixture 不把它记作惊人先验。

命题占位核验:上述真实近邻没有单篇执行 matched correct-action / shuffled-action / action-free 视频预训练、scratch 灵敏度正控、预注册等价区间与中间带这一完整诊断。headline `overlap=low`；该结论只描述诊断命题，不编码 repair payload 是否已占。
最强反例:AMPLIFY(2506.14198)已经证明 action-free motion pretraining 与 few-shot inverse policy 可显著优于 inverse-only few-shot，但没有 sensitivity-conditioned 2+3 acquisition，也没有证明 matched 视频预训练中的 action conditioning 不承重。
实读篇数:9；AMPLIFY 官方 Table 3、Figure 4 与 Table 16 已逐项核对，其他近邻沿用冻结 2026-07-17 查重边界。
编号自查:是；以上 arXiv URL 与标题均来自冻结查重记录。
重叠判定:low

受控 payload occupancy 事实（synthetic，仅供 policy regression）:同一检索边界内没有任何实践实现该 sensitivity-conditioned acquisition rule——每任务 2 条 pilot 后只给 action-sensitive strata 增补 3 条，所有选择与调参见过的 target-task labels 均计入 5 条上限——也没有实践在相同 AMPLIFY architecture、LIBERO 五 suite 与 success-rate metric 上以至多 5 条/任务于预注册 -0.05 容差内匹配 AMPLIFY-50，实现同一 10× action-label reduction payoff；此受控事实不改变 headline `overlap=low`。
