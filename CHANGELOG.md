# CHANGELOG

## 2026-07-12 复验硬化:缺票/非词表票不再塌成永久禁(B2)+ 自报 ≥2 MAJOR 机械硬顶(B3)

P1 复验 B2/B3 两项聚合硬化。均在 hunt.sh 评审聚合环,不改任何协议/role 文件——B2 落实 review.md 写侧「每个 id 必须有一行」、B3 落实 review.md 铁律「含 ≥2 MAJOR → 封顶 Accept-w-Rev」,原先这两条只写在 prompt 里、bash 编排器未机械兜底。

- 【B2 缺票塌成永久禁】原聚合对每个 id 逐席 `rank_of $(cut -f2)`,缺票/词表外 token 一律 → rank 0,与真 `reject` 无从区分 → min=0 → `classify_nonsa` 归 novelty-dead → 永久禁复活。即裁判 rc=0 却漏写某 id 的票、或票拼错大小写(基础设施/格式故障),会把一个候选永久判死。修:新增 `vote_valid`(票须精确 ∈ {strong-accept,accept-w-rev,reject}),聚合前(动 ledger 之前)逐 id×逐席内容级校验,任一票无效即 `fail_and_wait` 按 review 失败**重跑整轮**(与既有 rev_rc 进程级守卫同款处理),日志点名 `I<n>@rev<r>[票内容]`。真 `reject` 仍照常入账,不误触发。
- 【B3 MAJOR 列未用】verdict.tsv 第 3 列 `MAJOR数` 聚合时被完全忽略,裁判自评 `strong-accept` 却自报 ≥2 MAJOR 的自相矛盾被原样采信。修:新增 `major_cap`(rank=2 且自报整数 ≥2 → 硬顶 1),在 `rank_of` 后、票向量/sa_votes/min 之前施顶,使降级流经全部下游(不计 SA 票、min 随之下降、分类随之变)。MAJOR 字段解析宽松(取首个整数),无法解析则不顶、回落信任第 2 列——纵深防御的交叉核验,不因格式小疵废轮(缺票的强校验归 B2)。
- 边界(诚实标注):B2 只强校验第 2 列 verdict 落词表,不强校验 MAJOR 列可解析(避免格式小疵放大成整轮重跑);B3 对不可解析 MAJOR 回落信任 verdict,故极端下"strong-accept + 乱写 MAJOR"仍可能漏顶——但须同时全票 SA 且过 sa_gate 硬门槛,交叉核验仍在。AwR 复活 sidecar(awr-side.sh)另有独立聚合,不在本次改动范围。
- 验证:`bash -n` OK。单测(从 hunt.sh 抽真函数 source,非副本)36 项——`vote_valid` 词表内/外(含大小写错、拼写错、含空格、空票)、`major_cap` 真值表(2,3→1;2,0→2;1,3→1;0,3→0;"约2个"→顶;"abc"→不顶)、`rank_of`/`classify_nonsa` 回归。集成(隔离沙箱跑真 hunt.sh,RESUME_FRONT 跳前段只 stub 评审/报告)21 项——全票 SA 全链路发布回归;MAJOR=3 顶为 AwR + 日志 `MAJOR 复核` + 票记 1,1,1;MAJOR=0 的 2,2,1 不误顶、入 near-sa-queue;全票 reject 正常入账 + B2 不误触发;裁判3 漏票 → B2 废轮 + ledger.good 无该行(不塌成永久禁)+ stages.tsv review=1;裁判3 大写 Reject(非词表)→ B2 废轮。

## 2026-07-12 复验修复:near-SA 队列生命周期(A1)+ design-fixable 收窄(A2)+ 分类注释纠错(B1)

P1 复验(8 项全确认)后修 Top 2 真 bug。A1/A2 耦合:A2 制造"AwR+low 但非进化/复查资格(如 feasibility 封顶)"的行被误入队,A1 队列无 dequeue + generate 被要求"先取队首不得越过" → 不合格队首毒死唯一进化/复查名额。

- 【A1 队列只写不消费】near-sa-queue 原为 append + story 去重,无 dequeue、不清理、跨运行无界增长,generate 卡死队首。修:(a) `prune_near_sa_queue` 每轮生成前跑——删 story 在 ledger.good 已出现 ≥2 次的终态行(已复查/进化/改到 SA 或再判死),再按 `NEAR_SA_MAX`(默认 30)截断防无界增长、顺带老化淘汰从未被选中的残留;(b) 入队加 `story_cnt<2` 门(同 story 在 ledger ≥2 次=复查已用尽,不再入);(c) generate.md 改"取队列**首个资格仍合法**的行,不合格就跳下一行、别卡死队首",不再裸队首。
- 【A2 design-fixable 过宽】`classify_nonsa` 只按 (raw_min,overlap) 判 design-fixable、不看 reason,而进化资格要求 reason 属实验设计类且排除 novelty 封顶 → AwR+low 但因查重薄弱/feasibility 封顶的行被误标入队。reason 是自由文本、机器判不了类型,故不在 classify 硬判,而是:注释明确 design-fixable 是**粗标**(可能不合格),真资格由 generate 读 ledger reason 定(design-fixable→进化、evidence-incomplete→复查,不匹配就跳过),粗标只用于入队;prune 老化淘汰不合格残留。
- 【B1 顺带】classify_nonsa 注释原称"reject 必因 CRITICAL、正是 direct-hit/CRITICAL 集"——过度声称。改为如实:非降级 reject 归 novelty-dead 是**失败关闭近似**(机器只据 rank_of 判 min=0,缺票/软拒/乱码都塌成 0),不声称已机检 CRITICAL。(PROGRAM.md 的对应 category 文案在后续文档 PR 一并改。)
- 验证:`bash -n` OK;单测真 `prune_near_sa_queue`——终态行(count≥2)删、count<2 留、cap 保留最后 N;cap 顺序正确;e2e 入队 count-gate(预置 story 已在 ledger 1 次→本轮命中 count=2→不入队,其余入);#10 归档停机 A/B/C 无回归。

## 2026-07-12 P1 #1:独立 selector——生成只发散,排序交独立进程

落 `P1-PROGRAM-DRAFT.md` 的 #1(用户授权改 PROGRAM.md + policy)。原 generate 在同一 context 里"发散 10 再自筛 4-6",自筛混在生成里、违背角色分离;shortlist 排序只有 `keep_rank` 机械定。改成生成只发散、排序交独立进程。至此 P1-PROGRAM-DRAFT.md 三项(#4-schema/#6/#1)全部并入,按其说明删除该文件。

