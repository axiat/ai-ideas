# CHANGELOG

## 2026-07-06 自动化 claude 调起隔离 MCP(--strict-mcp-config)

- `hunt.sh` 的 `AGENT_CMD` 与 `calib/run_panel.sh` 的 `PANEL_CMD` 默认从 `claude -p` 改为 `claude -p --strict-mcp-config`:子进程零 MCP——不继承用户级注册的任何 server(lark、claude.ai 连接器等),省每个 agent 的 MCP 启动与健康检查开销,应用凭据不再进无关自动化的进程参数(`ps` 可见)。README/头注示例同步。
- 配套的环境侧动作(不在仓库内):lark 已从 Claude user scope 移除,配置存 `~/.claude/mcp-lark.json`(600),需要写飞书文档时用 `claude --mcp-config ~/.claude/mcp-lark.json` 按需挂载;codex 侧本就未注册。

## 2026-07-06 删承重假设通道:第 5 形态 + 裂缝核验 + 窄 break-glass(已合并,PR #13)

背景:ledger 51 AwR / 18 reject / 0 SA,且 07-05 校准证明真 oral 素材在旧条款下也拿不到 SA 票——生成端只产范式内 probe(天然 AwR 形态),评审端又把这一类封顶,两端严丝合缝。SA 级 idea 的共性(Transformer 原型):删一条范式承重假设 × 外部约束逼出 × 便宜决定性否证实验。据此开一条窄而硬的证明路径,全链路落地:

1. **第 5 形态「删承重假设」**(`brainstorming_policy.md`):结构化字段——删哪条承重假设 / 为何现在能删 / forcing constraint / 裂缝证据(≥2 行带 URL,待核验自报)/ 最小否证实验加严为须能一击杀死赌注。发散要求加删公理配额:10 个原料候选中至少尝试 1 个,进不进自筛后的 4-6 个只看质量;未成写标记行(带一句话候选与卡点)不入 ledger,宁缺勿造。
2. **裂缝核验走查重管线**(`roles/research.md`):裁判 novelty 只认 priorwork(`roles/review.md` 铁律),自报裂缝留在 ideas.md 制度性无效;查重进程对该形态逐条实读核验 URL(判定词:相符/部分/不符/不可达,只记事实不评说服力),在 priorwork 块写「裂缝证据核验」节。
3. **第二条 break-glass**(`roles/review.md` + policy 评审校准):「赌注未经验证」本身不计 MAJOR(前提:否证实验便宜且决定性);**同时**满足四条件可 SA——头条零命中 overlap=low / 裂缝核验 ≥2 条相符 / forcing constraint 为明确外部压力 / 否证实验 1×H100 可执行可杀死。不豁免任何既有硬门(direct-hit、CRITICAL、≥2 MAJOR、查重薄弱、缺否证实验);五字段缺失或核验不符 → 视为话术合规按普通形态从严。
4. **hunt.sh 机械化**:新增 `AXIOM_MIN_CRACKS`(默认 2)与 `is_axiom_idea`/`axiom_ok`/`cracks_ok` 三校验,接入生成后、查重后、resume、SA 硬门槛四个挂点(SA 另须核验「相符」≥2,防话术蹭全票);标记行放 ideas.md 首个 `##` 之前,按块解析的下游天然忽略。夹具单测 17/17。`trigger.md`(weekly 自律版)、`PROGRAM.md`、`README.md` 同步;trigger.md 有改动,远端 weekly routine 已于 2026-07-06 手动同步。
5. **calib 双侧验证**(详见 `calib/results-2026-07-06.md`):`neg-axiom-cosplay`(话术阴性:五字段结构合规、真 URL 假主张、核验全不符、头条被 Diffusion Policy 基线表覆盖)3/3 reject,三票独立命中全部设计雷点;`pos-axiom-adam`(形态探针,ICML 2026 oral 2602.07729「Do We Need Adam?」投稿前形态,LLM 域越域注记)3/3 SA——校准史首个 min-vote SA,逐票点名四条件、无人给「未验证」记 MAJOR、无一票因越域拒绝。同一条款话术关、真货开,判别落在证据(核验相符与否)而非修辞;对照 07-05 v2 真 oral 仅 2/6 单票 SA,佐证当时"剩余封顶在材料内证据不足"的判读——五字段随材料交裁判后直接全票。具身域正式阳性待 RSS 2026(7/13-17 悉尼)奖项揭晓后选定,选取标准、oral 金标来源与判读表见 `calib/README.md`。

