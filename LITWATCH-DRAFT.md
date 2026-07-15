# 领域近作监视(litwatch)— 设计草案

结论:用免费 agy 额度跑一个**独立常驻进程**,把领域近作预取成本地缓存,供 hunt 主循环的查重阶段当近邻种子。agy 不可靠,所以它绝不进主循环关键路径;它触及的每条记录都要机械验真才准入缓存。方向锁定为**宽口径领域近作监视**(照 `research_context.md` 主题铺开),不做按 idea 的定向查重。

## 为什么这条路非侵入

`hunt.sh` 的 FRONT / BACK / REV 席位都要让 agy 站进回路,主循环就得等它、被它的早停和登录验证拖时。litwatch 在回路之外,主循环对它只有一个只读依赖:缓存文件在不在。agy 挂掉 = 缓存不刷新,主循环照跑,行为与今天逐字节一致。这是整条路成立的全部前提。

对应偏好:agy 只囤证据、不出判断——不判 overlap、不判 novelty、不排 idea。这些留给主循环的可信 research 与裁判,和 PROGRAM 第 4/7 条一致(查重独立完成,裁判只认证据、不认生成方说法)。

## 组件

### 1. 取数 — 确定性脚本,不经 agy

默认走 arXiv **OAI-PMH**(`https://oaipmh.arxiv.org/oai`,批量元数据抓取端点,不像 search API 那样限流):按 set(默认 cs)+ 近 N 天批量抓,本地按类别白名单(cs.RO/cs.LG/cs.AI/cs.CV/cs.CL/stat.ML)+ 主题关键词过滤打标(`lib/litwatch.py harvest`,python 标准库,零依赖)。真机:2600 篇 cs → 过滤到 ~83 篇相关近作,主题标注正确。

另留两条 per-query 取数口(`LITWATCH_SOURCES` 切换):S2(需 `LITWATCH_S2_KEY`,相关性更强)与 arXiv search API(`_arxiv_search_query`,已知限流+噪声,不推荐)。三条口结果都直接来自 API,ID/标题/摘要都是真的,**结构上不可能幻觉**;取数这一步一个字都不过 agy。

### 2. agy 的活 — 判断性,但错了也无害

agy 一律经 `agy-worker.sh` 调起(继承其冷却闸,见组件 5),读本地已取到的真实文本做**相关性标注**:按主题标出"最像近邻风险"的若干篇,各写一行为什么(`roles/litwatch.md`)。错了 = 标歪,主循环 research 会自己重读重判;漏标无害。

主题词当前是 `research_context.md` 兴趣词的固定种子(`litwatch.sh` `default_themes`,`LITWATCH_THEMES_FILE` 可覆盖)。让 agy 按 `ledger.tsv` 死因反推查询词是干净的后续扩展(再加一段 agy stage 喂 theme 列表即可),v1 未做。

硬约束:agy 输出**只允许引用已取到的 id**。引用了不在取数集里的 id(或非串 id、坏行),`ingest` 丢弃并记 `drops.jsonl`——agy 结构上塞不进假论文。

### 3. 验真 — 结构隔离 + 纵深复核

两层。**结构隔离**:index 的记录只来自确定性取数(arXiv / S2 API 响应的 parse 产物);agy 的读写关在独立沙箱 `tmp/litwatch/agy/`(读的是那儿的 staging 只读拷贝),而 `ingest` 读的可信 `staging.jsonl` 在沙箱之外——agy 就算把自己拷贝塞满伪造论文也进不了 index。`ingest` 另校验每条标注的 id 逐字 ∈ 取数集,越界 / 非串 id / 坏行 / 重复丢弃记 `drops.jsonl`;注释文本不被信任(research 会重读)。**纵深复核**:agy-worker 的路径钉死是 prompt 级、非强制沙箱,不赌 agy 绝不越界写——`research.md` 对每条缓存 id 另做 live 复核(实际打开链接核对标题、≥1 条 live API、结构门槛一条不减),缓存即便被投毒也产不出错误 verdict,只白费一次查重。每条 index 记录天然带 API 来的真实 id,无须单独 re-resolve。

