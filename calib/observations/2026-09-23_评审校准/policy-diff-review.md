# 裁判规则修改复核

新增 AwR 条款保留了独立研究价值、有限修订和既定资源要求，未把实验便宜、填入 supports、零命中或没有 CRITICAL 变成接收依据。两个 MAJOR 禁止 SA、真正占用可在一个 MAJOR 时 Reject、无 repair 的重要意外诊断可获正面评价，均已有明确条款。仍有三项旧表述会抵消这些修正。

## 必须修正

### P1：五维分数仍机械否决知识型贡献

位置：`rubric.md:768–772`。

该段规定没有任何维度达到 7 就不能 SA，并将 policy 的约 6,6,8 审稿校准解释为必须具有约 8 分的改进维度。五个维度主要评价效果、速度、鲁棒性、成本与跨域能力，不能完整表征重要的新解释或评价结论纠正。此规则会使主评审允许的无 repair 诊断，在消费 rubric 的 AwR judge 被再次封顶。

反例：有对齐证据支持的重要排序反转，能够改变实际评估结论，但不提高任何方法的成功率或速度。新版 policy 允许按新增认识评价，rubric 数值规则仍可将其降级。

修正：移除维度分数的机械 SA 否决；说明 clear-accept 评价可以由有依据的知识贡献承担，维度分数仅提供适用于该贡献类型的诊断。评分均低也不能反过来强制提高分数。

### P1：迁移特例仍无条件要求因果论证

位置：`brainstorming_policy.md:65`，v1/v2 review contract 的 transferred mechanism 条款。

一般条款已要求按主张类型评价，但迁移段仍要求 specific causal argument，并以 weak causal support 拒绝突破上限。形式化迁移可能通过复杂度证明、误差界或可验证的不变量建立收益，不主张内部因果机制。该特例会覆盖一般条款，继续误伤这种贡献。

反例：目标控制约束强迫新的缓存失效规则，候选给出安全复用的充分条件和最坏情况误差界；独立近邻检索无直接覆盖，收益由推导支持，实验仅检验假设的实际适用范围。缺少内部因果干预不能成为独立缺陷。

修正：要求与主张相符的具体、可核查论证，可由观察、分析、推导或因果识别提供；只有因果/机制解释主张需要相应识别。仍保留目标约束强迫的实质适配、强基线、归因和 clear-accept 收益要求。没有依据的预期收益继续不能满足此项。

### P1：主评审的证据禁令过宽

位置：`roles/review.md:3` 的 Candidate claims are not evidence。

该句能合理排除自报新颖性和未经核验的收益断言，但字面上同时排除 candidate 中可直接检查的推导、反例或数学论证。candidate 与 prior-work 是该评审仅有的学术输入；新问题的原始推导通常不会先存在于 prior-work 中。

修正：将禁令明确限定为未经支持的新颖性、观测结果和收益断言。允许评审核查 candidate 内的推导、设计和带来源的观察，同时分别记录已知事实、假设和预测。引用 candidate 原文只证明候选确实如此声称，并未自动证明其经验断言为真。

## v2 Assessment 的语义要求

`coverage=not-covered` 允许研究价值不足的候选被 Reject；`covered` 要求决定性贡献被覆盖且剩余差异不构成独立贡献；AwR 要求未被覆盖的剩余贡献、已知阻碍及其正面价值。这些条件与新的三档 verdict 基本一致。`value-insufficient` 同时涵盖“低于 SA 但可 AwR”和“两项正面标准均不足”，需要由实际 verdict 和理由区分，不能把原因名直接映射为 Reject。

v2 在启用前还需补两项明确说明：

1. **原因类型数不等于 MAJOR 数。** `causes` 要求 code 唯一，但同一 design-invalid 可以包含两个独立失败条件。应要求在 Reason/Experiment/Estimand 分别说明这两项及所需证据，并按独立失败条件计 MAJOR；不能把同一 code 归并为一项。验证案例应包含两个独立设计问题共享一个 code、MAJOR=2、AwR，以及一个问题重复描述、MAJOR=1。
2. **精确引文验证仅核实出处。** 当前 parser 明确只校验文字存在性，无法验证科学蕴含关系。结构合法不等于科学判断正确。对 occupied 应核查被引结果确实覆盖候选的决定性主张；对 not-covered 应核查引用确实支持该具体差异。candidate 的自报贡献、通用机制引文、Query URL 或与主张无关的 supports 均不能单独承担这些结论。

第二项已有“Quotes establish provenance”和解释要求，方向正确；应保留并用独立语义案例验证，不能将 quote 匹配测试通过描述为裁判准确性通过。无需尝试用字符串规则替代科学判断。

## 其他入口一致性

