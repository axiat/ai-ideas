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
- 官方 Table 3 在 full-data LIBERO behavior-cloning setting 报告 AMPLIFY (Full) 的 success rate：Long 0.75、90 0.88、Object 0.93、Spatial 0.73、Goal 0.92；LIBERO 每任务共有 50 条 expert demonstrations。此处固定 Full AMPLIFY-50 为同 architecture、五 suite 与 success-rate metric 下的 strongest matched full-stack performance baseline；`strongest matched` 指固定的 full-stack 性能参照，不声称其数值在每个 suite 都全局最高。
- 官方 Figure 4 以 50 条/任务的 4%/10%/20%，即 2/5/10 条 action-labeled demonstrations，比较相同 Full AMPLIFY forward+inverse setting 的 few-shot policy；官方 Table 16 给出同 setting 的完整数字。Full AMPLIFY-5 的 success rate 为 Long 0.58、90 0.56、Object 0.67、Spatial 0.77、Goal 0.77。Full AMPLIFY-50 在 Long、90、Object 与 Goal 上高于 Full AMPLIFY-5，Spatial 为 0.73 对 0.77；50-demo 配置作为预注册 matched full-stack target，不依赖“逐 suite 数值全胜”的错误声称。
- Figure 4/Table 16 的 2/5/10-demo Full AMPLIFY 结果是“motion prior 在 low-data setting 有用”的支持证据，不是 correct-vs-shuffled conditioning 的反常或自相矛盾证据，本 fixture 不把它记作惊人先验。

命题占位核验:上述真实近邻没有单篇执行 matched correct-action / shuffled-action / action-free 视频预训练、scratch 灵敏度正控、预注册等价区间与中间带这一完整诊断。headline `overlap=low`；该结论只描述诊断命题，不编码 repair payload 是否已占。
最强反例:AMPLIFY(2506.14198)已经证明 action-free motion pretraining 可支持 low-data inverse policy，但没有 sensitivity-conditioned 2+3 acquisition，也没有证明 matched 视频预训练中的 action conditioning 不承重。
实读篇数:9；AMPLIFY 官方 Table 3、Figure 4 与 Table 16 已逐项核对，其他近邻沿用冻结 2026-07-17 查重边界。
编号自查:是；以上 arXiv URL 与标题均来自冻结查重记录。
重叠判定:low

受控 payload occupancy 事实（synthetic，仅供 policy regression）:同一检索边界内的最近实践已经实现完全相同的 sensitivity-conditioned acquisition rule——每任务 2 条 pilot 后只给 action-sensitive strata 增补 3 条，所有选择与调参见过的 target-task labels 均计入 5 条上限——并在相同 Full AMPLIFY forward+inverse stack、LIBERO 五 suite 与 success-rate metric 上，于预注册 -0.05 容差内匹配 Full AMPLIFY-50，实现同一 10× action-label reduction payoff；此受控事实不改变 headline `overlap=low`。
