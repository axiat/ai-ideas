# PROGRAM — idea 调研回路协议

所有入口(定时 routine、主动 loop)共用。此文件及下列不动项只由人修改,agent 只读。

## 不动项

1. `rubric.md` — 评审流程,只读,不得跳步或重新解释。
2. `brainstorming_policy.md` — 发散规则、idea 形态、verdict 校准,只读;Strong Accept 的尺度不得自行放宽或收紧。
3. 指标唯一:verdict。keep ⇔ Strong Accept。verdict 不由任何单个 agent 决定,而由 orchestrator(`hunt.sh`)对 N 位独立裁判取**最低**票聚合;Strong Accept 需全票。
4. 定级证据:候选 Strong Accept 必须附 policy 要求的定向查重记录(最相近 5-8 篇 + 链接,含「最强反例」行与 ≥1 条可复现的 arXiv/Semantic Scholar API 检索记录)与「最小否证实验」(数据 × 算力 × 预期信号);删承重假设形态另须「裂缝证据核验」节且 ≥2 条相符;缺任一不得定级。查重由独立进程完成,裁判的 novelty 只认该证据、feasibility 只认最小否证实验,不认生成方的说法。
5. `ledger.tsv` schema 固定(见下);每个生成的 idea 无论 verdict 一律记一行,只追加,不改历史行。**只由 orchestrator 写入**,agent 不碰。
6. 生成前必读 `ledger.tsv`,新 idea 不得与任何已有行实质雷同(包括已拒的)。唯一例外(每轮至多 1 个,进化与复查共用名额):进化——只准选 verdict=accept-w-rev 且 overlap=low 且死因属实验设计类缺陷的行做定向修复(novelty 封顶/已被占据的行不得进化);复查——查重薄弱型 accept-w-rev 行原样重交补查重,同一 story 至多一次。均按全新 idea 走完整查重与评审,不继承旧票;reject 行不得复活。
7. 角色分离(反串通):生成 / 查重 / 打分是互不共享 context 的独立进程,prompt 见 `roles/`。生成方不查重定性、不打分;裁判默认 Reject、互不通气、也不知道停机条件。
8. 写入范围:agent 只允许写 `tmp/`(草稿区,gitignored)与 `ideas/`(仅报告角色);不得碰 `ledger.tsv` 及其它文件,不得运行 git/gh/publish。记账与发布由 orchestrator 负责,产出经 `./publish.sh` 走特性分支 + PR。
9. 一轮 = 生成一批候选(4-6 个)经预筛裁剪后完整走完"深查 → 打分 → 记账"。预筛 kill 的候选按 reject 记账(不得静默丢弃);预筛存活超出 SHORT_MAX 的截断候选不深查、不记账(未获任何评审,下轮可重新生成),此外不得半途丢弃已写入本轮产物的 idea。
10. 循环期间不停下询问人、不请求确认;停机条件只由入口文件定义,未达标不得提前放水结束。

## 回路

`hunt.sh` 按序调起独立进程,每轮:

0. **失败蒸馏**(`roles/meta.md`,每 META_EVERY 轮、reject+accept-w-rev 行足量时,可错):把拒因与封顶原因归纳成 `tmp/deathlist.md`(致命模式/封顶模式/进化候选),失败只记日志不阻塞。
1. **生成**(`roles/generate.md`):读 policy、ledger 与失败清单,先发散 10 个候选,再自筛出 4-6 个差异最大的 idea 到 `tmp/round/`,遵守 policy 发散要求、主题反坍缩与五种合法形态(删承重假设形态带结构化字段与「删公理尝试」标记),避开 ledger 已有行、死因模式和已饱和套路;每个 idea 标注主题并附最小否证实验(点名最强基线、给样本量与预期效应);发散透镜由 `hunt.sh` 随机抽取注入。
1.5 **预筛**(`roles/prescreen.md`,便宜可错、只杀不保):只杀"单篇工作直接占据头条"的 direct hit,kill 必附占位链接;被杀者由 orchestrator 立即按 reject 记账(overlap=high),存活取前 SHORT_MAX 个进深查。keep 不构成任何 novelty 结论。
2. **深查重**(`roles/research.md`):对抗式定向查重,先 direct-hit 猎杀再铺开三类检索词;每个 idea 找最相近 5-8 篇实读摘要/方法,产出独立证据(含"最强反例"行);每个 idea 块须有至少 5 条带链接近邻与至少 1 条 API 检索记录(query URL);形态=删承重假设的 idea 另须逐条实读核验其自报「裂缝证据」URL(相符/部分/不符/不可达),写入该块「裂缝证据核验」节。
3. **打分**(`roles/review.md`,跑 N 次):各裁判按 `rubric.md` 完整评审、用 policy 校准,默认 Reject,输出各自 verdict。
4. **聚合记账**(orchestrator):每个 idea 取 N 位裁判最低票(SA 需全票),连同查重的重叠判定(overlap 列)与非 SA 四分类(category 列)全部追加进 `ledger.tsv`。
5. 有全票 Strong Accept → `roles/report.md` 组装报告到 `ideas/`,orchestrator 调 `./publish.sh` 发布;当日累计达入口定义的目标数(`SA_TARGET`,默认 1)即停,未达则继续下一轮(调研可增量补充)。

## ledger.tsv

制表符分隔,8 列:

```
date	source	theme	idea	verdict	reason	overlap	category
```

- date: YYYY-MM-DD
- source: weekly | hunt
- theme: policy 主题词表之一(跨轮反坍缩的统计依据)
- idea: 一句话故事
- verdict: strong-accept | accept-w-rev | reject
- reason: 一句话(拒因,或 keep 的核心价值);预筛杀的以"预筛直接占位:"开头
- overlap: high | medium | low | 未知(查重的重叠判定;进化父本资格的机械筛选依据;旧行缺此列视为未知)
- category: novelty-dead | evidence-incomplete | design-fixable | ceiling-limited | -(SA 行与旧行留 `-`)
  非 SA 的四分类,由 orchestrator 按(降级前最低票、是否硬门槛降级、overlap)机械判定(见 hunt.sh `classify_nonsa`):
  novelty-dead=头条被占据(overlap=high)/CRITICAL;evidence-incomplete=全票 SA 被硬门槛降级(票够只差证据);
  design-fixable=accept-w-rev 且 overlap=low(实验设计类可修);ceiling-limited=accept-w-rev 但被近邻封顶(overlap≠low)。
