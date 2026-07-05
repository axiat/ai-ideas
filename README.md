# ai-ideas

具身智能(WorldModel & VLA)idea 调研回路。仿 [karpathy/autoresearch](https://github.com/karpathy/autoresearch) 的分层:固定的回路协议 + 薄入口 + 实验台账。反串通:生成、查重、打分是互不共享 context 的独立进程,verdict 由 orchestrator 聚合,单个 agent 改不了判定。

固定层(人改,agent 只读):

- `PROGRAM.md` — 回路协议与不动项
- `rubric.md` — idea 评审标准(整合自 idea-evaluator skill)
- `brainstorming_policy.md` — 发散规则与 verdict 校准
- `research_context.md` — 研究背景,可选灵感

角色层(人改,agent 按其执行,prompt 在 `roles/`):

- `generate.md` — 只生成 idea,不查重、不打分;标注主题、附最小否证实验(点名最强基线);可含至多 1 个进化/复查版(父本限 overlap=low 的实验设计类 AwR / 查重薄弱型 AwR)
- `prescreen.md` — 廉价预筛,只杀"单篇直接占据头条"的 direct hit(kill 必附链接),keep 不构成 novelty 结论;被杀者由 orchestrator 按 reject 入账
- `research.md` — 对抗式深查重,先 direct-hit 猎杀,每个 idea 实读 5-8 篇、附"最强反例"行;须留 ≥1 条可复现的 arXiv/Semantic Scholar API 检索记录
- `review.md` — 打分裁判,默认 Reject,跑多次取最低票;feasibility 只认最小否证实验
- `report.md` — 纯组装报告,不改判
- `meta.md` — 失败蒸馏,读 ledger 的 reject+AwR 行,归纳致命模式/封顶模式/进化候选到 `tmp/deathlist.md`

入口层:

- `hunt.md` — 主动挂机,`hunt.sh` 驱动的多进程流水线,循环到 1 个全票 Strong Accept
- `trigger.md` — 每周定时 cloud routine("Weekly Embodied Idea Scout",远端 prompt 与此同步;单 agent,见下方 caveat)

产出层:

- `ledger.tsv` — 所有生成过的 idea 台账(含被拒的),跨轮查重依据;**由 orchestrator 写,agent 不碰**
- `ideas/` — 达标报告(`YYYY-MM-DD_weekly_ideas.md` / `YYYY-MM-DD_hunt.md`)

## 用法

**主动找 idea(挂机)**:`./hunt.sh`。每轮把一批 idea 走完「生成 → 预筛(杀 direct hit)→ 对抗式深查重 → 打分 ×N」,脚本自身聚合 verdict(取最低票,Strong Accept 需全票)、写 ledger(含查重 overlap 列)、发报告。首轮就出全票 SA 则发布即退;异常退出默认冷却 150 分钟;前段空产出或查重结构不达标先短重试,连续 `EMPTY_MAX` 次后才升级长冷却;正常无 SA 默认随机 1-8 分钟后重试——状态在 `ledger.tsv`,新会话不重做已评审的 idea。中断随时安全:`tmp/hunt.lock` 实例锁防同目录双开(陈旧锁自清);重启时当日报告已存在则先跑幂等的 `publish.sh` 补发布再退;中断遗留的前段产物过机械门槛则首轮自动续跑(跳过生成/查重,裁判一律重跑,`RESUME_FRONT=0` 关闭)。


```bash
# 最常用：agy 负责产生 idea + 调研 + 占据两个 review 席位、codex 负责最后一个 review 席位 + publish 把关
FRONT_CMD='./agy-worker.sh' \
BACK_CMD='codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write' \
REV_CMD_2='./agy-worker.sh' \
REV_CMD_3='./agy-worker.sh' \
REV_STAGGER_SEC=15 \
./hunt.sh

./hunt.sh                 
# 默认参数
#  agent: claude -p
#  裁判数: REVIEWERS=3
#  SA 实读门槛: MIN_READ=5
#  预筛存活上限: SHORT_MAX=3(超额 keep 不深查、不入账;kill 立即按 reject 入账 overlap=high)
#  异常冷却: 150 分钟
#  正常跑完但无 SA: 随机等待 1-8 分钟后重试
#  前段空产出上限: EMPTY_MAX=3(预筛全灭同样走空产出短重试)
#  查重链接门槛: PRIOR_MIN_LINKS=5
#  查重 API 记录门槛: PRIOR_MIN_API=1(0 关闭;近邻链接与 API 记录分开计数)
#  主题门槛: theme 须属 policy 词表,且 ≥THEME_MIN_LOW=2 个 idea 落在低存量主题(0 关闭分布校验;查的是预筛前的发散全集)
#  失败蒸馏: 每 META_EVERY=6 轮、reject+AwR 行 ≥ META_MIN_REJECTS=5 时刷新 tmp/deathlist.md
#  发散透镜: 每轮从 brainstorming_policy.md「发散透镜」小节随机抽一条注入生成 prompt
#  前段续跑: RESUME_FRONT=1(0 关闭;遗留前段产物过门槛则首轮跳过生成/预筛/查重,verdict 永不续用)
#  实例锁: tmp/hunt.lock,双开自动退出,持锁进程已死则自清重抢
#  连续异常上限: MAX_FAILS=12
#  有至少 1 个 Strong Accept: 写报告、发布 PR、退出


REVIEWERS=5 ./hunt.sh     # 加严:5 位裁判,仍取最低票
./hunt.sh 30              # 异常冷却改 30 分钟;正常无 SA 仍随机 1-8 分钟
NO_HIT_SLEEP_MIN_LO=1 NO_HIT_SLEEP_MIN_HI=8 ./hunt.sh
ALLOW_ZERO_NO_HIT_SLEEP=1 NO_HIT_SLEEP_MIN_LO=0 NO_HIT_SLEEP_MIN_HI=0 ./hunt.sh  # 测试用
AGENT_CMD='codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write' ./hunt.sh

# agy + claude:agy 只跑生成+查重;claude 跑打分+报告,publish 仍由 hunt.sh 调 publish.sh
# 不要把 3 个 reviewer 全交给 agy;并发认证会失败
FRONT_CMD='./agy-worker.sh' BACK_CMD='claude -p' ./hunt.sh

# agy + codex:agy 只跑生成+查重;codex 跑打分+报告
FRONT_CMD='./agy-worker.sh' BACK_CMD='codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write' ./hunt.sh

# 混合 reviewer:每席可单独指定;保留至少 1 个可信席位,agy 席位错峰启动
REV_CMD_1='codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write' \
REV_CMD_2='claude -p' REV_CMD_3='./agy-worker.sh' REV_STAGGER_SEC=15 ./hunt.sh

# agy 前段可调,默认 AGY_MODEL=gemini-3.5-flash-high,AGY_PRINT_TIMEOUT=8m
AGY_MODEL=gemini-3.5-flash-high AGY_PRINT_TIMEOUT=10m FRONT_CMD='./agy-worker.sh' BACK_CMD='claude -p' ./hunt.sh
```

报告在 `ideas/YYYY-MM-DD_hunt.md`,以 `hunt/日期` 分支 PR 提交,CI 按路径自动合并;本地收尾用 `./settle.sh`(合并后切回 main、清理本地/远程特性分支)。

**每周定时**:cloud routine "Weekly Embodied Idea Scout" 自动运行,远端 prompt 即 `trigger.md`;报告经 `./publish.sh weekly` 以 `weekly/日期` 分支 PR 提交。改 `trigger.md` 需手动同步远端 routine;改固定层/角色层无需同步,routine 每次读仓库最新版。**caveat**:cloud routine 是**单个 agent**,拿不到 `hunt.sh` 的进程级隔离。`trigger.md` 已把 hunt 中纯 bash 的那部分严格度写成自律纪律逼近——分阶段执行、默认 Reject、对抗查重、三遍取最低、SA 硬门槛自检——但生成/查重/打分终究同一 context,严格度弱于 hunt 的独立裁判。**权限**:weekly 单 agent 要自己记账 + 跑 `publish.sh`(内含 git/gh),这些全在 `.claude/settings.json` allowlist 之外——allowlist 只约束 hunt.sh 统率的**本地受控 agent**,不绑云端 routine。故 weekly 只能以 **full-access(skip-permissions)** 运行(若被 allowlist 绑住会连 publish 都做不了);其越权风险由 `publish.sh` 硬限提交范围(`ideas/` 与 `ledger.tsv`)+ CI 路径守卫 + pre-push 拒直推 main 兜底,而非 `ledger.good` 快照。要完全对齐 hunt,把 weekly 改跑本地一次性流水线(同一条 bash 路径,`source=weekly`)即可。

## verdict 完整性(反串通)

- 生成 / 查重 / 打分是独立进程,裁判看不到生成方自评,也不知道停机条件——没有灌水动机。裁判**并行 + 各用独立输入目录**,开跑时看不到彼此产出。
- novelty 只认独立查重进程产出的证据,不认生成方"没人做"的自述。
- verdict、ledger、publish 全由 `hunt.sh` 决定:每个 idea 取 N 位裁判**最低**票,SA 需全票,缺/坏票当 reject。
- **SA 硬门槛**:全票 SA 还须过 orchestrator 校验——该 idea 有查重块、实读篇数 ≥ `MIN_READ`(默认 5)、附最小否证实验、每位裁判都写了完整评审;缺任一则硬降级 reject。
- ledger 以 `tmp/ledger.good` 为单一可信基线,启动时取当前工作树 `ledger.tsv` 作为人工基线,之后只被 bash 聚合更新;任何 agent 擅改(含中途失败轮的残留)在下一轮开局被抹掉。
- **中断续跑不放水**:重启只沿用过机械门槛的前段产物(它们本就是 agent 产物,由门槛+裁判消化);遗留的评审票据与评审块一律清除、裁判由 orchestrator 重新调起——防前段借崩溃伪造整轮票据绕过独立评审。
- **分阶段守卫**:生成/查重/评审阶段禁写 `ideas/`(防伪造达标报告绕过全票),仅 report 阶段可写。

## 无人值守的四层保证

1. **工具策略**:claude 走 `.claude/settings.json` allowlist——只放行写 `ideas/` 与草稿区(仓库内 `tmp/`、系统 `/tmp`),WebSearch/WebFetch,无参 ls/date;`ledger.tsv` 写权与 publish/git/gh 都不给 agent(orchestrator 独占)。未匹配操作在无头模式下自动拒绝(前提:本仓库已 trust)。codex 走 OS 级 sandbox(`-s workspace-write` 写限仓库、`-c approval_policy=never`、`-c sandbox_workspace_write.network_access=true` 放行网络、`--search`;`codex exec` 须用 `-c approval_policy=` 而非 `-a`);codex 沙箱无细粒度写控,ledger 完整性靠上面的快照-还原兜底。agy(前段 `FRONT_CMD`)既无 allowlist 也无 OS sandbox,其 CLI sandbox 可读写 `$HOME`,不能当边界;故只用于生成/查重这类**可错**的上游——靠回路守卫(§3)回滚越界仓库改动、且它绝不碰 verdict/ledger/publish 兜底,错误 idea 由下游独立裁判毙掉。它对 `$HOME` 的越界写在守卫视野外,但不影响判定与发布产物。
2. **push 守卫**:发布只经 `./publish.sh`——add 范围硬编码为 `ideas/` 与 `ledger.tsv`,走 `hunt/当日` 分支 PR;幂等可重跑,commit 后中断可补推送/补 PR。`.githooks/pre-push` 拒直推 main(人工覆盖 `ALLOW_MAIN_PUSH=1`)。GitHub 端未启用 branch protection,远端 main 无服务端防线。
3. **回路守卫**:`hunt.sh` 每次调 agent 后按阶段校验已跟踪改动——生成/查重/评审阶段只允许 `ledger.tsv`(且靠 ledger.good 兜底),report 阶段才放行 `ideas/`;越界未提交改动自动回滚,越界已提交/未跟踪文件停机留人工。日志 `hunt.log`(gitignored),连续异常达 `MAX_FAILS`(默认 12)即停。
4. **CI 守卫**:auto-merge workflow 只按路径判定——本仓库分支的 PR 改动全落在 `ideas/**` 与 `ledger.tsv` 才自动合并,越界跳过并留言,固定层改动必须人工 merge。

**看结果**:达标 idea 在 `ideas/` 报告正文(仅 Strong Accept,附评审表与查重记录);历史上所有想过的 idea(含被拒的及拒因)在 `ledger.tsv`。

**调 harness**:评审严格度 → `brainstorming_policy.md`(尺度)/ `REVIEWERS`(票数)/ `roles/review.md`(裁判铁律);发散方向 → `brainstorming_policy.md`;回路规则 → `PROGRAM.md`;各角色行为 → `roles/`;单次运行的范围/停机/输出 → 对应入口文件。