- 新增 `roles/select.md`:独立 context、便宜可错、只排不杀,按四准则(命题强度 / clear-accept 上限 / 最小否证实验质量 / 可执行性)给发散全集全序排名,写 `tmp/round/select.tsv`。排序在深查前跑、拿不到 priorwork,故"novelty"维只评命题强度(逼不逼得出可证伪判别),不是"已确认无人做过"。
- `roles/generate.md`:"发散 10 再自筛 4-6" → "发散约 10 个全部写入,不自筛";透镜/删公理配额里"自筛后 4-6"改为"selector 排进深查名额";铁律"自筛避开已占机制"→"发散避开"。
- `hunt.sh`:generate 与 prescreen 之间插 `select` 阶段(FRONT_CMD,rc≠0 只告警不停);`select_rank_of` 查 select.tsv 名次、缺失/非法回落 999;`keeps.tsv` 加 select名次列;`select_shortlist` 排序键 = keep_rank(硬配额)> select名次 > 低存量主题 > 生成序。selector 缺失不废轮(退化为生成序)。
- `PROGRAM.md` §回路:step1 改"发散约 10(不自筛)"、新增 step1.4 排序、step9 "约 10 个经独立排序 + 预筛裁剪";canonical `brainstorming_policy.md:8` 删公理配额"自筛后 4-6"同步改。`README.md` 流程串补"独立排序"。
- 验证:`bash -n` OK;stub——select.tsv order=I3,I1,I2 + SHORT_MAX=2 → shortlist=I3,I1(I2 溢出丢、日志带 select名次),调用序列 generate→select→prescreen→research、stages.tsv 有 select 行;STUB_NO_SELECT(无 select.tsv)→ 回落生成序 shortlist=I1,I2;#10 归档停机 A/B/C 无回归(select 每轮跑、SA 轮发布正常)。

## 2026-07-12 P1 #6:复活软化——只 direct-hit/CRITICAL 永久禁,evidence-incomplete 准复查