- policy 的 `Feasibility depends only on ... Minimal Falsification Experiment` 与 bounded contract 的“minimal experiment and reasonable first-paper scope”措辞不同。建议统一为最小验证及其首篇资源依赖，避免廉价探针掩盖首篇必须的大规模训练。
- `roles/awr-judge.md` 首段仍写 unresolved evidence 一概 not-ready，后文允许一个 non-disqualifying MAJOR。应限定为尚未解决且阻碍 SA 的证据缺口，保留非否决项。
- research 与 AwR prior-work 均已写明 supports 来源也可能占用假设移除贡献，此项能够抵抗“两个支持引文就是创新”的填表攻击。
- 主评审、selector 和 AwR judge 均已说明未运行实验本身不扣级，且预测不能充当观测。主评审 contract 仍需修复迁移特例的无条件因果要求。
- `trigger.md` 与 `PROGRAM.md` 的未完成修改不属于本次复核结论。

## 必须保留的语义挑战

| 输入 | 期望约束 |
|---|---|
| 决定性贡献已被直接覆盖，实验完整，只有一项 MAJOR | Reject；不能以补实验或换数据集支持 AwR。 |
| 混杂与泛化切分错误独立存在，均属 design-invalid | 两项 MAJOR；禁止 SA，AwR 仍需价值与修订依据。 |
| 有实质适配、观察或推导支持，尚未运行实验 | 未运行本身不产生 MAJOR；按其他完整条件判定。 |
| 有价值的新认识，缺两项有限对照 | 可 AwR；两项缺口不自动成为 CRITICAL。 |
| 正确的非因果推导，无网络内部干预 | 按推导和适用假设评价，不能强求未声称的因果解释。 |
| 重要而意外的新诊断，无 repair | 允许按知识贡献评价；没有改进维度不机械否决 SA。 |
| 精确引用候选的“预计提升 30%”，没有观测或论证 | quote 合法仍不能支持该收益前景。 |
| 两条 supports 分别来自已经实现同一假设移除的工作 | 可行性获得支持，但新颖性仍可能被覆盖。 |

上述正例不要求无条件 SA；完整接收前提不足时仍可降级。科学语义测试应检查实际理由，而不能仅检查出现了某个标签。

## 前置筛选同步复核

roles/prescreen.md 的入口与 kill 条款、PROGRAM.md 和 trigger.md 已将单篇直接占用限定为完整候选的 decisive contribution，包含明示的剩余 adaptation、explanation、guarantee 或 payoff。输入、预算、输出和 host mechanics 未改变。

仍需同步 proposition-style 特殊条款中的旧 “headline finding is also occupied ... target paper admits it” 表述。该句应显式沿用完整 contribution 判断；目标论文承认已知现象并不自动回答候选提出的新解释检验。否则相邻条款对已知异常和新解释的区分仍有解释歧义。

本轮冻结 A/B 未调用 prescreen，因此该调整只能记录为规则文案与入口一致性修正，不能宣称已完成 prescreen 模型效果验证。

最终条款复核：proposition-style 现已显式沿用 complete-candidate test，且 target admission 只有回答包含明示剩余解释、保证或收益的决定性命题时才可触发 kill。与入口、一般 kill 条款及 PROGRAM/trigger 一致，此项没有剩余必须修复问题。该结论限于文案与入口一致性。

## 证伪范围通用检查复核

独立审阅 hash 为 3dd6c908f167efa5494d00672254d8a464501d657b78f86dd2324f893a1d9a30 的 contract。contract 与 policy 的新段落一致。区分资源／收益停止阈值与逻辑证伪、核查缺失 signature 是否为被排除命题的必要预测、限制正 signature 的排他解释，均是一般有效要求。该检查允许可核查推导或反例，不要求已完成实验或穷尽变体，未将 028 具体词汇写入规则。

冻结前有两处最小文字建议：将 “including a concrete … variant” 改成 “checking for … counterexample”，避免语法上要求每个候选必须存在一个变体；rubric 的 “positive signature must also address” 应与 contract 的 “positive signature alone does not eliminate” 同义，并复述 claim-relevant、supplied-material 范围，以免重新扩成非因果主张的泛因果控制义务。

原始60票科学报告已经完成，其结果与后续通用规则修订及针对性复验分开保留。

最终证伪条款复核：contract SHA-256 为 52c94b98fe9f09f407b255832d8fcdfb846e9f06dd29770d2ed4b60b1f33dffe。两处建议均已落实，contract/policy 段落程序核验逐字一致，rubric 同义并明确 supplied、claim-relevant 范围。没有剩余必须修复项，可以冻结用于既定针对性复验。该结论是规则内容检查，模型行为仍由单列 followup 评价。
