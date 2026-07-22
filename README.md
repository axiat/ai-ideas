# ai-ideas

具身智能(WorldModel & VLA)idea 调研回路。仿 [karpathy/autoresearch](https://github.com/karpathy/autoresearch) 的分层:固定的回路协议 + 薄入口 + 实验台账。反串通:生成、查重、打分是互不共享 context 的独立进程,verdict 由 orchestrator 聚合,单个 agent 改不了判定。

开发规划见 [`DEVELOPMENT.md`](DEVELOPMENT.md)。

固定层(人改,agent 只读):

- `PROGRAM.md` — 回路协议与不动项
- `rubric.md` — idea 评审标准(整合自 idea-evaluator skill)
- `brainstorming_policy.md` — 发散规则与 verdict 校准
- `research_context.md` — 研究背景,可选灵感

角色层(人改,agent 按其执行,prompt 在 `roles/`):

- `generate.md` — 只生成 idea,不查重、不打分;标注主题、附最小否证实验(点名最强基线);可含至多 1 个进化/复查版(父本限 overlap=low 的实验设计类 AwR / 查重薄弱型 AwR)
- `prescreen.md` — 廉价预筛,只杀"单篇直接占据头条"的 direct hit(kill 必附链接),keep 不构成 novelty 结论;被杀者由 orchestrator 按 reject 入账;结构失败/kill 佐证不全 fail-open 按 keep,不废轮
- `research.md` — 对抗式深查重,先 direct-hit 猎杀,每个 idea 实读 5-8 篇、附"最强反例"行;须留 ≥1 条可复现的 arXiv/Semantic Scholar API 检索记录
- `review.md` — 打分裁判,默认 Reject,跑多次取最低票;feasibility 只认最小否证实验
- `report.md` — 纯组装报告,不改判
- `meta.md` — 失败蒸馏,读 ledger 的 reject+AwR 行,归纳致命模式/封顶模式/进化候选到 `tmp/deathlist.md`

入口层:

- `hunt.md` — 主动挂机,`hunt.sh` 驱动的多进程流水线,循环到当日累计 `SA_TARGET`(默认 1)个全票 Strong Accept
- `trigger.md` — 每周定时 cloud routine("Weekly Embodied Idea Scout",远端 prompt 与此同步;单 agent,见下方 caveat)

产出层:

- `ledger.tsv` — 所有生成过的 idea 台账(含被拒的),跨轮查重依据;**由 orchestrator 写,agent 不碰**
- `ideas/` — 达标报告(`YYYY-MM-DD_weekly_ideas.md` / `YYYY-MM-DD_hunt.md`)

## 用法

**主动找 idea(挂机)**:`./hunt.sh`。每轮把一批 idea 走完「生成 → 独立排序 → 预筛(杀 direct hit)→ 对抗式深查重 → 打分 ×N」,脚本自身聚合 verdict(取最低票,Strong Accept 需全票)、写 ledger(含查重 overlap 列、非 SA category 列)、发报告。当日 SA 累计达 `SA_TARGET`(默认 1;`0` 不设上限)则发布即退,达标轮未凑满目标则发布后继续攒(同日报告加 `-2`/`-3` 后缀,幂等追加进同一当日分支与 PR);异常退出默认冷却 150 分钟;前段空产出或查重结构不达标先短重试,连续 `EMPTY_MAX` 次后才升级长冷却;正常无 SA 默认随机 1-8 分钟后重试——状态在 `ledger.tsv`,新会话不重做已评审的 idea。中断随时安全:`tmp/hunt.lock` 实例锁防同目录双开(陈旧锁自清);重启按 ledger 当日 SA 计数判断,已达标先跑幂等的 `publish.sh` 补发布再退,未达标但已有当日报告则补发布后继续;中断遗留的前段产物过机械门槛则首轮自动续跑(跳过生成/预筛/查重,裁判一律重跑,`RESUME_FRONT=0` 关闭)。


```bash
# 最常用：agy 跑前段(生成+调研)+ 两个 review 席位(REV_CMD_2/3);codex 跑 REV_CMD_1(未设,回落 BACK_CMD)与报告。
# agy 席位由启动闸门(AGY_LAUNCH_GAP_SEC,默认 60s)自动错峰,防快速重复调起触发登录验证。
FRONT_CMD='./agy-worker.sh' \
BACK_CMD='codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write' \
REV_CMD_2='./agy-worker.sh' \
REV_CMD_3='./agy-worker.sh' \
REV_STAGGER_SEC=15 \
./hunt.sh

./hunt.sh                 
# 默认参数
#  agent: claude -p --strict-mcp-config(零 MCP:不带用户级 lark/连接器,省启动与健康检查,凭据不进无关进程)
#  裁判数: REVIEWERS=3
#  SA 实读门槛: MIN_READ=5
#  预筛存活上限: SHORT_MAX=3(按优先级取:复查/进化>删公理>低存量主题>生成序;超额 keep 不深查、不入账;kill 立即按 reject 入账 overlap=high)
#  预筛 fail-open: prescreen.md 缺失/判定非法/kill 佐证不全按 keep 兜底,不废轮(调起 rc≠0 仍走异常重试)
#  异常冷却: 150 分钟
#  正常跑完但无 SA: 随机等待 1-8 分钟后重试
#  前段空产出上限: EMPTY_MAX=3(预筛全灭同样走空产出短重试)
#  查重链接门槛: PRIOR_MIN_LINKS=5
#  查重 API 记录门槛: PRIOR_MIN_API=1(0 关闭;近邻链接与 API 记录分开计数)
#  主题门槛: theme 须属 policy 词表,且 ≥THEME_MIN_LOW=2 个 idea 落在低存量主题(0 关闭分布校验;查的是预筛前的发散全集)
#  失败蒸馏: 每 META_EVERY=6 轮、reject+AwR 行 ≥ META_MIN_REJECTS=5 时刷新 tmp/deathlist.md
#  发散透镜: 每轮从 brainstorming_policy.md「发散透镜」小节随机抽一条注入生成 prompt(池含 3 张空白牌,抽中不注入、自由发散)
#  前段续跑: RESUME_FRONT=1(0 关闭;遗留前段产物过门槛则首轮跳过生成/预筛/查重,verdict 永不续用)
#  实例锁: tmp/hunt.lock,双开自动退出,持锁进程已死则自清重抢
#  连续异常上限: MAX_FAILS=12
#  轮级指标: 追加写 tmp/hunt.metrics.tsv(fail/empty/verdict 各一行:计数列+每 idea 票串+run_id,调参不翻 hunt.log)
#  按运行归档: 每轮 run_id(时间+pid+轮次),轮终点把 tmp/round 全量产物(ideas/priorwork/三席票据与
#             完整评审/逐阶段日志与起止时间)+ manifest(backend/policy 版本/退出原因)+ ledger 增量
#             固化到 tmp/runs/<run_id>/;任一 ledger 结论可由归档还原输入与判定过程
#  达标轮: 写报告、发布 PR;当日 SA 累计达 SA_TARGET=1 退出


REVIEWERS=5 ./hunt.sh     # 加严:5 位裁判,仍取最低票
SA_TARGET=3 ./hunt.sh     # 当日攒满 3 个全票 SA 才停;SA_TARGET=0 不设上限,Ctrl-C 手动停
./hunt.sh 30              # 异常冷却改 30 分钟;正常无 SA 仍随机 1-8 分钟
NO_HIT_SLEEP_MIN_LO=1 NO_HIT_SLEEP_MIN_HI=8 ./hunt.sh
ALLOW_ZERO_NO_HIT_SLEEP=1 NO_HIT_SLEEP_MIN_LO=0 NO_HIT_SLEEP_MIN_HI=0 ./hunt.sh  # 测试用
AGENT_CMD='codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write' ./hunt.sh
AGENT_CMD='./grok-worker.sh' ./hunt.sh   # grok 全链路;须走 worker(见 grok-worker.sh 头注),不可直接 grok -p …

# agy + claude:agy 只跑生成+查重;claude 跑打分+报告,publish 仍由 hunt.sh 调 publish.sh
# 不要把 3 个 reviewer 全交给 agy;须留至少 1 个可信席位
FRONT_CMD='./agy-worker.sh' BACK_CMD='claude -p --strict-mcp-config' ./hunt.sh

# agy + codex:agy 只跑生成+查重;codex 跑打分+报告
FRONT_CMD='./agy-worker.sh' BACK_CMD='codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write' ./hunt.sh

# agy + grok:agy 只跑生成+查重;grok 跑打分+报告
FRONT_CMD='./agy-worker.sh' BACK_CMD='./grok-worker.sh' ./hunt.sh

# 混合 reviewer:每席可单独指定;保留至少 1 个可信席位(claude/codex/grok),agy 席位错峰启动
REV_CMD_1='codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write' \
REV_CMD_2='./grok-worker.sh' REV_CMD_3='./agy-worker.sh' REV_STAGGER_SEC=15 ./hunt.sh

# agy 前段可调,默认 AGY_MODEL='gemini-3.6-flash-high'(使用 `agy models` 打印的完整 model ID),AGY_PRINT_TIMEOUT=8m,
# AGY_LAUNCH_GAP_SEC=60(两次 agy 启动的最小间隔秒数,防快速重复调起触发登录验证;0 关闭)
AGY_MODEL='gemini-3.6-flash-high' AGY_PRINT_TIMEOUT=10m AGY_LAUNCH_GAP_SEC=90 FRONT_CMD='./agy-worker.sh' BACK_CMD='claude -p --strict-mcp-config' ./hunt.sh

# grok 可调:GROK_MODEL(默认 grok-4.5) GROK_MAX_TURNS(默认 80) GROK_SANDBOX(默认 workspace;off 关闭)
# GROK_DISABLE_WEB=1 禁内建检索(校准面板用)
GROK_MODEL=grok-4.5 GROK_MAX_TURNS=100 FRONT_CMD='./agy-worker.sh' BACK_CMD='./grok-worker.sh' ./hunt.sh
```

报告在 `ideas/YYYY-MM-DD_hunt.md`,以 `hunt/日期` 分支 PR 提交,CI 按路径自动合并;本地收尾用 `./settle.sh`(合并后切回 main、清理本地/远程特性分支)。

**AwR 复活 sidecar(可选,与 hunt.sh 并行)**:`./awr-side.sh`。主环之外用多轮 agent(默认 agy,便宜可错;两席可换 claude/codex/grok)把 ledger 里 accept-w-rev 的 idea 磨成可复审成品:研究员按 reason 点名缺口检索出修订稿(`roles/awr.md`)→ 裁判按 rubric 判 `SA-可能/还不行`(`roles/awr-judge.md`,失败关闭)→ 还不行则缺陷回灌任务文件、下轮在旧稿上继续改;`SIDE_MAX_ROUNDS`(默认 3)轮反馈用尽带最后修订稿收尾。产物只落 `tmp/awr-side/awr/`(gitignored,主环守卫不可见),verdict/ledger/ideas 一概不碰;agent 每次调起只见 `tmp/awr-side/run.*` 临时镜像,指定输出文件由 bash 拷回,镜像隔文件树/CWD、不隔网/进程(agy 实测不守 prompt 写界;镜像内含 `.claude/` 供 claude 席 allowlist,codex 席 workspace 边界即镜像,grok 席由 `GROK_REPO`+sandbox 钉在镜像,其写域边界与已知绕过见 `grok-worker.sh` 头注)。早停/糊弄由机械校验兜住:修订稿须含「## 修订版 idea」节 + ≥3 条带 URL 检索记录 + 末行 `AGY-DONE`,判定须二选一且"还不行"附具体缺陷;不合格存 `.badN` 重跑,同 key 累计 3 次拉黑(删 `.badN` 解除)——对所有后端一视同仁。agy 席与 `agy-worker.sh` 共享启动闸门戳,默认间隔 `SIDE_GAP_SEC=120`,防连发触发登录验证;claude/codex/grok 席不走闸门。每 key 状态全由文件派生,无状态文件,中断随便杀。注意:主环运行中往仓库加任何未跟踪文件(tmp/ 之外)会触固定层守卫停机——sidecar 自身文件均已入库,产物全在 tmp/ 下,不会触。

```bash
caffeinate -is ./awr-side.sh        # 常驻:队列全终态后每 SIDE_POLL_SEC=600 秒重扫,等主环产新 AwR
SIDE_POLL_SEC=0 ./awr-side.sh       # 单遍:队列全终态即退
# 接入 claude/codex/grok(与 AGENT_CMD 同约定;SIDE_RESEARCH_CMD/SIDE_JUDGE_CMD 分席覆盖,不设回落 SIDE_CMD,再回落内置 agy):
SIDE_JUDGE_CMD='claude -p --strict-mcp-config' ./awr-side.sh   # agy 研究(便宜可错)+ claude 裁判(可信),推荐
SIDE_CMD='claude -p --strict-mcp-config' ./awr-side.sh         # 两席全 claude(不加载 MCP)
SIDE_CMD='codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write --skip-git-repo-check --ephemeral' ./awr-side.sh
SIDE_CMD='./grok-worker.sh' ./awr-side.sh                      # 两席全 grok
SIDE_JUDGE_CMD='./grok-worker.sh' ./awr-side.sh                # agy 研究 + grok 裁判
# 内置 agy 换模型:AGY_MODEL 使用 `agy models` 打印的完整 model ID
AGY_MODEL='claude-sonnet-4-6' caffeinate -is ./awr-side.sh
AGY_MODEL='claude-opus-4-6-thinking' caffeinate -is ./awr-side.sh
# 其余可调:AGY_PRINT_TIMEOUT=10m(仅内置 agy) SIDE_MAX_BAD=3 SIDE_MAX_ROUNDS=3
```

跑完后按序看:

```bash
grep -h '^- 状态' tmp/awr-side/awr/*.md | sort | uniq -c   # 总览:达标/未达标各多少
grep -l '状态: 达标' tmp/awr-side/awr/*.md                 # 给 claude 复审的候选
ls tmp/awr-side/awr/*.bad3 2>/dev/null                     # 拉黑名单(3 次机械作废)
grep 作废 tmp/awr-side/awr/side.log                        # 死法分布:早停多→任务再切小;检索不足多→查 .agy.log
```

达标品人工核三点再上贵模型:抽点 2-3 条 URL 确认真是所称论文且"占据/部分重叠"标注没胡说;修订版没被改空泛或面目全非;裁判意见有具体核对痕迹而非空洞放行。校准信号:第一轮达标率过半→裁判太松,把 brainstorming_policy.md 评审校准节单独摘给它;全军未达标→看几份 `<key>.task.md` 的反馈史,分辨裁判太严还是研究员补不动。成品不自动回灌主环——原始 AwR 仍在 ledger/deathlist 里,generate 会避开该方向;claude 认可后怎么用(手动成文进 ideas/ 或当下轮生成素材)是人工动作。

**领域近作监视(litwatch,手动 / 可选常驻)**:`./litwatch.sh`。免费 agy 额度的落点——回路外独立进程,把领域近作预取成本地缓存供 `research.md` 查重当近邻种子;不进 hunt 主回路,agy 挂掉或缓存缺失则查重与无缓存完全一致(零回归)。取数默认走 arXiv **OAI-PMH**(批量抓不限流)+ 本地类别/关键词过滤;agy 只在 `tmp/litwatch/agy/` 沙箱里给已取到的 record 打近邻标注(`roles/litwatch.md`;id 必须实来自 staging,越界/坏行经 `ingest` 丢弃记 `drops.jsonl`——agy 塞不进假论文),产物落 `tmp/litwatch/index.jsonl`(gitignored)。真机验过:抓 2600 篇 cs → 83 篇相关近作,真 agy 产 7 条高质量标注。手动随时跑,未接 cron。

```bash
./litwatch.sh                              # 默认:OAI 近 4 天 cs,类别+关键词过滤,再真 agy 标注
LITWATCH_NO_AGY=1 ./litwatch.sh            # 只建确定性缓存,不调 agy
LITWATCH_OAI_DAYS=7 LITWATCH_OAI_MAXPAGES=12 ./litwatch.sh    # 更宽窗口
LITWATCH_THEMES_FILE=my.txt ./litwatch.sh  # 覆盖默认主题(oai:行内 |-关键词组)
# 备选源:S2(相关性更强,需 key)/ arXiv search API(已知限流,不推荐)
LITWATCH_SOURCES="oai s2" LITWATCH_S2_KEY=<key> ./litwatch.sh
# 挂前台常驻:每 6h 刷一遍(Ctrl-C 停),caffeinate -is 防笔记本休眠打断
caffeinate -is env LITWATCH_LOOP_SEC=21600 ./litwatch.sh
```

看缓存:`tmp/litwatch/index.jsonl`(每行 `{id,title,abstract,url,date,theme,agy_note}`);越界标注在 `tmp/litwatch/drops.jsonl`。可调项(`LITWATCH_OAI_DAYS/SETS/MAXPAGES/CATS`、`LITWATCH_SORT` 等)见 `litwatch.sh` 头注。

**每周定时**:cloud routine "Weekly Embodied Idea Scout" 自动运行,远端 prompt 即 `trigger.md`;报告经 `./publish.sh weekly` 以 `weekly/日期` 分支 PR 提交。改 `trigger.md` 需手动同步远端 routine;改固定层/角色层无需同步,routine 每次读仓库最新版。**caveat**:cloud routine 是**单个 agent**,拿不到 `hunt.sh` 的进程级隔离。`trigger.md` 已把 hunt 中纯 bash 的那部分严格度写成自律纪律逼近——分阶段执行、默认 Reject、对抗查重、三遍取最低、SA 硬门槛自检——但生成/查重/打分终究同一 context,严格度弱于 hunt 的独立裁判。**权限**:weekly 单 agent 要自己记账 + 跑 `publish.sh`(内含 git/gh),这些全在 `.claude/settings.json` allowlist 之外——allowlist 只约束 hunt.sh 统率的**本地受控 agent**,不绑云端 routine。故 weekly 只能以 **full-access(skip-permissions)** 运行(若被 allowlist 绑住会连 publish 都做不了);其越权风险由 `publish.sh` 硬限提交范围(`ideas/` 与 `ledger.tsv`)+ CI 路径守卫 + pre-push 拒直推 main 兜底,而非 `ledger.good` 快照。要完全对齐 hunt,把 weekly 改跑本地一次性流水线(同一条 bash 路径,`source=weekly`)即可。

## verdict 完整性(反串通)

- 生成 / 查重 / 打分是独立进程,裁判看不到生成方自评,也不知道停机条件——没有灌水动机。裁判**并行 + 各用独立输入目录**,开跑时看不到彼此产出。
- novelty 只认独立查重进程产出的证据,不认生成方"没人做"的自述。
- verdict、ledger、publish 全由 `hunt.sh` 决定:每个 idea 取 N 位裁判**最低**票,SA 需全票,缺/坏票当 reject。
- **SA 硬门槛**:全票 SA 还须过 orchestrator 校验——该 idea 有查重块、实读篇数 ≥ `MIN_READ`(默认 5)、附最小否证实验、每位裁判都写了完整评审,删承重假设形态另须「裂缝证据核验」≥2 条相符(`AXIOM_MIN_CRACKS`);缺任一则硬降级 reject。
- ledger 以 `tmp/ledger.good` 为单一可信基线,启动时取当前工作树 `ledger.tsv` 作为人工基线,之后只被 bash 聚合更新;任何 agent 擅改(含中途失败轮的残留)在下一轮开局被抹掉。
- **中断续跑不放水**:重启只沿用过机械门槛的前段产物(它们本就是 agent 产物,由门槛+裁判消化);遗留的评审票据与评审块一律清除、裁判由 orchestrator 重新调起——防前段借崩溃伪造整轮票据绕过独立评审。
- **分阶段守卫**:生成/查重/评审阶段禁写 `ideas/`(防伪造达标报告绕过全票),仅 report 阶段可写。

## 无人值守的四层保证

1. **工具策略**:claude 走 `.claude/settings.json` allowlist——只放行写 `ideas/` 与草稿区(仓库内 `tmp/`、系统 `/tmp`),WebSearch/WebFetch,无参 ls/date;`ledger.tsv` 写权与 publish/git/gh 都不给 agent(orchestrator 独占)。未匹配操作在无头模式下自动拒绝(前提:本仓库已 trust)。codex 走 OS 级 sandbox(`-s workspace-write` 写限仓库、`-c approval_policy=never`、`-c sandbox_workspace_write.network_access=true` 放行网络、`--search`;`codex exec` 须用 `-c approval_policy=` 而非 `-a`);codex 沙箱无细粒度写控,ledger 完整性靠上面的快照-还原兜底。grok 必须经 `./grok-worker.sh`(不可直接 `grok -p`):OS sandbox 写限工作根 + 对 ledger/固定层的 file-tool 写禁,shell 越界写同 codex 靠快照兜底;边界只到文件写域——网络/进程不禁、间接写(python 等)绕得过 deny、且继承用户级 hooks/plugins/MCP,这些外部副作用靠 prompt 铁律非机械闸,参数与坑位详见其头注。校准裁判由 `run_panel.sh` 镜像隔离(每席一次性镜像,只拷回 verdict/review;越界写靠后端沙箱挡,无沙箱命令不要当 PANEL_CMD)。agy(前段 `FRONT_CMD`)既无 allowlist 也无 OS sandbox,其 CLI sandbox 可读写 `$HOME`,不能当边界;故只用于生成/查重这类**可错**的上游——靠回路守卫(§3)回滚越界仓库改动、且它绝不碰 verdict/ledger/publish 兜底,错误 idea 由下游独立裁判毙掉。它对 `$HOME` 的越界写在守卫视野外,但不影响判定与发布产物。
2. **push 守卫**:发布只经 `./publish.sh`——add 范围硬编码为 `ideas/` 与 `ledger.tsv`,走 `hunt/当日` 分支 PR;幂等可重跑,commit 后中断可补推送/补 PR。`.githooks/pre-push` 拒直推 main(人工覆盖 `ALLOW_MAIN_PUSH=1`)。GitHub 端未启用 branch protection,远端 main 无服务端防线。
3. **回路守卫**:`hunt.sh` 每次调 agent 后按阶段校验已跟踪改动——生成/查重/评审阶段只允许 `ledger.tsv`(且靠 ledger.good 兜底),report 阶段才放行 `ideas/`;越界未提交改动自动回滚,越界已提交/未跟踪文件停机留人工。日志 `hunt.log`(gitignored),连续异常达 `MAX_FAILS`(默认 12)即停。
4. **CI 守卫**:auto-merge workflow 只按路径判定——本仓库分支的 PR 改动全落在 `ideas/**` 与 `ledger.tsv` 才自动合并,越界跳过并留言,固定层改动必须人工 merge。

**看结果**:达标 idea 在 `ideas/` 报告正文(仅 Strong Accept,附评审表与查重记录);历史上所有想过的 idea(含被拒的及拒因)在 `ledger.tsv`。

**调 harness**:评审严格度 → `brainstorming_policy.md`(尺度)/ `REVIEWERS`(票数)/ `roles/review.md`(裁判铁律);发散方向 → `brainstorming_policy.md`;回路规则 → `PROGRAM.md`;各角色行为 → `roles/`;单次运行的范围/停机/输出 → 对应入口文件。