### 4. 缓存 — 耐用产物

`tmp/litwatch/index.jsonl`(gitignored,符合"agent 只写 `tmp/`")。按主题分组的 `{id, title, abstract, url, date, theme, agy_note}`。紧凑,可被 WebFetch / 读文件直接消费。

### 5. 冷却调起

litwatch 调 agy 一律走 `agy-worker.sh`,直接继承它的 mkdir 锁 + 时间戳 + `AGY_LAUNCH_GAP_SEC`。于是 litwatch 与任何 `hunt.sh` 的 agy 用法**共用同一个全局启动闸**:两边都不会把 agy 登录打爆,litwatch 也被限速不抢跑。代价:litwatch 的 agy 启动可能让 `hunt.sh` 的 agy 席(若启用)等最多 gap 秒;但默认主流程裁判是 claude,不启用 agy 席时零接触。

### 6. 节奏

cron / schedule routine 每 N 小时跑一次 `litwatch.sh`,尽力而为。agy 失败 = 本轮不刷新缓存;笔记本休眠不跑也无所谓,缓存旧了主循环照样冷启动补齐。

## 三条约束逐条兑现

1. **结果可被主流程用**:缓存打的就是 `research.md` 已经在打的同一批 arXiv / S2 端点。`research.md` 加**一段**:缓存在则先看本主题缓存条目当近邻种子,但 PROGRAM 的 ≥1 条 live API 记录、≥5 条实读近邻、结构门槛一条不减;缓存缺则行为不变。
2. **验 agy 产出正确性**:取数不经 agy(结构上无幻觉);agy 只能引用已取真实 id,越界 / 非串 id / 坏行被 `ingest` 丢弃记 `drops.jsonl`;index 记录只来自 API parse,agy 无法新增记录。
3. **冷却调起**:走 `agy-worker.sh`,继承 mkdir 锁 + gap 闸,与主流程 agy 用法共用一个全局限速。

## 不做(YAGNI)

- 不做按 idea 的定向查重(那要进循环)。
- 不建 embedding / 向量库(近作监视用不上;主循环靠实读判定,不靠相似度)。
- agy 不出任何 verdict / overlap / novelty 结论。
- 不改 `ledger.tsv`、不碰 publish、不进 `hunt.sh` 主回路。

## 真机测试结论(2026-07-14)

- **取数相关性 → 改 OAI-PMH 解决**:arXiv search API 对具体主题查询限流极狠(429/超时,Mac + 集群 xyh 两 IP 都验),唯一稳定返回的 term-soup + date 排序给离题噪声。改用 OAI-PMH 批量抓 + 本地类别/关键词过滤后,Mac 真机抓 2600 篇 cs → 83 篇相关近作、主题标注正确;OAI 端点(oaipmh.arxiv.org)不限流。
- **agy 端到端验过(有料输入)**:对 24 篇相关 OAI 近作跑真 agy(经 agy-worker.sh,~1min,无登录卡),产出 7 条高质量标注——正确识别近邻风险、结合 `research_context.md`、每主题 ≤5 篇、id 全部实来自 staging(ingest 0 丢弃)、沙箱隔离与零回归成立。**agy 能正常工作。**(对比:早前喂 search API 噪声时,agy 正确地一条没标。)
- **S2**:无 key 基本 429,默认关、留 `LITWATCH_S2_KEY`(x-api-key 头)口子。
- **网络错误**:429/超时/连接错干净跳过 + 退避重试认 Retry-After,真机复现并验过。

## 治理

组件 1 落地会新增 `litwatch.sh` + `roles/litwatch.md`(或等价 prompt);组件 3 会给 `roles/research.md` 加一段消费契约。后者属 `roles/` 协议改动,须记 CHANGELOG 并走分支 + PR 留审,不自动合。