用户授权改 `brainstorming_policy.md`(#6 复活规则 canonical 在此),落 `P1-PROGRAM-DRAFT.md` 的 #6。原"reject 行一律不得复活"把"全票 SA 仅因硬门槛降级、票够只差证据"的候选也永久封,near-SA 转化率上不去。

- `PROGRAM.md §不动项6` + canonical `brainstorming_policy.md:7` 同步:reject 复活资格按 category——**novelty-dead**(direct-hit / overlap=high / CRITICAL)永久禁;**evidence-incomplete** 准一次复查(补证),块首记「复活自」「复活条件」,同一 story 至多一次、补后仍不达标并入永久禁。
- `roles/generate.md`:复查条从"仅 accept-w-rev"扩到"也含 category=evidence-incomplete 的 reject 行";进化条改为"只修 accept-w-rev,reject 复活见复查";near-sa-queue 读注标明 design-fixable→进化、evidence-incomplete→复查。
- `hunt.sh`:near-sa-queue 入队条件 `design-fixable` → `design-fixable || evidence-incomplete`(两者都 sa_votes≥1);classify_nonsa 注释更新(reject 恒 novelty-dead 正是 direct-hit/CRITICAL 集,唯一可复活 reject 是 evidence-incomplete)。
- **对草案的更正**:草案把 design-fixable/ceiling-limited 也列为"可复活 reject"不准确——它们是 accept-w-rev 的类别(走既有进化/复查);reject(min=0)只会是 novelty-dead 或 evidence-incomplete,故 #6 实际放开的唯一 reject 类是 evidence-incomplete。
- 验证:`bash -n` OK;stub——全票 SA(2,2,2)+ 空 review.md → sa_gate 降级 → ledger `verdict=reject, cat=evidence-incomplete`,near-sa-queue 收该行(标 evidence-incomplete);#10 归档停机 A/B/C 无回归。

## 2026-07-12 P1 #4-schema:ledger 加 category 列(非 SA 四分类持久化)

用户临时授权改 PROGRAM.md,落 `P1-PROGRAM-DRAFT.md` 的 #4-schema。非 SA 四分类原只在 `tmp/nonsa-class.tsv` 观测、跨运行不持久;进 ledger 后可持久、可供 generate 复活判定(#6 的前置)。

- `PROGRAM.md`:ledger schema 7 列 → 8 列,末列 `category`(novelty-dead / evidence-incomplete / design-fixable / ceiling-limited / `-`);§回路 step4 聚合记账补 category 列;旧 7 列行缺此列按"未知"处理(同 overlap 旧行规矩)。
- `hunt.sh`:两处 ledger 写补第 8 列——聚合处 SA 行写 `-`、非 SA 走 `classify_nonsa`;预筛 kill(direct-hit)写 `novelty-dead`。所有 positional 读(theme=f3 主题存量、verdict=f5 SA 计数/META 统计、overlap=awk on priorwork)不受影响。
- `generate.md`/`meta.md`/`trigger.md`:"行末 overlap 列"位置引用改为"overlap 第 7 列 / category 第 8 列"(否则新 schema 下"行末"会指到 category)。
- **未做**:#6(复活软化)、#1(独立 selector)的规则 canonical 在 `brainstorming_policy.md`(#6 复活规则在第 7 行、#1 的"自筛后 4-6"在第 8 行),要改 policy(另一 human-only 文件),仅授权 PROGRAM.md 不够;见 `P1-PROGRAM-DRAFT.md`。
- 验证:`bash -n` OK;stub——near-SA 轮 ledger 每行 8 字段且 col8=design-fixable;#10 归档停机 A/B/C 无回归(scenario C 发布的 SA 行 8 字段、col8=`-`,publish/settle 正常)。

## 2026-07-12 P1 候选质量:研究只报事实 + 检索不完整补查 + 非 SA 四分类 + near-SA 队列

P1「提高候选质量与 near-SA 转化」的 agent 可改子集(#2/#3/#4-观测/#5-队列)。#1(独立 selector)、#6(复活软化)、#4 落 ledger schema 要改 human-only 的 PROGRAM.md/schema,草案见 `P1-PROGRAM-DRAFT.md`,不在本次代码内。

- 【#2 research 只报事实】`roles/research.md`「最强反例」行原要 research 判"差异是否足以支撑 clear-accept",违背该角色自己的铁律「只陈述事实、不打分」→ 改成只报"具体差异在哪"这个事实,clear-accept 上限交裁判(裁判本就据 priorwork 的 overlap+差异独立判 ceiling,不依赖 research 代判)。
- 【#3 检索不完整→定向补查,不进定级】原查重不达机械门槛(链接/API/块/裂缝核验不足)= `empty_and_wait` 整轮作废,连同同轮好候选一起丢、也把"没查完"当成了"低重叠"定论。改:`RESEARCH_RETRY`(默认 1)次对同一 shortlist 定向重跑 research(补查前 `rm priorwork.md` 防新旧块混算门槛),耗尽才整轮作废;research.md 加铁律"检索没做完如实标,别为凑门槛把没读透的近邻硬写成 low"。
- 【#4 非 SA 四分类(观测,不动 schema)】新增 `classify_nonsa`(按 降级前最低票 / 是否硬门槛降级 / overlap):evidence-incomplete(全票 SA 被硬门槛降级=票够只差证据)、novelty-dead(overlap=high 头条被占)、design-fixable(accept-w-rev+low)、ceiling-limited(accept-w-rev 但被近邻封顶)。落 `tmp/nonsa-class.tsv`(持久观测),不进固定 ledger schema——进 schema 的版本见草案 #4。
- 【#5 near-SA 队列 + lineage/delta】聚合处对 design-fixable 且有 SA 票的 near-SA 写 `tmp/near-sa-queue.tsv`(去重按 story `grep -Fxq`,防跨轮堆积);`generate.md` 父本优先级改 near-sa-queue ＞ deathlist 进化候选 ＞ ledger 自筛,唯一进化名额先取队首、不越过它盲目扩池,并要求进化块加「delta:<相对上版改了什么、为何突破上次封顶>」行。仍守 PROGRAM step6 的 accept-w-rev+low 父本资格与"至多 1 个"名额(队列只改优先级、不新开复活路径);"每个 near-SA 达终态"的完整簿记归 AwR 复活链路重建(另一 P1)+ #6。
- 验证:`bash -n hunt.sh` OK;stub 回归三场景全绿(见下)。

## 2026-07-12 复验修复四:重启补归档语义纠错 + delta 入 rc + 声称/注释收敛

第四轮复验:6/10 PASS,4 项仍有缺口。

- 【#10 PARTIAL 恢复语义错 + delta 被吞】停机文案/注释谎称「修复 `$RUNS_DIR` 后重启会补归档并发布」,实则重启走孤儿 SA 路径(新 run_id、以已达标发布),原 run 的归档永不回填——等于重启就重开这个洞。**修法**:(a) 停机时落哨兵 `tmp/HALTED-ARCHIVE-FAIL`(记 run_id/sa_count/reason,放仓库内、不与常见停机因 `$RUNS_DIR` 不可写同命),启动即检测、有则 exit 2,逼人工二选一(补回该 run 归档,或从 `ledger.good` 删孤儿 SA 行重查)再删哨兵;(b) `ledger.delta.tsv` 写失败原被 `|| true` 吞、不进 rc,改为写失败即 rc=1(SA 轮据此停机)——增量是审计载体的一部分;(c) 文案改成如实描述恢复步骤。
- 【#3 FAIL 标签仍称真检索】源码 98-100 已诚实免责,但脚本头注、`calib/README.md`、输出仍写「真实检索/E2E」→ 收敛为「端到端检索召回校准(效力以后端联网为前提)」,README 补「效力边界」段(机械断言只查召回结构、不自证联网),`run_e2e.sh` 头注同改,示例注释「真检索跑」→「联网后端跑」。
- 【#5 FAIL 维度否决残留】720/723 两条 bullet 仍是「count ⇒ verdict」措辞 → all≤4 改「thin idea 的算术投影,不是 Reject 规则;Reject 由 value assessment 或 CRITICAL 定,绝不由计数」;no-dim-7 改「clear-accept bar 需一个 standout(约 8),无 7+ 者定义上够不到——是 bar 生效、非独立计数否决,只封 SA、不单独在 AwR/Reject 间定谳」。
- 【#1 PARTIAL agy 可写归档】重定位关不掉同用户 untrusted 的 agy(可写 `$HOME`)——本性如此,非位置 bug。兜底收敛为诚实 best-effort:hunt.sh 头注/RUNS_DIR 注释如实说明「唯一够得到的是 agy、真隔离需独立 uid/容器」;`agy-worker.sh` prompt 禁写清单补入 `~/.ai-ideas-runs/`;归档的可还原性只在有 SA 的可信裁决轮被依赖(agy 永不担任 verdict 席)。
- 验证:全脚本 `bash -n`;stub 三场景——A 归档坏+SA → exit 2、写哨兵(run_id/sa_count/reason 齐)、无报告;B 哨兵在(目录已修好)→ 启动即 exit 2、循环未跑(0 聚合日志、0 新 manifest);C 删哨兵+清孤儿行+修目录 → exit 0、发布,manifest+报告+`ledger.delta.tsv` 齐。

## 2026-07-12 复验修复三:归档失败对 SA 轮改停机(不再发布审计链断裂的 SA)

复验指出上一版 P2-5 仍不彻底:归档拷贝失败只告警不停机,探针令 `$RUNS_DIR` 中途不可写后 archive 完全缺失,hunt 仍发布 SA 且 rc=0——P0 可还原性承诺对一个已进 ledger+PR 的 SA 静默作废。

- `archive_round` 改返回 rc(建目录/manifest/拷贝任一失败即 rc=1,不再一律吞错),按终态分级:
  - **有 SA 将发布的轮**:裁决归档(verdict,发布前)失败 → 停机 exit 2,绝不发布审计链断裂的 SA;SA 行已在 ledger.good,修复 `$RUNS_DIR` 后重启补归档并发布。
  - **无 SA 轮 / 发布后的 published 刷新**:失败只告警(前者无发布物;后者裁决归档已成、已带完整 adjudication,只缺 report 日志刷新)。
- 验证:stub 探针(归档目录 review 阶段置只读 + 全票 SA)→ hunt exit 2、不生成报告、不发布;正常对照(归档可写 + SA)→ exit 0、1 manifest + 1 报告正常发布。

## 2026-07-12 复验修复:归档移出 workspace、E2E 声称收敛、awr-judge 硬门内联、rubric 维度否决

上一条修复的复验指出 4 项未做彻底(#2/report 冻结、run_all/publish/cp 三条 P2 复验通过):

- 【#1 归档仍在写域内】上版只加 grok/claude 的 tmp/runs deny,但 codex/grok 的 OS sandbox 写域=仓库工作树,tmp/runs 仍可写 → RUNS_DIR 默认移出仓库到 `$HOME/.ai-ideas-runs/<repo>`:claude allowlist(仅 ideas/tmp/)、codex/grok sandbox(仅仓库)都够不到,归档只由 hunt.sh(无沙箱编排器)写。启动 log 打印落点。残留仅 agy(可写 $HOME、前段不可信、不碰 verdict/ledger)。grok/claude 的 tmp/runs deny 保留作 RUNS_DIR 被覆盖回仓库内时的兜底。
- 【#3 E2E 不能证明真检索】link/API 计数只证明产物结构完整,离线 agent 硬编码这些字符串(甚至明写「未检索」)仍过——纯文本无法证伪。**改法是收敛声称而非假装证明**:注释/头注明确 E2E 只作回归门(拦薄/空/漏占位),效力来自用真·联网后端跑 research 角色,判读须默认后端联网;删掉「验证真检索发生了」的过度措辞。检索结构门槛保留(仍拦薄产物)。
- 【#4 awr-judge 丢了 PROGRAM.md 独有硬门】上版去掉 sandbox 外的 PROGRAM.md 引用,但连带丢了其独有的「最强反例」行 + ≥1 条可复现 API query 硬门,而 sandbox 仍无 PROGRAM.md → 把定级证据硬门(定向查重 5-8 篇+链接+最强反例行+API query URL、最小否证实验、删承重假设裂缝核验 ≥2 相符)直接内联进 awr-judge.md,自足、不依赖沙箱外文件。
- 【#5 rubric 残留维度否决】740-741 已修,但 716「all ≤4 → Reject」仍是纯维度否决、717-718「no dim reaches 7 → ask the user」既是维度否决又假设有 user(自主 hunt 没有)→ 加总纲句把这组聚合降为诊断启发,verdict 锚定 fatal-flaws 逻辑(CRITICAL→Reject、≥2 MAJOR→封顶)+ policy clear-accept bar;all≤4 改「value assessment 落到 Reject,由计数佐证而非决定」,no-dim-7 改「封顶 SA、评审中点名主轴,但不单独定 verdict」,删掉 ask-the-user。
- 验证:全脚本 bash -n;stub hunt 端到端确认归档落 `$HOME/.ai-ideas-runs/<repo>`(仓库内无 tmp/runs)、冻结语义在新位置仍正常(published 归档保留评审输入 + report 阶段进 stages/logs、delta 3 行)。

## 2026-07-12 code review 修复:归档冻结/审计边界、E2E 检索证据、run_all 假绿堵漏

对 P0(#20)与 gold set 诚实化(#22)的 code review 确认 10 项缺陷,全部修复(P1×5、P2×5):

- 【P1】按运行归档在 agent 写域内(tmp/runs 可被 file-tool 写、且在 guard 视野外,后续阶段可静默回改旧运行的 manifest/证据)→ grok-worker 加 `deny_tree tmp/runs`、`.claude/settings.json` 加 `deny Write/Edit(tmp/runs/**)`;归档由 bash 写、agent 禁改。codex workspace-write 无子树 deny,残留同 `tmp/ledger.good`,头注注明。
- 【P1】report 阶段覆盖裁决快照:`archive_round` 在 verdict 后还会被 published/report-missing 复用同一 run_id,原实现 `rm -rf`+全量重拷 tmp/round,把 report 之后的状态写进归档 → 改冻结语义:首次全量拷入即冻结评审输入(ideas/priorwork/rev),二次只刷 manifest + 补 stages/logs,绝不重拷。
- 【P1】E2E 跑道不验证真检索:grader 只算 link 数不校验,只发记忆里的占位 URL + `重叠判定:high` 即可过 expect → 加检索硬门槛(每块 ≥E2E_MIN_LINKS=5 条非 API 近邻 + ≥1 条结构化 API query URL,同 hunt.sh priorwork_ok 口径),不足判 retrieval-thin。
- 【P1】awr-judge.md 引用 sandbox 里没有的 PROGRAM.md(#20 引入):awr-side run_agent 只拷 awr roles/rubric/policy 进沙箱 → 改成只引沙箱确有的 policy(SA 唯一定义)+ rubric(SA gate/定级证据)。
- 【P1】rubric.md 残留机械否决:214-218 已改「维度分不单独决定 SA」,但 740-741 仍写「无 8+ 维的 SA 不自洽」,对全 7 维的 clear-accept 候选给出互相矛盾的强制规则 → 740-741 改指 policy clear-accept 标准,维度分只作诊断提示。
- 【P2】E2E 镜像拷真仓 settings(含 `Write(//tmp/**)`、`Write(//private/tmp/**)`),research 席可把持久写留在镜像外 → 改写 E2E 专用受限 settings:只放行 `Edit/Write(tmp/**)` + WebSearch/WebFetch。
- 【P2】run_all 假绿:显式请求但缺 expect 只 skip、空/纯注释 expect 零断言仍计 pass、全 skip 仍 exit 0 → 加 `config-error` 计数(缺 expect、零有效断言均计入),退出谓词要求 `fail=0 && panel-fail=0 && config-error=0 && pass+probe≥1`。
- 【P2】run_all panel-fail 仍读 aggregate.tsv:run_panel 若在清场前早退(如 PANEL_CMD 非法),残留上轮票据被记进 panel-fail 行 = 谎报本次从未产生的票 → panel-fail 时 votes 置 `-`。
- 【P2】publish.sh 失败在 archive_round 之前 exit,终态只留 exit_reason=verdict(缺 report 日志与真实失败)→ 失败分支补 `archive_round publish-failed`(冻结刷新,带 report 阶段)。
- 【P2】归档 `cp -R || true` 静默吞错,磁盘满/不可写时审计快照静默残缺却继续发布 → 改响亮 `log` 告警(不 halt:tmp 拷失败不值废整轮,但绝不静默)。
- 验证:全脚本 `bash -n` + settings.json JSON 校验过;run_all 三种坏 expect(空/纯注释/缺)端到端记 config-error 且退出非零、正常 case 仍 exit 0、panel-fail 行 votes=`-` 不带旧票;检索门槛对真产物(7 links+1 API)放行、对薄产物(1 link+0 API)拦为 retrieval-thin;E2E 受限 settings 端到端(Opus)复跑 neg-meanflow-mp1。gold set 单模型全量回归(3 席 Opus 4.8)pass=4/probe=1/fail=0/panel-fail=0。

## 2026-07-12 gold set 材料诚实化:两条被点名条款判对,真 bug 在旧材料谎报低重叠

首轮完整校准(见 `calib/results-2026-07-12.md`)两个正式阳性 0 SA,理由落在 novelty 封顶 + 查重缺 web/工业占位。为解耦「材料不达现行门槛」与「条款过严」,对三个阳性 case 的 priorwork 做诚实全知重建(实际 web+API 检索核实各 case 投稿时点真实文献格局),条款一字不动重跑:

- pos-axiom-adam(删公理探针):补齐 web/工业占位「未检出」记录后 **1/3 → 3/3 SA**,四条件逐票点名——上轮 1/3 纯是材料不达门槛,删公理通道机制正确。
- pos-meanflow → **neg-meanflow-mp1**:核实发现头条被 MP1(2507.10543,2025-07,带代码)在 ICLR 投稿前 72 天精确占据,「一步全靠蒸馏」前提早被 FlowPolicy(AAAI 2025 oral)证伪;真实 oral(2602.13810)靠 IVC 增量+RL 赛道错位+未引 MP1 存活,不构成诚实全知下值 SA 的阳性。`git mv` 降级为 direct-hit 阴性(2025 新占位,补 neg-replai 的 2022 老占位形态),3/3 reject,e2e 召回 2507.10543 记 high。
- pos-robomme:核实发现 MIKASA-Robo(2502.10550,早 11 个月)已占四类记忆分类学+隔离任务族,诚实重叠 medium(旧材料谎报 low);重跑仍 3/3 AwR,但理由变为 novelty 封顶+单人建构 feasibility MAJOR+无 8+ 维叠加。expect 改 min_vote>=accept-w-rev(真 oral 不该被 reject;本仓单人 phase-1 SA 门槛下 medium 重叠+建构负担使 SA 非 ground truth)。
- 结论:被点名两条 verdict 条款(机制迁移「适配非平凡」、诊断天花板)均判对——meanflow 的 AwR 是旧材料谎报低重叠把 negative 抬成 AwR;诊断天花板是条件式(无 8+ 维才封顶)非绝对禁令,low 重叠高 Broader 的 benchmark 会拿 8+ 逃逸。**条款不改**,brainstorming_policy.md / roles/review.md 未动。真缺陷在 gold set 材料。
- 缺口(记入 results):无干净 low 重叠 benchmark 正对照正面验证诊断天花板逃逸出口;method 型正对照席位随 meanflow 降级而空缺。
- bugfix:`calib/run_e2e.sh` 与 `hunt.sh` 的 overlap 解析改锚定行首 `^重叠判定`——查重块其它行会路过「重叠判定」字样(如 API 召回说明"不作重叠判定依据"),非锚定 `grep -m1 '重叠判定'` 抓错行、把真实 high 误读成 low(neg-meanflow e2e 首跑即中招)。
- 模型注记:初次面板 Fable 5 用量触顶,robomme/meanflow 重跑于 Opus 4.8;axiom 触顶前在 Fable 5 跑完(与初次同模型,材料是唯一变量)。未改材料的 neg-replai/neg-axiom-cosplay 条款材料均未动,沿用初次 3/3 reject 不重跑。

## 2026-07-12 P0 落地:SA 口径单源 + run_id/按运行归档 + calib 机器判读与端到端跑道

DEVELOPMENT.md「优化 autoresearch 成功率」P0 五项(统一判定与完整观测):

- SA 口径单源化(policy 评审校准节新增唯一定义声明):`rubric.md` Step 8 的「两维 8+」与 Integrity gate #5 的机械 SA 门槛改为指向 policy 的 clear-accept 标准(维度分是诊断证据,不是 SA 机械阈值);`roles/review.md` 撤销「且能冲 oral/spotlight」加严(policy 原文是「更佳」非必要条件——抄写漂移把 SA 门槛抬高了一档,可能是 288 票仅 6 SA 的成因之一);`roles/awr-judge.md` 与 README 的「rubric 的 SA 硬门槛(节)」错误指向改为 policy 评审校准 + PROGRAM.md 定级证据;`trigger.md` 同步撤销「冲不了 oral 不给 SA」(远端 weekly routine 已于 2026-07-12 由 Qinning 手动同步到该版)。
- hunt.sh 按运行归档:每轮 run_id(启动时间+pid+轮次,candidate_id=`<run_id>/I<n>`);轮终点(fail:<stage>/empty:<stage>/verdict/report-missing/published)把 tmp/round 全量产物(ideas/priorwork/预筛/三席票据与完整评审/逐阶段日志)+ manifest(来源/backend/policy_sha+git_head/退出原因/票向量)+ ledger 增量行固化 `tmp/runs/<run_id>/`,同一 run_id 后到覆盖先到(exit_reason 按最新);ledger 只留摘要,任一结论可还原输入与判定过程。run_stage 输出另 tee 进 `tmp/round/logs/<stage>.log`,起止/rc 记 `stages.tsv`(review 段并行块单独记)。metrics.tsv 末列加 run_id,启动时一次性 header 迁移(旧行保持 12 列,按前 12 列位置解析不受影响)。ledger 增量行数基线取 `grep -c ''`,空文件时不可接 `|| echo 0`(grep 已输出 0 且 rc=1,双行)。
- calib gold set 机器判读:`cases/<case>/expect` 断言 DSL(min_vote/sa_votes/reject_votes/all_votes;`probe` 只跑不打分),`run_panel.sh` 聚合另落机器可读 `aggregate.tsv`,新增 `calib/run_all.sh` 批跑打分、打印校准正确率(probe 与 panel-fail 不计分母——面板基础设施失败不得形成校准结论),逐 case 追加 `tmp/calib/summary.tsv`。
- 端到端(真检索)校准跑道与冻结校准分开:新增 `calib/run_e2e.sh`,镜像跑 roles/research.md(检索放开,写界同面板),断言 priorwork 召回已知占位(`e2e.expect`:overlap/url_contains;首个 case neg-replai→2209.13583)。阳性无端到端跑法(已发表工作被真检索判成自占据),边界记入 calib/README。踩坑:子 shell cd 进镜像后日志重定向必须用绝对路径(run_panel 同款坑,已有注释)。
- 围栏感知 id 提取抽单源 `lib/md_ids.sh`(run_panel 与 run_e2e 共用,防幻影 id/吞真标题两个方向)。
- 验证:scratch 克隆(本地 bare origin)+ 假 agent 全链路——空产出→归档 empty:research;全票 SA→报告→发布(分支落 bare)→归档 published(delta=3 行 SA、stages/logs/manifest 齐);research rc=1 且 MAX_FAILS=1→exit 1 归档 fail:research;前段续跑轮首调即 review、verdict 归档 sa_count=0;metrics 老文件 header 迁移后旧行 12 列无损。calib:reject/SA stub 面板 5 case 判分(2 pass/2 fail/1 probe,正确率 2/4,退出码 1/0 正确);run_e2e pass/fail/agent-fail 三径与镜像清扫;md_ids 围栏样例无幻影、未闭合围栏 rc=3。全脚本 `bash -n` 过。真实裁判(claude/codex/grok)校准面板未在本次跑,按例行 `./calib/run_all.sh` 执行。

## 2026-07-11 review 修复:resolver 换行/空白路径、GROK_SANDBOX 封 fail-open、面板前置校验与 BOM

code review(high 档)确认 10 项缺陷,全部修复:

- `lib/resolve_cmd.sh`:首词切分弃 `read -r`(here-string 只读到首个换行,含换行的 SIDE_CMD/PANEL_CMD 会静默丢掉换行后全部参数——沙箱/审批 flags 就此蒸发还不报错),改参数展开按任意空白切、rest 原样保留;解析出的绝对路径含空白即拒 exit 2(调起点按 IFS 拆词必拆碎它,含空格仓库路径原本过了启动校验、调起时全 127 空转,正是启动解析要防的形态)。
- `grok-worker.sh`:GROK_SANDBOX 补校验——grok 0.2.x 对认不出的 profile 只打 warning 就**无沙箱跑完全程**(实测,fail-open),固定枚举 workspace/off,其它值 exit 2(与 GROK_DISABLE_WEB 同风格:安全开关不 fail-open)。有意不放行 sandbox.toml 自定义 profile:可靠判定「toml 真有该 table」需要 TOML parser + profile 名转义(grep 级检查会被注释里的同名串骗过,复核实测确认),为无人在用的形态不值;两处 sandbox.toml 均不存在。
- `calib/run_panel.sh` 前置校验挪到 `rm -rf` 清场之前:裁判数须正整数(原 REVIEWERS=0/非数字时 seq 空转、空 pids 数组在 bash 3.2 + set -u 下 `wait` 崩 unbound variable,且上轮票据已被清掉);PANEL_CMD 解析失败同样不再先毁上轮票据再退。
- `calib/run_panel.sh` id 提取:未闭合围栏(CommonMark 语义吞到文件尾)不再静默照办——其后真标题全进不了 id 清单、正确面板被误作废,现 `PIPESTATUS` 捕获 awk `exit 3` 响亮报错让人修 case。
- `calib/run_panel.sh` verdict.tsv 拷回规范化补剥 UTF-8 BOM(带 BOM 合法票首 id 变 `\xef\xbb\xbfI1` 被判未知 id,与 CR 同类隐形字节假失败);`LC_ALL=C` 使 substr 按字节计(UTF-8 aware awk 把 BOM 当 1 字符会多剥)。
- `calib/run_panel.sh` 聚合的「缺票(计 reject)」死分支(rc 校验+verdict_ok 后 v 不可能空)改内部不一致响亮中止——留着会让维护者误信缺票仍静默降 reject,恰是 verdict_ok 加固要消灭的伪装通道。
- `awr-side.sh` resolve 报错前缀按值的实际来源命名(SIDE_CMD/SIDE_RESEARCH_CMD/SIDE_JUDGE_CMD,与 `:-` 回落同判据;原一律报 SIDE_CMD,SIDE_JUDGE_CMD 拼错会支使用户查错变量)。
- 镜像隔离预提示抽单源 `lib/mirror_pre.sh`(run_judge 与 run_agent 各持同构文本已实际漂移:awr 侧禁写 `~/.gemini` 等家目录、面板侧没有;单源后面板席同样获得家目录禁写)。有意不合并两函数的 mktemp/拷入/拷回骨架:输入集、拷回策略、节流/熔断真不同,强抽会造出参数森林。
- 验证:22 项回归全过(resolver 换行/tab/空串/`..`/裸名/PATH 回退/含空格仓库、REVIEWERS=0/非数字与 PANEL_CMD 拼错均 exit 2 且上轮票据无损、未闭合围栏响亮退、闭合围栏 id 无幻影、BOM+CR+trim 规范化及无 BOM 不误伤、GROK_SANDBOX 三态、awr-side 三席报错标签),全脚本 `bash -n` 过。

## 2026-07-10 review 修复:resolver 抽单源加固 + 面板输入冻结

code review(high 档)确认 3 个正确性缺陷、1 个幻影 id 隐患、1 个快照口径问题,全部修复:

- resolver 抽单源 `lib/resolve_cmd.sh`(SIDE_CMD 与 PANEL_CMD 原各持逐字拷贝,修 resolver 必两边同步、必然漂移),同时修三处:`..` 封禁从「仅开头 `../`」改为任意路径段(`./tmp/../../x` 原可穿出真仓 pin,静默执行仓外脚本);裸名须真仓根同名文件可执行才遮蔽 PATH(原杂散非可执行同名文件会让 `SIDE_CMD='claude -p …'` 启动即死,常驻 sidecar 整体停摆);报错前缀参数化。grok-worker 写禁树加 `lib`。
- awr-side run_agent 的 agy 闸门首词改 `read -r` 按任意空白切(原 `${cmd%% *}` 只认空格,tab 分隔的 agy 命令串绕过闸门,连发触发登录验证——正是闸门要防的);`${nbad:-0}` 死防护改 `$nbad`(计数前已无条件初始化,`:-0` 是旧 `ls|grep -c` 管道的遗留,留着误导维护)。
- run_panel 输入冻结:活 `$CASE` 只在启动时读一次成 `$OUT` 快照,id 清单与各席镜像全取自快照(原各席镜像各自再读活 `$CASE`,面板启动中 case 被编辑会让各席输入不一致、审计快照不再是「裁判当时读到的」);id 提取跳过围栏代码块(idea 正文引用 `## I<n>` 样例会生成幻影 id,verdict_ok 向每张票索要不存在的行、面板必败)——按 CommonMark 记围栏字符+长度的状态机:```` ``` ````/`~~~` 都算、容 ≤3 前导空格、关栏须同字符且长度 ≥ 开栏(裸 `!fence` 翻转会被 `~~~`、缩进栏、四内嵌三穿透出幻影 id;错翻反向还会吞真标题,票里冒「未知 id」同样面板必败,复测确认);反引号开栏行 info 串含反引号按行内代码不认栏;不写 `{0,3}` 区间,BSD awk 对 brace 区间支持不稳。
- 有意不改:id 来源收窄到 ideas.md `## I<n>` 单源(既定决策,注释已文档化);hunt.sh 与 awr-side 的 glob 计数惯用法保持各一份、仅统一 `[ -e ]` 守卫注释(两脚本独立,不值得为 5 行引依赖)。
- 验证:resolver 回归 15 例(`..` 开头/嵌中/绝对路径内/纯段、裸名遮蔽×可执行性、tab 切词、空串、相对/绝对/缺失)全过;假裁判端到端(含围栏样例的 case,2 席)ids 无幻影、快照落位、聚合正常;全部脚本 `bash -n` 过。

## 2026-07-10 grok 一等公民接入(hunt / AwR / calib 全链路)

- 新增 `grok-worker.sh`:grok 无头适配层,只收单一 prompt 参数拼 `grok … -p`(直接 `grok -p …` 会被 flags 吃值,裸 positional 无 TTY 挂;多参报错,防命令串夹带 flags 被静默吞掉用错配置);`--always-approve` + `--sandbox workspace` + `--no-subagents`,对 ledger.tsv、`tmp/ledger.good`(hunt 唯一可信基线,tmp/ 可写且在守卫视野外)、固定层、编排脚本、roles|calib|.claude 等发 file-tool Edit|Write 写禁(相对+绝对+`**/`glob 三套,仅相对挡不住绝对路径写);`GROK_REPO` 指定工作根(默认脚本所在目录);`GROK_DISABLE_WEB` 枚举校验(禁搜是安全开关,不 fail-open,认不出的值 exit 2);可调 GROK_MODEL/GROK_MAX_TURNS/GROK_SANDBOX/GROK_BIN。不用 `--disallowed-tools Agent`(grok 0.2.x session 构建期崩溃)。写界边界诚实记入头注:仅达文件写域,deny 挡 file tools 与可静态识别的 shell 写但间接写(python `open().write()` 实测)绕得过;sandbox workspace 不禁网/不禁进程(terminal 实测可 git/gh/curl 联网),且继承用户级 `~/.claude`+`~/.grok` 的 hooks/plugins/MCP——这些外部副作用无机械闸,靠 prompt 铁律,真要封须再包 OS 沙箱(本脚本不做)。
- hunt.sh:AGENT_CMD/FRONT_CMD/BACK_CMD/REV_CMD_N 均可指 `./grok-worker.sh`,grok 计入可信席位(与 claude/codex 同级)。
- awr-side.sh:两席可换 grok;自定义 SIDE_CMD 启动时一次性解析并验证(相对路径钉成真仓绝对路径,绝对路径同验 `-f`+`-x`,裸名优先真仓根同名文件、回退 PATH;失败立即退出——若留到调起时才失败会绕过 nofile 熔断,坏命令下无限空转;PANEL_CMD 同构同验),调起注入 `GROK_REPO=<镜像>`;沙箱预提示明文禁写 `~/.gemini`/`~/.claude`/`~/.codex`/`~/.grok`。
- calib/run_panel.sh 裁判改镜像执行:每席一次性镜像,bash 只拷回 `verdict.tsv`/`review.md`,真仓不作为裁判工作目录;镜像只隔 CWD,越界写由后端沙箱挡(无沙箱命令不得当 PANEL_CMD);禁搜机械层:grok 席自动注入 `GROK_DISABLE_WEB=1`(禁内建检索),claude 席镜像内写 calib 专用 settings(只许写 tmp/**,deny WebSearch/WebFetch——原样拷真仓 allowlist 会放行检索);OS 层不限网络,shell 侧禁搜靠 prompt 铁律+泄漏标记(聚合 `LC_ALL=C sort -u`,BSD sort 在 UTF-8 locale 下会把等长 CJK 标记吞成一条)。不同 case 面板可并行(镜像名按 case 名隔离清扫,`_→_u`/`.→_d` 单射编码防前缀 glob 误伤与 `foo.bar`/`foo_bar` 同名互扫)。曾试过的内容快照守卫被复现 fail-destructive(agent 删掉快照后,守卫把全部 tracked 文件当「快照外新增」逐个删),弃用。
- 票据硬校验:verdict.tsv 拷回即规范化(剥 CR、trim 字段——校验与聚合读同一份规范文本,否则校验层容忍的尾随空白在聚合层把合法票降成缺票 reject);裁判 rc=0 时逐 id 校验——每 id 恰好一行、枚举合法、无未知 id/重复,容忍 header/空行;不合格按裁判失败计、面板作废(否则坏裁判在阴性对照里以缺票/错 id 伪装成正确全 reject);id 清单单源取 `ideas.md` 的 `## I<n>`(= 发进镜像、裁判唯一所见),不另立 ideas.tsv 为源(否则裁判按 md 投的票会因 id 集不符被 verdict_ok 全判失败、面板必败)。裁判输入(ideas.md/priorwork.md)留一份审计快照到结果目录,镜像随 `rm -rf` 即弃后仍可还原「裁判当时读到什么」。泄漏标记聚合后全局打印一次(放 per-id 循环内会对每个 id 重复整份、把 1 条读成 N 条)。
- 验证:grok-worker 写界烟测(file-tool 写 ledger/roles 全 DENIED,`GROK_REPO` 钉根)、resolve 单测(相对/绝对/裸名 × 缺失/不可执行/合法)、假裁判七种产物形态、真 grok 禁搜端到端(direct-hit case 判 reject)。

## 2026-07-07 生成端转向命题式,查重端镜像命题占位

hunt 循环 3 天 0 SA:120 行判定死因≈50 次落在同一封顶模式——头条是「换轴近迁移/组合」(把机制 M 搬到域 D、或 A+B 拼接),novelty 落在可枚举的配对里,强查重必然找到近邻占位。而发散透镜池 11 条里 8 条是「换 X 轴」模板(`pick_lens` 均匀抽,换轴抽中率 8/14≈57%),正把生成推向被封顶的形状。命题式(把 novelty 放进一句关于世界的断言:某解释被推翻、某假设可删、某问题被命名)不直接落 M×D 网格、较难被单篇占位——ledger 里最不封顶的几条恰是「解释公认现象」型。

- `brainstorming_policy.md` 透镜池 11→6:8 条换轴塌成 1 条「换一条轴(慎用)」,抽中率 57%→11%;命题式起手式(解释公认现象/删承重假设/命名新问题/换评测对象)提为高权重条目,新增「命名一个真实但没名字的问题/被测量」;引言点明命题式 vs 换轴式两类。form#4「瓶颈定位实验」补 probe 天花板约束(纯诊断上限 borderline,冲 SA 须绑可修复臂或惊人发现)——从易失的 deathlist 固化进 policy;经典 CS 迁移形态标注默认增量。
- `roles/generate.md` 头条自测(落笔前每候选跑一遍):能写成 M×D/A+B 配对即近迁移、预期至多 AwR;改写成命题唯一算数的判据是逼出与最近邻不同的可证伪判别(落在最小否证实验信号上),句式改写不算——堵话术换壳;另加 estimand 对齐、诊断绑修复臂两条护栏。
- `roles/research.md` / `roles/prescreen.md`:direct-hit/三类词是配对取向,抓不到命题占位。增专搜——竞争解释是否已发表(含反向结论/相邻学科)、estimand/问题是否已命名、被点名靶子 limitation/ablation 是否自认(LAPA 自认潜动作编码相机运动、LDA 自认欧氏动作头瓶颈即此类)。prescreen 只做便宜的靶子一瞥、系统检索留深查,保住 fail-open 便宜性。
- `roles/review.md`:estimand 错位(判别信号≠命题声称的量)、纯诊断 probe 天花板从「通用严谨性隐式抓」升为点名检查项——生成端自评可话术化,真门仍在裁判。

## 2026-07-07 预筛判定行严格解析,堵宽松抽词误杀

codex 复审 fail-open 改动时指出:`prescreen_dec` 的 `grep -oE 'kill|keep'` 是子串抽词,`判定:not kill`/`判定:kill? keep`/`判定:killed` 都被抽成 kill,块内再有 API 记录+任一非 API 链接即按 reject+overlap=high 永久入账。存量问题(旧 `prescreen_ok` 同一解析),但 fail-open 契约已承诺「判定非法→keep」,解析器须兑现:

- 首条判定行整行严格匹配 `判定:kill|keep`(容忍空白与全/半角冒号)才算数,附加任何词视为非法→空→fail-open keep;含糊判定的代价方向从"可能永久误杀"变为"多花一次深查"。首行畸形不捡后面的严格行——畸形块直接 fail-open,比扫全块更保守。
- `roles/prescreen.md` 同步:判定行不得附加任何词,附加词=kill 白判。
- codex 二次复审补漏:落码时括号内全角冒号丢成 ASCII `[::]`,`判定：kill` 解析为空走 fail-open keep(方向安全,只多花深查);已改 `[:：]`,全/半角与附加词非法三组回归通过。当前纯理论敞口——模板与真实 prescreen 输出全是半角冒号。

## 2026-07-07 预筛结构失败 fail-open,不再废轮

采纳 codex 07-07 调研第 3 条。9fa98c8 只在 prompt 层修了挂后台导致 prescreen.md 不落盘的问题,orchestrator 层结构失败仍整轮作废——白扔已花的生成+透镜抽取。预筛定位"只杀不保"的纯省钱优化,不是正确性门槛,失败方向应是多花深查钱而非废轮:

- 删 `prescreen_ok`(任一 id 不达标即废轮),换 `kill_evidence`:只校验 kill 佐证(块内 ≥1 条结构化 API 检索记录 + 非 API 占位链接),通过才进 kills.tsv 按 reject 入账;佐证不全降级 keep——kill 是永久入账(overlap=high),幻觉/缺失链接不得污染 ledger。
- prescreen.md 缺失/为空、判定缺失/非法:fail-open 按 keep 进优先级 shortlist,由深查重+裁判+SA 硬门槛兜底。调起 rc≠0 仍走 fail_and_wait——后端系统性故障,fail-open 只会让下一阶段(同一 FRONT_CMD)接着失败。
- fail-open 逐 id 记 hunt.log,并写 metrics(outcome=failopen),防预筛系统性坏掉被兜底掩盖。`roles/prescreen.md` 契约同步:「不达标整轮作废」→「无效 kill 白判、fail-open 全 keep」。

## 2026-07-07 预筛 shortlist 优先级选取 + 轮级机器可读指标

采纳 codex 07-07 调研的两条建议(候选调度与可观测性;安全边界不动):

- shortlist 弃 FIFO:预筛 keep 先写 `tmp/round/keeps.tsv`(rank、主题存量、生成序),`select_shortlist` 排序取前 `SHORT_MAX` 个——`keep_rank` 复查/进化(0)> 删承重假设块(1)> 普通(2),同 rank 按 ledger 同主题行数升序、再按生成序;溢出 keep 照旧丢弃不入账。FIFO 按生成顺序取,已把排位靠后的稀缺候选丢掉 51 次(hunt.log;含中断遗留一轮的复查候选 I6,该轮在新逻辑下选出 I6/I1/I3——复查、删公理、存量 2 的人机交互与部署)。
- 轮级指标 append-only `tmp/hunt.metrics.tsv`:阶段异常(fail)/空产出作废(empty)/聚合定谳(verdict)各追加一行——round、stage、lens、gen/kill/keep/short 计数(由 tmp/round 文件即时派生,drop=keep-short)、pw_links/pw_api、verdicts 每 idea 票串 `id=r1,r2,r3->终判`(2=SA 1=aWr 0=rej -=缺票;全 2 却 ->reject 即 SA 硬门槛降级)。诊断 84/107 accept-w-rev 卡在 novelty 封顶还是查重薄弱,不再翻 hunt.log/ledger prose。
- 踩坑:BSD awk/sort/uniq 字符串比较走 strcoll,en_US.UTF-8 下纯 CJK 串互判相等(`awk '$3=="效率与系统"'` 能命中「动作表征」行),主题存量计数改 `grep -Fxc` 字节比较。既有代码未踩坑(themes_ok 用 awk 数组下标,哈希字节精确);CJK 等值判断避开 awk `==` 与默认 locale 的 sort/uniq。

## 2026-07-06 预筛铁律:一次性调用禁挂后台 + 限流有界重试

当晚两轮预筛空产出同因:代理把 API 检索挂后台、结束回复"等通知",而 `claude -p` 回复结束进程即退,`prescreen.md` 永不落盘(hunt.log 22:07/22:46,烧掉 3 次短重试中的 2 次)。

- `roles/prescreen.md` 铁律新增首条:一次性调用,禁挂后台/等回调;遇 API 限流换另一家 API 或 `sleep 10` 重试,每 idea 合计至多 2 次,仍失败则记录已发出的 query URL、判 keep 不再等待(与「拿不准一律 keep」同向,机械门槛只查 URL 模式,此路合规);`prescreen.md` 必须在回复结束前落盘。
- `.claude/settings.json` 放行精确命令 `Bash(sleep 10)`:单次等待在权限层钉死 10 秒,限流路径整体有界,不依赖 run_stage 加超时。

## 2026-07-06 发散透镜扩池 + 空白牌 + 词表加「人机交互与部署」

依据 2025-26 顶会获奖创新盘点(CoRL 2025 UniFP/Fabrica、RSS 2025 FEAST、NeurIPS 2025 best papers、ICRA 2026 finalists):原 8 条透镜全是「换元件」型动作,覆盖不到统一/闭环/极端规模/机制解释这几类获奖创新。

- `brainstorming_policy.md` 透镜池 8→11:新增「换输出表征」「统一或拆分」「闭环与经验」;「换失败假设」放宽为「解释公认现象」(失败/成功/scaling 曲线/涌现行为);「换算力约束」改双向「换规模轴」(砍量级或推高量级);「换时间尺度」补记忆与上下文长度;「换评测对象」补混杂变量。透镜定位明确为起手式非硬约束,贴合度不进机械校验。
- 抽签池加 3 张空白牌:`hunt.sh` `pick_lens` 按 total+3 抽签,抽中空白牌不注入、log 标注,本轮自由发散。
- 主题词表加「人机交互与部署」(FEAST 类工作原先无家可归);新主题存量 0,会被反坍缩规则优先覆盖,属预期。`themes_ok` 动态解析词表,无需改码。README 头注同步。

## 2026-07-06 AwR sidecar 多后端 + 改名 awr-side.sh

- `agy-side.sh` → `awr-side.sh`:研究员/裁判两席可接 claude/codex——`SIDE_CMD` 两席统一覆盖,`SIDE_RESEARCH_CMD`/`SIDE_JUDGE_CMD` 分席覆盖(与 hunt.sh `AGENT_CMD` 同约定,claude 席同样 `--strict-mcp-config`,codex 席示例加 `--skip-git-repo-check --ephemeral` 适配无 `.git` 镜像),不设时仍为内置 agy、行为不变。沙箱镜像对所有后端保留,并拷入 `.claude/` 供 claude 席 allowlist;启动闸门只罩 agy 席(专治连发触发登录验证),claude/codex 直起、不动共享戳;机械校验/`.badN`/熔断对所有后端一视同仁。
- 环境变量 `AGY_SIDE_*` → `SIDE_*`(`AGY_MODEL`/`AGY_PRINT_TIMEOUT` 保留,仅内置 agy);状态目录 `tmp/agy-side/` → `tmp/awr-side/`,启动时自动整体迁移,队列状态无损续跑;实例锁改 `tmp/awr-side.lock`。README/roles 头注同步。

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
