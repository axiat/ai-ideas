# Weekly Embodied Idea Scout — 2026-07-19

> **本周无达标 idea(0 Strong Accept)。** 如实报告:一轮完整回路——发散 10 → 自筛 5 → 预筛杀 2 → 深查重 3 → 三遍打分取最低,默认 Reject。最接近的是 2 个 accept-w-rev(附录 B1/B2),均因 novelty 天花板未达 clear-accept。**宁可无达标,不可放水凑数。**

评审日期:2026-07-19 ｜ 来源:weekly ｜ 尝试轮数:1（发散 10 → funnel）｜ 打分:每个存活 idea 独立三遍、取最低票,默认 Reject ｜ 单 agent 近似 hunt 反串通(默认 Reject、对抗查重、三遍取最低、SA 硬门槛自检)。

---

## 1. 本周文献综述(过去 7–14 天为主,相邻近月为辅)

### WorldModel(世界模型)

近两周三条主线清晰:**「还需不需要逐帧生成视频」之争**、**「世界模型当策略评委」竞赛**、以及 **model-based RL/规划走向 diffusion/consistency**。

- **GigaWorld-1**（[2607.02642](https://arxiv.org/abs/2607.02642)）——把世界模型系统性地当**策略评委**,提出 WMBench(真机遥操+执行);断言评委质量由**长时程、动作忠实的 rollout 一致性**主导,视觉逼真度是错的目标。
- **Structured 4D Latent Predictive Model**（[2607.01166](https://arxiv.org/abs/2607.01166)）——在结构化 4D(3D-over-time)潜空间做视频预测规划,预测未来经目标条件逆动力学转动作;假设显式 3D 几何对多视角一致性必要。
- **TACO — Tactile World Model as Self-Corrector**（[2607.02840](https://arxiv.org/abs/2607.02840)）——Recognize-Imagine-Label 环,用**触觉感知** WM 把真机失败变合成纠正数据(无人工监督);假设纯视觉 WM 会生成「视觉合理但接触不一致」的轨迹。
- **What Makes Video WM Latents Action-Relevant: Prediction over Reconstruction**（[2606.07687](https://arxiv.org/abs/2606.07687)）——诊断:是**时序预测**而非像素重建保真让 latent 变 action-relevant,高重建模型可在动作上失败。
- **How Should World Models Be Evaluated for Embodied Decision-Making?**（[2606.15032](https://arxiv.org/abs/2606.15032)）——L0–L7 评测阶梯,从视觉合理度升到策略优化效用(含 exploitability、不确定度校准);断言决策效用而非感知质量应居证据顶端。
- **LaWAM**（[2606.15768](https://arxiv.org/abs/2606.15768)）——在冻结视觉基座潜空间预测紧凑潜子目标而非重建未来帧,~24× 快于像素空间。
- **ImageWAM — Do World Action Models Really Need Video Generation, or Just Image Editing?**（[2606.19531](https://arxiv.org/abs/2606.19531)）——用预训练**图像编辑**模型替代视频生成 WM,只建目标帧变换。
- **WoW-World-Eval**（[2601.04137](https://arxiv.org/abs/2601.04137)）/ **WorldArena**（[2602.08971](https://arxiv.org/abs/2602.08971)）——具身 WM 评测基准;报告视频基座在长时程规划(~17%)与真机执行(~0%)崩溃,揭示显著的 perception-functionality gap。
- model-based RL/规划:**MBDPO**（[2605.26282](https://arxiv.org/abs/2605.26282)，轨迹扩散统一 search 与 value）、**Interactive World Simulator**（[2603.08546](https://arxiv.org/abs/2603.08546)，consistency model 稳定交互)。
- 自适应算力 WM(预筛阶段浮现,与本周删公理候选相关):**Looped World Models**（[2606.18208](https://arxiv.org/abs/2606.18208)）、**DLWM**（[2606.15160](https://arxiv.org/abs/2606.15160)）、**SANTS**（[2605.27947](https://arxiv.org/abs/2605.27947)）、**One-Token-Per-Frame**（[2605.07931](https://arxiv.org/abs/2605.07931)）。

### VLA(视觉-语言-动作)

三处饱和:**推理效率(缓存/快慢异步)**、**flow/diffusion VLA 的 RL 后训练**、**双系统快慢架构**。

- **VLA-Corrector**（[2607.01804](https://arxiv.org/abs/2607.01804)）——监控开环执行中 latent 视觉特征漂移,触发自适应重规划/缩短 horizon;假设 latent 偏移是廉价可靠的早警信号。
- **ActionCache**（[2607.06370](https://arxiv.org/abs/2607.06370)）——免训练复用过去去噪中间态热启动,报告最高 11.75×/34.43× 提速;依赖动作的时间冗余前提。
- **VLA for UAV & Bimanual: A Review**（[2607.06706](https://arxiv.org/abs/2607.06706)）——183 篇综述,断言双手操作与空中控制有「基础算法并行」、动作表征跨本体可迁移(survey 级断言,未受控验证)。
- **FlowDAgger**（[2607.08877](https://arxiv.org/abs/2607.08877)）——把人纠正经逆时积分映回噪声 latent 适配冻结 flow/diffusion 策略;假设专家动作可干净反演且 latent DAgger 保预训练分布。
- **Z-1**（[2606.31846](https://arxiv.org/abs/2606.31846)）/ **π_RL**（[2510.25889](https://arxiv.org/abs/2510.25889)）——flow VLA 的高效/在线 RL(task-wise GRPO；Flow-Noise/Flow-SDE 解 log-likelihood 可解性)。
- **RobustVLA**（[2511.01331](https://arxiv.org/abs/2511.01331)）——RL 后训练加 Jacobian/平滑正则提部署鲁棒;把鲁棒等同局部 Lipschitz 敏感度。
- **ActionCodec**（[2602.15397](https://arxiv.org/abs/2602.15397)）——用信息论准则(最大时间重叠、最小词表冗余、token 独立)设计 tokenizer,断言重建保真是错的目标,只报 IL 结果、无 RL。
- **FASTer**（[2512.04952](https://arxiv.org/abs/2512.04952)）、**DuoCore-FS**（[2512.20188](https://arxiv.org/abs/2512.20188)，异步快慢)、**DUST**（[2510.27607](https://arxiv.org/abs/2510.27607)，双流扩散 WM-VLA)、**Discrete Diffusion VLA**（[2508.20072](https://arxiv.org/abs/2508.20072)）、**Don't Blind Your VLA**（[2510.25616](https://arxiv.org/abs/2510.25616)，动作 SFT 侵蚀预训练视觉表征)。

## 2. 趋势与 gap 分析

**趋势:**
1. **「视频生成是否必要」成为世界模型最热的一等争论。** ImageWAM(图像编辑)、LaWAM(潜子目标)、Prediction-over-Reconstruction、Structured-4D 各自删掉一块(时序/重建/几何)并**互相矛盾地**声称自己那块承重——这是「删承重假设」体裁最肥的矿脉,但也意味着任一单点删除都已被某篇占据。
2. **世界模型即评委**成建制:GigaWorld/WorldArena/WoW-World-Eval/L0–L7 都在把「视觉逼真≠决策效用」形式化,竞相定义闭环/排序指标。
3. **VLA-RL 后训练**密集围绕 flow 策略 log-likelihood 不可解这单一障碍(MDP 离散化 / ODE→SDE + GRPO);**推理效率**密集围绕动作时间冗余(缓存/快慢异步)。

**Gap(本周探过但均未能转成 clear-accept 命题):**
- 评委的**不确定度校准 / exploitability** 被 L0–L7 命名却几乎无人实测(→ 候选 #2 命中此 gap,但被 StressDream 的共享前提与 MSE→mode-collapse 的「预期性」封顶)。
- **tokenizer 信息结构 × RL 可微调性**几乎无人连接,只连到 IL 成功率(→ 候选 #1 命中此 gap,但 ActionCodec 占轴、Subwords-as-Skills 反向近邻使差异不足 clear-accept)。
- WM 自纠正合成数据的**盲区继承**(→ 候选 #8,但机制=MOPO/model-collapse 教科书结论,已被 Uncertainty-Aware RWM 在真机实证占据)。
- 三条最新 gap 的共同点:**building blocks 全部新鲜且真实,但相邻工作已近到让差异读作「可预期推论」**——这是本周 0-SA 的直接原因,与近月 ledger 高度饱和一致。

## 3. 达标 idea（Strong Accept)

**本周无达标 idea。** 无任一候选通过 SA 硬门槛(三遍全票 strong-accept + novelty 经独立对抗查重为 low + 实读 ≥5 + 最小否证实验 + 完整 rubric + 差异足以支撑 clear-accept)。三个进入深查的存活候选(#1/#2/#8)在**独立对抗查重**中均被判定「head 未逐字发表、但存在共享承重前提的近邻工作,差异不足 clear-accept」,即 novelty 封顶;在**三遍默认-Reject 打分**下无一能拿到三票 strong-accept。

---

## 4. 附录:borderline（accept-w-rev,仅供一览,不计入达标)

### B1 — 世界模型即评委的排序误差主因是「方差盲」而非乐观偏差（世界模型-训练目标）

**一句话:** 学习式 WM 当策略评委时,因 mean-seeking(MSE/众数)训练目标,对**同均值、不同结局方差**的两策略给出近乎相同的 mean rollout,故系统性**低估低方差(鲁棒)策略**;命名 evaluator **variance-discrimination**,方差保持(随机/集成 rollout)WM 应恢复方差排序。

**最小否证实验:** 在 sim(RoboCasa/LIBERO)构造成对策略——同真机成功率均值、不同结局方差(如同一 checkpoint 加不同温度/噪声)。用生成式 WM 评委(GigaWorld 式,少 rollout 锐化)排序 vs 多 rollout 真值 risk-adjusted 排序,测 variance-discrimination 一致性。信号:锐化单-rollout WM 无法区分同均值配对(排序 ~随机)、方差保持 WM 追平 risk 排序即证成;方差保持仍不涨则命题死。单人 1×H100 可执行(sim 真值廉价)。

**三遍 verdict:** accept-w-rev / accept-w-rev / accept-w-rev → **最低 = accept-w-rev**。

**定向查重记录(实读 ≥5):**
- 最近邻 **StressDream**（[2606.00267](https://arxiv.org/abs/2606.00267)）:steer WM 初噪找「high-impact 却合理」的尾部未来;**共享**「mean/nominal rollout 漏尾部,除非采极多样本」这条承重前提,但目标是 risk surfacing + 策略改进,**不拥有** ranking estimand、不 claim 系统性低估低方差策略、不做 det-vs-stochastic 排序恢复实验。**最强反例。**
- **WorldGym / Evaluating Robot Policies in a WM**（[2506.00613](https://arxiv.org/abs/2506.00613)）:确立**乐观偏差**(低估 in-dist、高估 OOD 动作、幻觉 OOD 成功)——**已排除的头条**,非本命题。
- **GigaWorld-1**（[2607.02642](https://arxiv.org/abs/2607.02642)):consistency=单轨视觉保真,非跨样本方差;无 variance-discrimination 分析。
- **WorldEval**（[2505.19017](https://arxiv.org/abs/2505.19017)）、**dWorldEval**（2604.22152）、**Scalable Policy Eval with Video WM**（2511.11520):报 Pearson/rank 相关,不分解 bias vs variance。
- 邻域(risk-sensitive OPE):Universal OPE(Chandak NeurIPS'21)、CVaR-OPE(2312.00342)——按 risk 排序的**解**已存在,但未作为 learned-WM-evaluator 的**失败诊断**命名。
- API:`abs:"world model" AND abs:"policy evaluation" AND abs:variance`→1(无关 TD-Flows);`all:"world model" AND all:"policy selection" AND all:risk`→**0**。
- **重叠判定:low–medium**(head/估计量独立查重零命中,上界被 StressDream 共享前提抬离纯 low)。

**为何止步 AwR:** 头条与估计量未被占,但 (a) StressDream 共享承重前提,(b) 「mean-seeking→方差盲」是 MSE→mode-collapse 的**预期推论**、惊喜低,(c) 独立查重明判「非 automatic clear accept」——需 bias-vs-variance 排序误差分解 + 排除 StressDream steering 已解 ranking,才可能达 clear-accept。三遍默认-Reject 下难拿全票 SA;绑方差保持修复臂故过 reject 门,封顶 borderline。

### B2 — tokenizer 的 IL-最优信息结构未必是 RL 后训练最优（动作表征）

**一句话:** 让动作 tokenizer 在**模仿学习**上最优的信息论性质(低词表冗余、高时间重叠,ActionCodec)**未必**、甚至可能**有害于** RL 后训练可微调性——低冗余尖峰 token 分布给 RL(GRPO / flow-noise-MDP / π_RL 式)退化或平坦梯度;命名解耦估计量 **tokenizer RL-fine-tunability**,命题:RL 提升与 IL-最优冗余度**反相关**。

**最小否证实验:** 同 VLA + RoboCasa,tokenizer 冗余度从低(ActionCodec 式)到高扫,各跑 GRPO **和** flow-noise-MDP 两种 RL 后训练,测 RL lift vs IL lift。信号:RL lift 与 IL-最优冗余度反相关(≥5pt 分离)且跨两种 RL 制度一致即证成;无反相关或仅单制度出现(可归混杂)则命题死。单人 1×H100 可执行(小 VLA + sim RL)。

**三遍 verdict:** accept-w-rev / accept-w-rev / accept-w-rev → **最低 = accept-w-rev**。

**定向查重记录(实读 ≥5):**
- **ActionCodec**（[2602.15397](https://arxiv.org/abs/2602.15397)）:定义 IL-最优信息轴,**只报 IL、零 RL**——是前提不是竞争者,但**占据了「好 tokenizer 的信息论性质」这一框架**。
- 最强反例(反向近邻)**Subwords as Skills**（[2309.04459](https://arxiv.org/abs/2309.04459)，NeurIPS'24):BPE 式动作 tokenization 的粗粒度/压缩/时间重叠**帮助**稀疏奖励 RL 探索——与本 idea「压缩伤 RL」**方向相反**,是必须正面迎战的已发表反证。
- **Sparse but Critical**（[2603.22446](https://arxiv.org/abs/2603.22446)）:RL 微调只改稀疏少数 token 分布——支持「尖峰↔RL」机制,但测 RL **诱导的**偏移,不连接预存 tokenizer 峰度与 RL 可微调性。
- **RL Token**（[2604.23073](https://arxiv.org/abs/2604.23073)）、**ExToken**(IROS'26)、**π_RL**（[2510.25889](https://arxiv.org/abs/2510.25889)）:VLA-RL 后训练把 tokenizer 当固定量优化 RL 过程,**无一**把 tokenizer 信息结构当自变量测 RL lift。
- LLM-RL 机制邻域:Ignore-the-KL-Penalty（[2502.06533](https://arxiv.org/abs/2502.06533)）、High-Entropy-Minority-Tokens——低熵/尖峰 token 位约束 RL 探索(文本域,支持机制)。
- API:`all:"action tokenizer" AND all:reinforcement AND all:fine-tuning`→**1**(无关);`all:"action tokenizer" AND all:reinforcement`→21(全固定 tokenizer);`all:"vocabulary size" AND all:reinforcement AND all:exploration`→**0**。
- **重叠判定:low–medium**(具体命题=IL-最优⇄RL-敌对反相关 + 命名估计量,直接查重零命中;上界被 ActionCodec 占轴 + Subwords-as-Skills 反向近邻抬离纯 low)。

**为何止步 AwR:** head 未占,但 (a) ActionCodec 已占「tokenizer 信息论性质」框架,(b) **Subwords-as-Skills 给出反方向已发表证据**(压缩/时间重叠助 RL),使「压缩伤 RL」的单调命题被削弱、需干净因果隔离(排除 codebook 覆盖/动作误差校准混杂)且跨 ≥2 RL 制度才成立,(c) 独立查重明判「非 automatic clear accept,可被读作 ActionCodec + 关键 token RL 文献的可预期推论」。三遍默认-Reject 难拿全票;绑双-RL-制度否证实验故过 reject 门,封顶 borderline。

## 5. 被拒 idea 简表

| id | 一句话 | 拒因 | overlap |
|---|---|---|---|
| #3 | 删掉「操作 WM 对 rollout 内所有未来时刻等预算建模」,只在决策分叉点高保真(**删承重假设形态,五字段齐**) | **预筛直接占位**:Looped World Models(2606.18208)/DLWM(2606.15160)/SANTS(2605.27947)/One-Token-Per-Frame(2605.07931)已把「按状态复杂度/决策相关性非均匀分配 WM 算力」做遍 | high |
| #6 | VLA 组合 OOD 失败是 binding 而非 Jacobian 敏感度,失败率随组合新颖度而非扰动幅度 | **预筛直接占位**:Robust Skills Brittle Grounding(2602.24143)已诊断 held-out object-region 配对 44%→0% 的 binding 失败,+LiLo-VLA(2602.21531)/ACT-VLA(2607.00351) | high |
| #8 | WM 自纠正合成数据继承 WM 盲区(correction echo chamber),增益随 WM 局部置信下降而衰减 | 对抗深查:机制=MOPO(2005.13239)/model-collapse 教科书结论,Uncertainty-Aware RWM(2504.16680)已在真机用置信度加权 WM 合成数据(目标域非零命中),机制迁移被 dominated,唯一未发表的真-vs-合成配对对照 thin | medium |

**自筛淘汰(未进 funnel,未获评审,不入 ledger):** #4 latent-DAgger 改意图纠正扭曲预训练分布(HITL 饱和)、#5 ActionCache 精度损失集中接触相(接触相饱和)、#7 UAV↔bimanual 可迁移性由 control-bandwidth 决定(借 survey、弱)、#9 action-relevance 承重件=单步接触事件编码(与 2606.07687 太近)、#10 DuoCore-FS staleness 在子目标边界坍缩(子目标边界饱和)。

## 6. 元信息

- **尝试轮数:1**(发散 10 → 自筛 5 → 预筛杀 2(#3/#6,overlap=high)→ 深查 3(#1/#2/#8)→ 三遍打分取最低)。
- **删公理配额:成 #3**(五字段齐:删哪条=WM 逐帧等预算生成;为何现在能删=WoW/WorldArena 揭示长时程非均匀崩溃;forcing=evaluator 长时程推理算力;裂缝证据 2 条=WoW-World-Eval 长时程~17%/WorldArena perception-functionality gap;否证=决策分叉点高保真 vs 均匀预算测 ranking agreement)——配额以「成」计,预筛被自适应算力 WM 群占位 → reject,非放水。
- **低存量主题反坍缩:达标。** 进 funnel 的 5 个候选中 #1(动作表征,36)、#2(世界模型-训练目标,36)、#3(世界模型-架构,35)三个落在 ≤第三低存量(36,并列计入)主题,≥2。
- **每个 SA 的三遍 verdict:** 无 SA。
- **最接近达标的 2 个:** B1(evaluator 方差盲,overlap low–medium,差 novelty 一档——StressDream 共享前提 + MSE-mode-collapse 预期性)、B2(tokenizer IL⇄RL 反相关,overlap low–medium,差在 Subwords-as-Skills 反向证据使单调命题需更强隔离)。
- **纪律:** 单 agent 近似 hunt 反串通——默认 Reject、生成/查重/打分分阶段(查重由独立 subagent 对抗式完成,prompt 明令「证明已被做过」)、三遍取最低、novelty 只认独立查重证据、SA 硬门槛自检。
