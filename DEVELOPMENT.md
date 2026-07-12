# A 类 SA 命中率优化计划

状态：开发规划（持续维护）  
更新：2026-07-12

本地候选池：`tmp/sa-potential-ideas.md`（动态候选，未获正式主环确认，不纳入版本控制）

目标：提高候选进入正式评审后获得全票 Strong Accept 的概率，同时保持 direct-hit 阴性样本的拒绝能力。

## 基线

- `ledger.tsv`：209 个 idea，155 AwR，54 Reject，0 SA。
- `tmp/hunt.metrics.tsv`：96 个候选完成三席评审，288 张票中仅 6 张 SA；3 个候选得到 `2×SA + 1×AwR`，无全票 SA。
- 59 次有指标记录的尝试中，32 次到达 verdict，27 次停在 empty/fail。
- AwR sidecar 已产出大量 `SA-可能`，但不保存通过理由，也不回灌主环，不能计作正式命中。

## 成功率拆分

| 指标 | 定义 | 当前信号 |
|---|---|---|
| 校准正确率 | gold positive / negative 能否被稳定区分 | 普通 oral 阳性仍无法全票通过 |
| 候选质量率 | 正式候选中至少获得 1 张 SA 票的比例 | 3 / 96 |
| near-SA 转化率 | `2,2,1` 或 `1,2,2` 经修订后转为全票 SA 的比例 | 尚无转化链路 |
| 最终 SA 命中率 | 正式候选中全票 SA 的比例 | 0 / 96 |
| 运行完成率 | 尝试中到达 verdict 的比例 | 32 / 59 |

## P0：统一判定与校准

- [ ] 钉死唯一 SA 定义。建议以 `brainstorming_policy.md` 的 clear-accept 标准为准：约 `6,6,8`；oral/spotlight 是加分项，不是硬门。
- [ ] 删除 `rubric.md` 中“两项 8+”、`8+6` 正例、两 MAJOR 处理等互相冲突的规则，只保留一份可执行 verdict 表。
- [ ] 同步 `brainstorming_policy.md`、`rubric.md`、`roles/review.md`、`roles/awr-judge.md`、`trigger.md`，禁止不同入口自行加严或放宽。
- [ ] 建立具身领域 gold set：普通 method、benchmark/new problem、删承重假设、direct-hit 阴性各有代表。
- [ ] 校准拆成两层：冻结 `ideas + priorwork` 的裁判校准只测 verdict 尺度；允许真实检索的端到端校准单独测近邻召回和 overlap 判断。
- [ ] 在 gold set 能稳定区分阳性和阴性前，不调整 `min-vote` 聚合，也不以新增轮数评估改动。

验收：已知 A 类阳性出现可复现的全票通过；direct-hit 阴性保持全票 Reject；同一 case 重跑时理由落在同一 gate。

## P0：保留完整观测

- [ ] 每次运行生成稳定 `run_id` 和 `candidate_id`，记录 story、来源、backend、prompt/policy 版本、阶段时间和退出原因。
- [ ] 按运行归档 `ideas`、`priorwork`、三席 `verdict.tsv`、三席 `review.md` 与聚合结果，不再由下一轮覆盖。
- [ ] 保存完整票向量与每席理由；ledger 的最低票理由只作摘要，不再是唯一学习信号。
- [ ] 单独记录检索基础设施失败，避免 CAPTCHA、限流、编号未核实被永久写成 idea 质量结论。

验收：任一 ledger 行可还原当轮输入、三席评审和聚合过程；三个历史 near-SA 的同类案例不再丢失正面两票依据。

## P1：提高候选质量

- [ ] 生成仍负责发散；新增独立 selector，按 novelty 证据、clear-accept 上限、最小否证实验、可执行性排序。
- [ ] 主题稀缺度只作同质量候选的 tie-break，不再优先于质量，也不因主题不均匀废整轮。
- [ ] shortlist 优先保留 selector 的高上限候选；删承重假设、进化标签不自动获得更高排位。
- [ ] `roles/research.md` 只报告 prior work 覆盖事实，删除“差异是否足以 clear accept”的预判；三位 reviewer 独立作上限判断。
- [ ] direct-hit、medium-overlap、检索不完整分别处理；检索不完整先补查，不进入正式定级。

验收：正式评审中 high-overlap/direct-hit 占比下降；至少一张 SA 票的候选比例上升；主题分布不再是主要优化结果。

## P1：从失败中提纯

- [ ] 将非 SA 分成四类：`novelty-dead`、`evidence-incomplete`、`design-fixable`、`ceiling-limited`。
- [ ] 只有 direct-hit / CRITICAL 进入永久禁复活集合；检索失败、实验缺口和单席分歧保留复查资格。
- [ ] 保存 revision lineage 与明确 delta，允许在原问题上形成新机制，不靠改写故事绕过去重。
- [ ] `2,2,1` / `1,2,2` 候选优先于继续盲目扩池；near-SA 队列未处理完时，至少保留一个 shortlist 名额用于修订稿。

验收：每个 near-SA 都有补证、修订、重评或判死的终态；新增运行能利用正面票据，而非只扩大 deathlist。

## P1：重建 AwR 复活链路

- [ ] 入队顺序改为：主环 near-SA → low-overlap 且 design-fixable → evidence-incomplete；medium/high/unknown novelty 封顶项不自动复活。
- [ ] 研究席可用便宜模型；裁判席使用独立可信模型，且通过时必须逐项写出 novelty、feasibility、clear-accept gate 的证据。
- [ ] `SA-可能` 只表示进入主环复审的资格；修订稿必须重新走完整 priorwork 和三席评审。
- [ ] 保存 backend、模型、policy 版本；policy 更新后，旧终态不得直接沿用。
- [ ] 第三轮反馈后的最新版必须再评一次，不能以“未达标”收尾却留下未审终稿。

验收：每个 sidecar 通过项都有可审计理由和正式主环 verdict；不再出现原始 high-overlap、2 MAJOR 候选被单席两行输出放行。

## P2：减少无效运行

- [ ] 对 generate/research/review 分别统计结构失败、API/网络失败、模型失败和内容失败。
- [ ] 每个候选记录主 blocker code、次要 blocker、三席正面理由、模型与规则版本；不再只从 prose log 反推漏斗。
- [ ] 单候选 research 失败只重试该候选，避免整批重跑；前段合格产物继续支持安全 resume。
- [ ] 保存超出 `SHORT_MAX` 的 keep 候选及 selector 分数，后续可重排，不再静默丢弃。
- [ ] 将运行完成率与 SA 命中率分开汇报；empty/fail 的下降不等于 idea 质量提升。
- [ ] 每个配置 epoch 只改一个变量，在固定校准集和固定正式评审预算下比较；`SA-可能` 数量不作优化指标。

验收：运行完成率提高；检索或格式故障不再产生永久 ledger 结论；同等正式评审数量下调用成本下降。

## 实施顺序

1. P0 判定口径与 gold calibration。
2. P0 运行归档与完整票据。
3. P1 near-SA 优先队列、selector 与 priorwork 去裁决化。
4. P1 AwR 过滤、可信复审与主环回灌。
5. P2 运行效率优化。

在 1–4 完成前，继续扩大 ledger 的收益很低。
