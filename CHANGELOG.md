# CHANGELOG

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
