# 最终规则的针对性科学复验

最终规则在本次六票中识别了预定的具体问题，且没有将未运行实验或无 repair 方案作为独立扣级理由。028 的三票均发现 identity-blind 计数反例；025 的三票均区分项目收益阈值与形式命题，并将唯一 MAJOR 定位于完整状态存储缺少论证。

## 范围与分母

六个计划 cell 均完成且有效，全部原文已逐票阅读，SHA-256 与 receipt 一致。两个完整组均为 AwR，所有票均为 0 CRITICAL、1 MAJOR。原始 projection 与 aggregate.json 已程序核对。逐票记录见 [followup-effect-audit.json](followup-effect-audit.json)。

条件名为 `C-final-active-contract`；目录中的 `B` 仅沿用 runner 内部标签，不是原60票的冻结 B 条件。唯一 active contract 的 SHA-256 为 `52c94b98fe9f09f407b255832d8fcdfb846e9f06dd29770d2ed4b60b1f33dffe`。025、028 的四个输入文件与原 A/B 逐字节一致。

| 案例 | 原冻结 B 三席 | 本次 C 三席 | 本次主要理由 |
|---|---|---|---|
| 025 | AwR／SA／SA | AwR／AwR／AwR | pending outcomes 的总存储没有被 event identifiers 的界支持。 |
| 028 | SA／SA／SA | AwR／AwR／AwR | 缺少 count × identity 交互不能排除忽略 identity 的独立计数。 |

这是原 A/B 发现问题之后的针对性复验，共六次调用，没有替换原票。原 A/B 的结果和遗漏继续保留，见 [冻结 A/B 科学审查](../effect-review.md)。标签变化不直接表示准确率改善。

## 028：必要预测缺口

三席均明确指出：候选与 E5 都没有将身份标签敏感性设为 double-counting 的必要条件。一个忽略身份标签、为每个 retrieved copy 累积相同证据的模型，可以产生过度置信、减少 inspection 并降低成功率，而两种标签条件具有相同变化，因此交互为零。第三席给出 `L = L0 + n*l(o)` 的具体构造，使反例可直接检查。

三票均将错误排除规则计为一个 MAJOR，没有把 Experiment 与 Estimand 的重复描述计作两个问题。它们要求区分 identity-sensitive 与 identity-blind 假设，将交互缺失只用于排除必须预测该交互的假设，并利用已有位置、duplication 和 inspection 对比检验相应效应。第二席还要求保留与观测相容的 identity-blind 解释，而不是由该交互的 null 直接确立 recency。

这些完成条件沿用候选的实验臂、冻结模型与预算，保留解释研究的独立价值。三票均明确无需另加 repair，并指出 E1/E2 的总效应不能保证控制位置后的效应幅度。它们对修订后可以获得的结论作了限制，没有将干预可操作直接视为内部计数机制已经被证明。

三票的 `Assessment` 均以候选原始 kill 条款和 E5 的相关证据独立计数理论支持 `design-invalid`，引文与解释直接相关。此次理由比原 B 三张 SA 更完整地处理了已发现反例；这项局部观测不能证明对其他机制检验的一般可靠性。

## 025：停止阈值与形式主张

三席均承认 E5 支持 pending outcomes 与依赖结构联合处理的研究价值，E6 提供实际约束。唯一 MAJOR 是 `O(w log n)` event identifiers 未覆盖 confirmed／pending outcomes 及相关信息的完整存储；E1 的同步前提和 E2 的历史增长也未提供这一论证。三票都将其表述为缺少支持，而非证明算法不可能。

它们要求明确 dependency／feedback 假设、提供可核查的 sufficient-state 与存储分析，并将全部状态计入预算。第一、三席允许显式引入 pending-history 参数或收缩适用条件，前提是保留 E5 的关键交互与非平凡压缩。这是对现有贡献的具体修订要求，未要求先完成全部实验。

三票还正确区分五个百分点的项目取舍与 continuation equivalence。第一席用 unsafe rate 从 2% 降到 0% 的例子说明：满足 Pareto 改进仍可能不足五点；第三席指出 exact compact method 也可能与已安全的 E4 同表现。它们允许以五点作为收益停止阈值，但不允许据此否定更宽形式命题。此项澄清没有被额外计为 MAJOR。

有限枚举可以发现违反保证的反例，枚举通过不能代替一般论证。三票均保持这一正常区分，没有把“未运行”本身变成缺陷。`evidence-insufficient` 的引文对应完整状态存储这一具体缺口，没有泛化成对一切初步想法的完整证明要求。

## 可支持的结论

本次六票均给出独立价值、具体 blocker 和有限完成条件，没有凭可修复性自动接收，也没有因为原实验尚未运行或缺少 repair 就拒绝。025 聚合为 `design-fixable`，028 为 `ceiling-limited`；两组均无支持 SA 的票，因此首次自动再入资格为 false。此处只核对首次提交谓词，没有实际执行数据库再提交或 AwR sidecar。

该检查由同一案例发现的错误触发，只有两个合成条件例、同一模型各三席，没有独立留出集或人类真值。它支持“通用条款在本次针对性复验中被正确应用”，不能支持真实误拒率下降、全部科学判断正确或未来稳定保证。025 的算法和存储分析仍待研究，028 的实际机制仍未通过实验建立；其 AwR 只表示在假设材料下保留研究价值。
