# Task 2 执行记录 — `DONE_WITH_CONCERNS`

## 结论

配对 synthetic fixture、两个 root-level `expect` 与 `calib/README.md` 说明已就位。机械隔离约束通过：两份 `ideas.md` byte-identical，两份 `priorwork.md` 只有末行 synthetic occupancy 事实不同，两个 `expect` 均精确为 `probe\n`。`run_all.sh` 对 `probe` 的已有逻辑明确不将其计入 calibration accuracy 分母。

live synthetic RED **未复现**。删除 `priorwork.md` 中越过 research-role 边界的“可归因净增量”评审结论后，唯一一次 clean old-policy run 为 `AwR/AwR/AwR`，SA 票数为 0，RED 断言 exit 1。该 fixture 不得作为 policy regression 已证明的证据；本任务未修改 Task 1 policy，也未继续抽样追求 SA。

唯一权威 RED 证据仍是冻结的 2026-07-17 I3 归档：

`/Users/qinningxu/.ai-ideas-runs/ai-ideas/20260717T190135-p28520-r14/round/`

其 `rev/1..3/verdict.tsv` 中 I3 均为 `strong-accept`。该归档证明旧 policy 对原 I3 的放行，不替代本次 paired fixture 未复现 RED 的事实。

## Fixture 事实

- 头条限定为“matched 视频预训练中 action conditioning 是否承重”，含 scratch 灵敏度正控、非饱和 suite、分层 bootstrap 95% CI、等价带与预注册中间带。
- 唯一 payoff baseline 是同 AMPLIFY architecture、LIBERO 五 suite 与 success-rate metric 下的 AMPLIFY-50 full-data inverse-policy 配置。repair 的 target-task action-label 总预算是每任务 `2+3=5`，pilot、选择和调参所见 labels 全部计入。在预注册 `-0.05` 容差内 matched AMPLIFY-50 才记 10× action-label reduction。
- `priorwork.md` 记录 AMPLIFY Table 3 的 50-demo full-data comparator，以及 Figure 4/Table 16 的 2/5/10-demo few-shot setting；Table 3 的对应 full-data 结果高于 Table 16 的 5-demo 结果。headline `overlap=low` 在两案中相同。
- occupied 末行只陈述最近实践已实现同一 `2+3` rule、5-label 口径、matched AMPLIFY-50 与 10× payoff；incremental 末行只陈述同检索边界内无该 rule/payoff。两者都标为 synthetic policy input，未代替外部文献事实。

## RED 证据

命令：

```bash
rtk ./calib/run_panel.sh calib/cases/probe-diagnostic-occupied-payload 3
rtk awk -F '\t' '$2~/(^|,)strong-accept(,|$)/{ok=1} END{exit !(NR==1 && ok)}' \
  tmp/calib/probe-diagnostic-occupied-payload/aggregate.tsv
```

执行记录：

1. 首次 backend 调用三席均报 `API Error: Stream idle timeout - no chunks received`，`run_panel.sh` exit 2；无票据，不计入 RED。
2. backend retry 产生 `AwR/AwR/AwR`，但当时 occupied `priorwork.md` 末句直接写入“可归因净增量为零”的 reviewer 结论，越过 research-role 边界；该轮不作为 clean RED。
3. 同步删除两份 `priorwork.md` 的评审结论，完整保留 same rule / 5 labels / matched AMPLIFY-50 / 10× payoff 原始事实。唯一 clean run 产生：

```text
I1  accept-w-rev,accept-w-rev,accept-w-rev  accept-w-rev
clean_red_assert_rc=1
```

清理旧文本后曾启动 incremental 预跑，在任何 verdict 产出前终止（exit 130）；该轮不是证据。clean RED 失败后未再调用 panel。

## 逐席理由审计

| Seat | Vote | MAJOR | 重复 payoff 理由 | 其它缺陷 |
|---|---|---:|---|---|
| 1 | `accept-w-rev` | 2 | 同检索边界、完全相同 `2+3` acquisition、5-demo 口径、`-0.05` 容差与 AMPLIFY-50 匹配已实现，10× payoff 无法再作贡献 | 3 seed 下 `±0.03` 等价 CI 功效未证，计 1 MAJOR |
| 2 | `accept-w-rev` | 2 | 同 rule、同 5-label cap、同 matched comparator/payoff 已占，repair 只是复现 | 3 seed 等价检验功效存疑，计 1 MAJOR |
| 3 | `accept-w-rev` | 1 | 同边界、同 rule、同 5-demo cap、同 AMPLIFY-50/容差/净 10× reduction，payoff 被完整占据 | 无独立 MAJOR |

三席都明确以“已占 payload 不得重复记账”封住 SA；两席同时指出独立的 equivalence-test power 缺陷。按“唯一 clean RED 仍 0 SA 则停止”的边界，没有继续修改 seed/CI 设计或再抽样。

## 机械校验

```bash
rtk cmp calib/cases/probe-diagnostic-occupied-payload/ideas.md \
  calib/cases/probe-diagnostic-incremental-payload/ideas.md
# exit 0

rtk proxy git diff --no-index --numstat \
  calib/cases/probe-diagnostic-occupied-payload/priorwork.md \
  calib/cases/probe-diagnostic-incremental-payload/priorwork.md
# 1  1  .../priorwork.md ; exit 1（预期有一行差异）

for f in calib/cases/probe-diagnostic-{occupied,incremental}-payload/expect; do
  rtk wc -c < "$f"
  rtk od -An -tx1 "$f"
done
# 两者均 6 bytes: 70 72 6f 62 65 0a = "probe\n"

rtk proxy find calib/cases/probe-diagnostic-occupied-payload \
  calib/cases/probe-diagnostic-incremental-payload -type f -print
# 恰好六个文件；无嵌套误路径

rtk rg -n -C 4 'probe|accuracy|expect' calib/run_all.sh
# run_all.sh: probe 记为 grade=probe；graded=pass+fail，probe 不进准确率分母

rtk git diff --check
# exit 0
```

`run_all.sh` paired probe 与全量 formal calibration 未运行：clean RED 未建立，本任务不授权修改 Task 1 policy，继续调 panel 只会构成追票。因此不声称 paired green、不声称本 pair 已保护 policy regression，也不声称本轮实测了 formal calibration accuracy。

## 范围与自审

- 未修改 `brainstorming_policy.md`、`hunt.sh`、reviewer 聚合、AwR sidecar、generator、selector、prescreen 或 archive path。
- 未修改 `ledger.tsv`；未 push、publish、merge。
- Task 2 产物仅为两组 fixture、`calib/README.md` 的 probe 边界说明与本执行记录。
- 实现完整性：fixture 结构和 synthetic/probe 计分边界完成。
- 验收缺口：live RED 未复现；clean occupied run 已经是三席 AwR，两席还指出独立功效 MAJOR。fixture 可保留为非计分 synthetic probe，不能承担“旧 policy 会重复记账”的现场回归证明。
- Commit subject: `calib: add paired synthetic payload probes`（Task 2 files only，no push）。
