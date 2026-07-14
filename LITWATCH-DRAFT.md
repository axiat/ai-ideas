# 领域近作监视(litwatch)— 设计草案

结论:用免费 agy 额度跑一个**独立常驻进程**,把领域近作预取成本地缓存,供 hunt 主循环的查重阶段当近邻种子。agy 不可靠,所以它绝不进主循环关键路径;它触及的每条记录都要机械验真才准入缓存。方向锁定为**宽口径领域近作监视**(照 `research_context.md` 主题铺开),不做按 idea 的定向查重。

## 为什么这条路非侵入

`hunt.sh` 的 FRONT / BACK / REV 席位都要让 agy 站进回路,主循环就得等它、被它的早停和登录验证拖时。litwatch 在回路之外,主循环对它只有一个只读依赖:缓存文件在不在。agy 挂掉 = 缓存不刷新,主循环照跑,行为与今天逐字节一致。这是整条路成立的全部前提。

对应偏好:agy 只囤证据、不出判断——不判 overlap、不判 novelty、不排 idea。这些留给主循环的可信 research 与裁判,和 PROGRAM 第 4/7 条一致(查重独立完成,裁判只认证据、不认生成方说法)。

## 组件

### 1. 取数 — 确定性 curl,不经 agy

主循环的查重本来就用 WebFetch 打 arXiv API(`http://export.arxiv.org/api/query?search_query=...`)和 Semantic Scholar(`https://api.semanticscholar.org/graph/v1/paper/search?query=...`)。litwatch 取数就是一个 curl 脚本打这两个**同样的端点**,按主题拉近 N 天近作。

结果直接来自 API,ID / 标题 / 摘要都是真的,**结构上不可能幻觉**。取数这一步一个字都不过 agy。

### 2. agy 的活 — 判断性,但错了也无害

agy 一律经 `agy-worker.sh` 调起(继承其冷却闸,见组件 5),只干两件事,都在本地已取到的真实文本上做:

- **拟检索词**:读 `research_context.md` 主题 + 近期 `ledger.tsv` 死因主题(idea 反复死在哪个方向),给出一组多样化 query。错了 = query 不好、召回差,下游自然丢弃。
- **相关性标注**:读已取到的摘要,按主题标出"最像近邻风险"的若干篇,各写一行为什么。错了 = 标歪,主循环 research 会自己重读重判。

硬约束:agy 输出**只允许引用已取到的 ID**。引用了不在取数集里的 ID,写入器直接忽略——agy 结构上塞不进假论文。

### 3. 验真 — 确定性准入闸

缓存不变量:**每条记录都带一个能被 curl 解析命中的 arXiv / S2 ID**。写入前逐条 re-resolve(打开链接核对标题),解析不了的丢弃并记日志。这与 `research.md` 现有的"编号自查、防幻觉、实际打开链接核对标题"是同一套要求,只是移到缓存构建时做一次。agy 的标注另做集合校验(引用 ID 必须在取数集内);注释文本本身不被信任(research 会重读)。

### 4. 缓存 — 耐用产物

`tmp/litwatch/index.jsonl`(gitignored,符合"agent 只写 `tmp/`")。按主题分组的 `{id, title, abstract, url, date, theme, agy_note}`。紧凑,可被 WebFetch / 读文件直接消费。

### 5. 冷却调起

litwatch 调 agy 一律走 `agy-worker.sh`,直接继承它的 mkdir 锁 + 时间戳 + `AGY_LAUNCH_GAP_SEC`。于是 litwatch 与任何 `hunt.sh` 的 agy 用法**共用同一个全局启动闸**:两边都不会把 agy 登录打爆,litwatch 也被限速不抢跑。代价:litwatch 的 agy 启动可能让 `hunt.sh` 的 agy 席(若启用)等最多 gap 秒;但默认主流程裁判是 claude,不启用 agy 席时零接触。

### 6. 节奏

cron / schedule routine 每 N 小时跑一次 `litwatch.sh`,尽力而为。agy 失败 = 本轮不刷新缓存;笔记本休眠不跑也无所谓,缓存旧了主循环照样冷启动补齐。

## 三条约束逐条兑现

1. **结果可被主流程用**:缓存打的就是 `research.md` 已经在打的同一批 arXiv / S2 端点。`research.md` 加**一段**:缓存在则先看本主题缓存条目当近邻种子,但 PROGRAM 的 ≥1 条 live API 记录、≥5 条实读近邻、结构门槛一条不减;缓存缺则行为不变。
2. **验 agy 产出正确性**:取数不经 agy(结构上无幻觉);agy 只能引用已取真实 ID,越界引用被忽略;准入前逐条 re-resolve,复用现有"编号自查"。
3. **冷却调起**:走 `agy-worker.sh`,继承 mkdir 锁 + gap 闸,与主流程 agy 用法共用一个全局限速。

## 不做(YAGNI)

- 不做按 idea 的定向查重(那要进循环)。
- 不建 embedding / 向量库(近作监视用不上;主循环靠实读判定,不靠相似度)。
- agy 不出任何 verdict / overlap / novelty 结论。
- 不改 `ledger.tsv`、不碰 publish、不进 `hunt.sh` 主回路。

## 待确认(建实现前一次性验掉)

- **S2 API 免费限流**:确认 curl 侧限速 / 退避够用(pipeline 已有 `sleep 10` + 换另一家 API 的先例)。
- **agy 稳定产结构化标注**:一次 dry-run 验 agy 能读 `--add-dir` 本地文件并稳定输出可解析的标注。拟检索词与标注两步都不需要 agy 联网,故即使 agy 无 web 工具此路仍成立。

## 治理

组件 1 落地会新增 `litwatch.sh` + `roles/litwatch.md`(或等价 prompt);组件 3 会给 `roles/research.md` 加一段消费契约。后者属 `roles/` 协议改动,须记 CHANGELOG 并走分支 + PR 留审,不自动合。
