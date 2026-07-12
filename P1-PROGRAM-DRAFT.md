# P1 待人工过的 PROGRAM.md / schema 改动草案

P1「提高候选质量与 near-SA 转化」六条里,#2、#3、#5(near-SA 队列+lineage)已由 agent 落在 roles/hunt.sh(见 CHANGELOG 2026-07-12)。
下列三处必须改 `PROGRAM.md`(其头部声明"此文件及不动项只由人修改,agent 只读")或固定的 ledger schema,故只给草案,由人核后手工并入,并删本文件。

三处有依赖:**#4-schema 是 #6 的前置**(reject 要按类别决定能否复查,类别得能持久化落 ledger)。建议并入顺序:#4-schema → #6 → #1。

---

## #4-schema:ledger 加 `category` 列(非 SA 四分类机器可读)

**为何要改**:非 SA 四分类(novelty-dead / evidence-incomplete / design-fixable / ceiling-limited)现在只落 `tmp/nonsa-class.tsv`(agent 侧 `classify_nonsa` 已算),重启/跨运行不持久、也进不了 generate 的复活判定。要让 #6 按类别筛复活资格,类别必须进 ledger。

**改法**:7 列 → 8 列,末尾加 `category`。旧 7 列行缺此列,消费端按"未知"处理(与 overlap 旧行同规矩)。

`PROGRAM.md` §ledger.tsv,把

```
制表符分隔,7 列:

date	source	theme	idea	verdict	reason	overlap
```

改为

```
制表符分隔,8 列:

date	source	theme	idea	verdict	reason	overlap	category
```

并在字段说明末尾追加:

```
- category: novelty-dead | evidence-incomplete | design-fixable | ceiling-limited | -(SA 行或旧行留 -)
  非 SA 的四分类,由 orchestrator 按(降级前最低票、是否硬门槛降级、overlap)机械判定(见 hunt.sh classify_nonsa);
  复活资格的机械依据:novelty-dead 永久禁,其余留可审计复查条件(见 §不动项 6)。
```

**hunt.sh 侧配套**(schema 落地后 agent 可改):聚合处把 `$cat` 作第 8 列写进 ledger 行(现在只写 `tmp/nonsa-class.tsv`);SA 行写 `-`。`classify_nonsa` 已就绪,只差把结果接到 ledger 写行那句 `printf`。

---

## #6:复活软化——只 direct-hit / CRITICAL 永久禁,其余留可审计复查条件

**现状**:`PROGRAM.md` §不动项 6 末句"reject 行不得复活",generate.md 同款铁律——所有 reject 一刀切永久死。代价:全票 SA 被硬门槛降级的 evidence-incomplete(票够、只差补证)、以及 low-overlap 被可修 MAJOR 杀掉的 reject,都被永久封,near-SA 转化率上不去。

**改法**:`PROGRAM.md` §不动项 6,把

```
均按全新 idea 走完整查重与评审,不继承旧票;reject 行不得复活。
```

改为

```
均按全新 idea 走完整查重与评审,不继承旧票。reject 行的复活资格按 category 分:
category=novelty-dead(direct-hit 占位 / overlap=high / CRITICAL)进永久禁复活集合,任何形态都不得再选;
其余 reject(evidence-incomplete / design-fixable / ceiling-limited)保留一次可审计复查——
须在复活块首写「复活自:<原故事>」「复活条件:<上次死因 → 本次补了什么(补证 / 补强基线 / 修 estimand / 换归因对照)>」,
同一 story 累计复活至多一次(ledger 已出现该 story ≥2 次的不得再选);补后仍封顶则并入永久禁集。
```

**配套**:
- generate.md 对应铁律"reject 行不得复活"同步改成上面这套按 category 的规矩;进化/复查父本池扩到 `category≠novelty-dead` 的 reject 行(现仅 accept-w-rev)。
- `tmp/near-sa-queue.tsv` 的入队条件(hunt.sh)可放宽到 evidence-incomplete(现仅 design-fixable);evidence-incomplete 是"票够只差证据"的最高价值复查目标,现在因它 ledger verdict=reject 被挡在队外。
- 风险(需人工权衡):软化的是防重复/防打转的核心保证。要靠"同一 story 至多复活一次 + 补后仍封顶并入永久禁"兜住,别让它变成无限重试同一个死点子。

---

## #1:独立 selector——生成只发散,排序交独立进程

**现状**:generate.md 自己"先发散 10 个,再自筛 4-6 个"(`PROGRAM.md` §回路 step 1),自筛混在生成同一 context 里,违背角色分离精神;shortlist 排序另由 hunt.sh 的 `keep_rank`(复查/进化>删公理>低存量主题)机械定。

**改法**:生成只出发散全集(约 10 个,anti-template/主题反坍缩/删公理配额仍在生成侧约束),新增独立 `roles/select.md` 按四准则排序,排序结果替代 `keep_rank`。selector 是第 4 个独立 context(只做 triage、不出 verdict,不破坏生成/查重/打分三分离)。

**注意口径**:selector 在深查之前跑,拿不到 priorwork 证据,所以它的"novelty 证据"只能是**命题强度/头条自测**(能否逼出可证伪判别,非已验证的无人做过——那仍是 research+裁判的事)。四准则据此落成:

1. 命题强度:头条是命题式(逼出与近邻不同的可证伪预测)还是可枚举配对(近迁移封顶)。
2. clear-accept 上限:是否绑了可修复臂/惊人发现(纯 probe 上限 borderline)。
3. 最小否证实验:是否点名最强基线、给样本量/预期效应、信号能否归因到新颖成分。
4. 可执行性:单人 + 1×H100 80G 下 phase-1 能否跑完。

**`PROGRAM.md` §回路 step 1**,把"先发散 10 个候选,再自筛出 4-6 个差异最大的 idea"改为"发散约 10 个候选写入 `tmp/round/`(不自筛)";新增 step 1.4:

```
1.4 **排序**(roles/select.md,独立 context、便宜、只排不杀):按命题强度、clear-accept 上限、
    最小否证实验质量、可执行性四准则给发散全集打分排序,写 tmp/round/select.tsv
    (id 顺位 + 每准则一句依据)。只做 triage,不出 verdict、不查重、不背书;排序供 orchestrator 取深查名额。
```

step 9 一轮定义里"生成一批候选(4-6 个)经预筛裁剪"改为"生成一批候选(约 10 个)经排序+预筛裁剪"。

**新文件 `roles/select.md`**(草案要点,人核后落):
- 读:`tmp/round/ideas.all.md`、`brainstorming_policy.md`(校准四准则的尺度)。
- 铁律:只排序不淘汰(淘汰是预筛/裁判的事)、不查重、不打分、看不到生成方自评之外的东西、不写 ideas/ledger。
- 写:`tmp/round/select.tsv`,每行 `id<TAB>顺位<TAB>命题强度<TAB>ceiling<TAB>实验<TAB>可执行性`(后四列各一句)。

**hunt.sh 侧配套**:在 generate 与 prescreen 之间插 select 阶段;`select_shortlist` 的排序键从 `keep_rank` 换成 `select.tsv` 顺位(仍保留低存量主题作 tie-break、删公理/复查配额作硬约束)。select 缺失/非法则 fail-open 回落现有 `keep_rank`,不废轮。
