# PROGRAM — idea 调研回路协议

所有入口(定时 routine、主动 loop)共用。此文件及下列不动项只由人修改,agent 只读。

## 不动项

1. `rubric.md` — 评审流程,只读,不得跳步或重新解释。
2. `brainstorming_policy.md` — 发散规则、idea 形态、verdict 校准,只读;Strong Accept 的尺度不得自行放宽或收紧。
3. 指标唯一:verdict。keep ⇔ Strong Accept。verdict 不由任何单个 agent 决定,而由 orchestrator(`hunt.sh`)对 N 位独立裁判取**最低**票聚合;Strong Accept 需全票。
4. 定级证据:候选 Strong Accept 必须附 policy 要求的定向查重记录(最相近 3-5 篇 + 链接,含 ≥1 条可复现的 arXiv/Semantic Scholar API 检索记录)与「最小否证实验」(数据 × 算力 × 预期信号);缺任一不得定级。查重由独立进程完成,裁判的 novelty 只认该证据、feasibility 只认最小否证实验,不认生成方的说法。
5. `ledger.tsv` schema 固定(见下);每个生成的 idea 无论 verdict 一律记一行,只追加,不改历史行。**只由 orchestrator 写入**,agent 不碰。
6. 生成前必读 `ledger.tsv`,新 idea 不得与任何已有行实质雷同(包括已拒的)。唯一例外:对 accept-w-rev 行的定向进化(每轮至多 1 个,点名修复的缺陷,按全新 idea 走完整查重与评审,不继承旧票);reject 行不得进化复活。
7. 角色分离(反串通):生成 / 查重 / 打分是互不共享 context 的独立进程,prompt 见 `roles/`。生成方不查重定性、不打分;裁判默认 Reject、互不通气、也不知道停机条件。
8. 写入范围:agent 只允许写 `tmp/`(草稿区,gitignored)与 `ideas/`(仅报告角色);不得碰 `ledger.tsv` 及其它文件,不得运行 git/gh/publish。记账与发布由 orchestrator 负责,产出经 `./publish.sh` 走特性分支 + PR。
9. 一轮 = 一批最终候选 idea(4-6 个)完整走完"生成 → 查重 → 打分 → 记账",不得半途丢弃已写入本轮产物的 idea。
10. 循环期间不停下询问人、不请求确认;停机条件只由入口文件定义,未达标不得提前放水结束。

## 回路

`hunt.sh` 按序调起独立进程,每轮:

0. **死因蒸馏**(`roles/meta.md`,每 META_EVERY 轮、拒行足量时,可错):把 ledger 拒因归纳成 `tmp/deathlist.md`,失败只记日志不阻塞。
1. **生成**(`roles/generate.md`):读 policy、ledger 与死因清单,先发散 10 个候选,再自筛出 4-6 个差异最大的 idea 到 `tmp/round/`,遵守 policy 发散要求、主题反坍缩与四种合法形态,避开 ledger 已有行、死因模式和已饱和套路;每个 idea 标注主题并附最小否证实验;发散透镜由 `hunt.sh` 随机抽取注入。
2. **查重**(`roles/research.md`):对抗式定向查重,每个 idea 找最相近 3-5 篇实读摘要/方法,产出独立证据;每个 idea 块须有至少 3 条带链接近邻与至少 1 条 API 检索记录(query URL)。
3. **打分**(`roles/review.md`,跑 N 次):各裁判按 `rubric.md` 完整评审、用 policy 校准,默认 Reject,输出各自 verdict。
4. **聚合记账**(orchestrator):每个 idea 取 N 位裁判最低票(SA 需全票),全部追加进 `ledger.tsv`。
5. 有全票 Strong Accept → `roles/report.md` 组装报告到 `ideas/`,orchestrator 调 `./publish.sh` 发布,结束;否则下一轮(调研可增量补充)。

## ledger.tsv

制表符分隔,6 列:

```
date	source	theme	idea	verdict	reason
```

- date: YYYY-MM-DD
- source: weekly | hunt
- theme: policy 主题词表之一(跨轮反坍缩的统计依据)
- idea: 一句话故事
- verdict: strong-accept | accept-w-rev | reject
- reason: 一句话(拒因,或 keep 的核心价值)