## 2026-07-05 AwR 复活 sidecar(直接提交 main)

- 新增 `agy-side.sh` + `roles/awr.md` + `roles/awr-judge.md`:主环之外用多轮 agy 把 ledger 中 accept-w-rev 的 idea 磨成可复审成品——研究员检索补缺口出修订稿,裁判按 rubric 判 `SA-可能/还不行`(失败关闭),缺陷回灌下轮继续改,默认 3 轮反馈用尽收尾。产物只落 `tmp/agy-side/awr/`,不碰 verdict/ledger/ideas,与 hunt.sh 并行安全。
- agy 弱点全走机械对策:每次调起只见 `tmp/agy-side/run.*` 临时镜像、指定输出由 bash 拷回(agy 实测不守 prompt 写界);产物机械校验(「## 修订版 idea」节、≥3 条带 URL 检索记录、判定二选一、末行 `AGY-DONE` 防早停),不合格 `.badN` 重跑、3 次拉黑;调起前清目标输出防旧 `judge.md` 误复用;与 `agy-worker.sh` 共享启动闸门戳(默认 120s)防连发触发登录验证。
- README 增 sidecar 用法与判读章节。

## 2026-07-05 当日目标数 SA_TARGET(已合并,PR #8)

- `hunt.sh` 新增 `SA_TARGET`(默认 1,行为同旧版;0=不设上限):停机条件从"当日 ≥1 全票 Strong Accept"改为"当日累计达目标数"。达标轮发布后未达目标则继续攒;同日多份报告按 `roles/report.md` 既有 `-2`/`-3` 后缀累加,`publish.sh` 幂等追加进同一当日分支与 PR,二者零改动。
- 重入判定从"当日报告文件存在"改为"`tmp/ledger.good` 基线中当日 hunt 源 strong-accept 行数达标";已有报告但未达标(如上调 `SA_TARGET` 重启)时启动先幂等补发布再继续。
- 报告写出判定从"当日报告存在"改为"报告文件数新增",防多报告日被旧报告蹭过。
- 同步 `hunt.md` 停机条件、`PROGRAM.md` 回路第 5 步、`README.md`。

## 2026-07-05 预筛 + 深查重 + 进化资格 + 裁判校准(已合并,PR #9)

背景:当日 29 个 idea 全无 SA(21 AwR + 8 reject)。死因分布:8 个 reject 全为 F1"已被占据"(查重/裁判事后才发现);约七成 AwR 死因涉 novelty(封顶或"实读仅 3 篇→novelty 未证实");3 次进化全选了 novelty 封顶的父本再次封顶。结论:瓶颈在查重深度与进化父本选择,不在生成时长。落地五项:

1. **预筛阶段**(`roles/prescreen.md` 新增,`hunt.sh` 生成与深查之间):便宜可错、只杀不保——只杀"单篇工作直接占据头条"的 direct hit,kill 必附占位链接与 ≥1 条 API 检索记录;被杀者由 orchestrator 立即按 reject 入账(overlap=high,防下轮重生成),存活取前 `SHORT_MAX`(3)个进深查,超额 keep 丢弃不入账,全灭走空产出短重试。shortlist/kill 台账由 bash 机械构建,agent 只给判定。主题门槛改查预筛前的发散全集(`ideas.all.tsv`)。
2. **深查重**:`roles/research.md` 每 idea 实读 3-5 → **5-8 篇**、先 direct-hit 猎杀、新增必填「最强反例」行(单篇最近邻 + 差异是否够 clear-accept);机械门槛联动 `PRIOR_MIN_LINKS` 3→5、SA 硬门槛 `MIN_READ` 3→5——prompt 与机械地板同步,防 agent 只满足地板。
3. **ledger 加 overlap 列**(6→7 列):聚合时从 priorwork「重叠判定」提取 high/medium/low 入账,进化父本资格由此机械可查;预筛杀的记 high。
4. **进化/复查资格收紧**(`roles/generate.md`、`brainstorming_policy.md`、PROGRAM.md 不动项 6):进化只准选 verdict=accept-w-rev 且 overlap=low 且死因属实验设计类缺陷的行(novelty 封顶/已被占据的不修);查重薄弱型 AwR 走「复查」——原样重交补查重,同 story 至多一次,补完仍封顶永久放弃;两者共用每轮 1 个名额。另:最小否证实验必须点名最强基线、给样本量与预期效应(对着今天的高频 MAJOR 硬化)。
5. **失败蒸馏扩容**(`roles/meta.md`):deathlist 三节化(致命模式/封顶模式/进化候选),触发计数从"仅 reject"改为 reject+AwR(今天主失败是 AwR,旧计数一直不触发,deathlist 从未产出过)。ledger 不再人工清理 reject 行——清了会饿死蒸馏与防重生成。

**裁判校准 harness**(`calib/`):`run_panel.sh` 对对照 case 跑 N 位禁搜裁判(独立目录、min-vote,与 hunt 评审同构;禁搜是因为对照多为已发表工作,联网会变成"被自己占据"的假阴性;裁判怀疑对应已发表论文时只做泄漏标记不改判)。对照组:pos-robomme(ICML 2026 oral,benchmark 型,arXiv 2603.04639)、pos-meanflow(ICLR 2026 oral,method 型)+ 理想 priorwork(8 篇实读、low、编号全经 arXiv API 核验);neg-replai(头条被 RepLAI 2209.13583 直接占据,如实 high)。判读:阳性若给足理想证据仍无人投 SA → 瓶颈在 verdict 逻辑/聚合规则;阴性若不全票 reject → 面板放水。

当日跑完(结果详见 `calib/results-2026-07-05.md`):阴性 3/3 reject(面板没坏);两个阳性均 3/3 accept-w-rev、六票零 SA。零 SA 票的结构性来源是两条评审条款——生命周期可行性按 idea 全量 scope 而非最小否证实验评、"已知机制搬新域默认不到 SA";在此之下聚合规则改 2/3 也不会触发。另:两份手工理想 priorwork 各漏了一个真实近邻(MIKASA-Robo、MP1),被裁判凭训练数据点名——深查重是对的投资方向。

### 校准后条款修正(A+B,operator 拍板)

- **A 可行性收窄**:lifecycle/feasibility 的评估对象改为「最小否证实验 + 首篇论文的合理裁剪(phase-1 scope)」,不再按 idea 最大愿景评;愿景全量超出单人算力不单独计 MAJOR。改动:`brainstorming_policy.md`、`roles/review.md`、`rubric.md` Step 6。
- **B 机制迁移破例**:同时满足目标域零命中(只认 priorwork)、适配机制非平凡、信号落地即够 clear accept 三条件的机制迁移可给 SA,逐条点名证据、缺一仍封顶。改动:同上两处 + review.md SA 门槛条款。
- 一致性修正:`PROGRAM.md` 不动项 4 与 `brainstorming_policy.md` 定向查重的篇数同步为 5-8;`ledger.tsv` 29 行历史行一次性 backfill 第 7 列 overlap=未知(schema 迁移,此后行行 7 列)。

远端 cloud routine "Weekly Embodied Idea Scout" 的 prompt 已于 2026-07-05 按新 `trigger.md` 手动同步。

## 2026-07-05 中断恢复(已合并,PR #6)

- **实例锁**:`tmp/hunt.lock`(mkdir 原子抢锁 + pid 记录),同目录双开第二个实例直接退出;持锁进程已死则自清重抢。双开会互踩 `tmp/round`、ledger 基线与守卫,此前无防护。
- **启动补发布**:当日报告已存在时,先跑幂等的 `./publish.sh` 再退。堵住"report 写完、publish 被中断"后重启直接 break、报告永久滞留本地的缺口。
- **publish.sh 幂等化**:无新改动但当日分支已存在(上次在 commit 后、push/PR 前中断)时补推送、补 PR;此前该状态下直接报"无待发布改动"退出。
- **前段续跑**:`RESUME_FRONT=1`(默认)时,中断遗留的 `tmp/round` 前段产物(ideas.tsv/ideas.md/priorwork.md)过机械门槛则首轮跳过生成/查重,省掉已花的调用费。评审票据/评审块残留一律清除、裁判重新调起——verdict 永不续用,防前段借崩溃伪造票据绕过独立评审。

## 2026-07-05 竞品调研落地(已合并,PR #5)

参照 Google Co-Scientist(meta-review / evolution)、AI Scientist v2(结构化检索的教训)与 Si et al. 2409.04109(LLM ideation 模式坍缩、feasibility 偏弱)落地五项:

1. **死因蒸馏**:新增 `roles/meta.md`;`hunt.sh` 每 `META_EVERY`(6)轮、ledger 拒行 ≥ `META_MIN_REJECTS`(5)时由前段进程把拒因归纳成 `tmp/deathlist.md`,生成阶段必读规避;可错阶段,失败不阻塞。
2. **进化通道**:每轮可含至多 1 个对 ledger accept-w-rev 行的定向修复版,按全新 idea 走完整查重与评审,不继承旧票;reject 行不得复活。改动:`PROGRAM.md` 不动项 6、`brainstorming_policy.md`、`roles/generate.md`。
3. **跨轮反坍缩**:`ledger.tsv` schema 5 列 → 6 列(新增 `theme`,取 policy 主题词表);`tmp/round/ideas.tsv` 加第 3 列主题;生成要求本轮 ≥2 个 idea 落在存量最少的三个主题;`hunt.sh` 每轮从 policy「发散透镜」小节随机抽一条注入生成 prompt(随机性在 bash 层)。
4. **feasibility 锚点**:每个 idea 必须附「最小否证实验」(数据 × 算力 × 预期信号);裁判 feasibility 只认它,缺失或不可执行按 MAJOR 计、封顶 accept-w-rev;SA 硬门槛(`hunt.sh sa_gate_ok` 与 trigger.md 自检)加该字段机械校验。改动:`roles/generate.md`、`roles/review.md`、`rubric.md` Step 6、`brainstorming_policy.md`。
5. **结构化查重通道**:`roles/research.md` 要求每个 idea 块 ≥1 条 arXiv/Semantic Scholar API 检索记录(实际 query URL,可复现);`hunt.sh priorwork_ok` 机械校验(`PRIOR_MIN_API`,默认 1,0 关闭)。API 只管召回,判定仍靠实读。

其余同步:`README.md`(角色列表、默认参数、SA 硬门槛)、`trigger.md`(阶段 1-4 对齐上述规则)。

### 同日 review 修正

- `hunt.sh priorwork_ok`:近邻链接只计「- 」bullet 且排除 API URL——修复"2 条近邻 + 1 条 API query 恰好凑满 `PRIOR_MIN_LINKS=3`"的充数漏洞;API 记录仍单独计数。
- `hunt.sh` 新增 `themes_ok` 主题门槛(生成阶段机械校验):theme 必须属 policy 主题词表,且本轮 ≥ `THEME_MIN_LOW`(2,0 关闭)个 idea 落在存量最少三个主题(阈值取第三低存量,并列计入;冷启动全零全员达标);不达标视同空产出重跑。此前 theme 纯靠生成端自标,可乱贴标签污染反坍缩统计。
- `hunt.sh sa_gate_ok`:最小否证实验从"字段存在"加严为"冒号后内容 ≥30 字节",拦空字段/占位;语义真伪仍归裁判。
- `hunt.md` 砍掉流程复述(已与 PROGRAM.md 分裂:仍写"三个独立进程"、查重只提 3 条链接),只保留入口特有项,协议指向 `PROGRAM.md`。

远端 cloud routine "Weekly Embodied Idea Scout" 的 prompt 已于 2026-07-05 按新 `trigger.md` 手动同步。
