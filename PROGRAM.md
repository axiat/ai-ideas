# PROGRAM — idea 调研回路协议

所有入口(定时 routine、主动 loop)共用。此文件及下列不动项只由人修改,agent 只读。

## 不动项

1. `rubric.md` — 评审流程,只读,不得跳步或重新解释。
2. `brainstorming_policy.md` — 发散规则、idea 形态、verdict 校准,只读;Strong Accept 的尺度不得自行放宽或收紧。
3. 指标唯一:verdict。keep ⇔ Strong Accept。
4. 定级证据:候选 Strong Accept 必须附 policy 要求的定向查重记录(最相近 3-5 篇 + 链接);无记录不得定级。
5. `ledger.tsv` schema 固定(见下);每个生成的 idea 无论 verdict 一律记一行,只追加,不改历史行。
6. 生成前必读 `ledger.tsv`,新 idea 不得与任何已有行实质雷同(包括已拒的)。
7. 写入范围:agent 只允许写 `ideas/` 与 `ledger.tsv`,不得改动其它文件。
8. 一轮 = 一批 idea(4-6 个)完整走完"生成 → 评审 → 记账",不得半途丢弃未评审的 idea。
9. 循环期间不停下询问人、不请求确认;停机条件只由入口文件定义,未达标不得提前放水结束。

## 回路

LOOP:
1. 读 `brainstorming_policy.md`、`rubric.md`、`ledger.tsv`(`research_context.md` 可选灵感,不构成约束)。
2. 文献调研:范围与时间窗由入口文件定义,每篇记标题、arXiv 链接、核心方法、关键结果、局限。
3. 综合分析:趋势、方法分歧、未解决的 gap、隐藏假设。
4. 生成一批 idea,遵守 policy 的发散要求与四种合法形态,避开 ledger 已有行。
5. 按 `rubric.md` 完整评审,verdict 用 policy 校准。
6. 记账:每个 idea 一行追加进 `ledger.tsv`。
7. 达到入口定义的达标条件 → 按入口的输出格式写报告,结束;否则回到 4(调研可增量补充)。

## ledger.tsv

制表符分隔,5 列:

```
date	source	idea	verdict	reason
```

- date: YYYY-MM-DD
- source: weekly | hunt
- idea: 一句话故事
- verdict: strong-accept | accept-w-rev | reject
- reason: 一句话(拒因,或 keep 的核心价值)
