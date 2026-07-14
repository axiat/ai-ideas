# AwR 复活链路重建方案

状态：整体搁置，保留为完整设计规格。实验闸已跑（2026-07-14）：最强候选 `2b500d736c99` 卡在 overlap 校准刀刃上（原始全 claude 轮判 low → 2,2,1 near-SA，严查判 high → reject），从未达 SA，本方案不实施。结果与决定见 `DEVELOPMENT.md` P1「重建 AwR 复活链路」。

## 1. 范围与结果

当前有三条相关路径：

| 路径 | 输入 | 输出 | 边界 |
|---|---|---|---|
| A：主环复查/进化 | 满足 `PROGRAM.md:12` 的进化/复查父本；near-SA 仅作优先提示 | priorwork + 配置的 N 席正式 verdict | 现有主环 |
| B：AwR sidecar | latest-wins 的 low-overlap design-fixable AwR | 带证据和 provenance 的 `SA-可能` 成品 | P1 |
| C：人工 promote | 人工确认的 sidecar 成品 | 手交主环重评 | #3 落地前过渡 |

P1 sidecar 主循环不把 `SA-可能` 写成正式 verdict，也不改 `ledger.tsv`；§3.8 的显式 `promote` 是转交现有主环的独立管理流程。可信成品须同时满足：

- 裁判席通过启动期可信配置门；
- `check_judge` 通过 novelty、feasibility、clear-accept 三节的结构门；
- novelty 节含强制的「最强反例」证据；
- final marker/manifest 可追到终态 draft、judge、feedback、finalize 的完整 artifact DAG；
- 第三次反馈后的最新版仍经过裁判。

自动桥把可信成品重新送入主环，语义为 at-least-once external execution + exactly-once committed effect。外部 agent、priorwork 和评审不进入长 DB 事务；DB 只原子提交正式 verdict 与 committed 状态，文件 materialization 在事务外幂等重放。

## 2. 不变量

### P1

1. sidecar 主循环接受的仓库写入只限 `tmp/awr-side/`，不写 `tmp/round/`、`ideas/`、`ledger.tsv`，不执行 git；§3.8 `promote` 另走主环管理边界。所有 agent 在 per-call mirror 中运行且只回收白名单产物。无法提供 OS 级外部副作用隔离的 backend 不能当 trusted judge。
2. 默认未配置可信裁判时 dormant `exit 0`，零队列访问、零 agent 调用、零任务状态写入。
3. 配置、infra、contract 故障不能形成「还不行」或永久科学终态；只有合法内容 reject 消耗反馈轮。
4. trusted 主循环按顺序持 legacy inhibit `tmp/agy-side.lock` 与 `tmp/awr-side.lock`；migration/空安装 bootstrap 按 `hunt -> legacy -> new` 取得三锁。只持 new lock 不得检查 absent 后生成空 `DONE`。
5. sealed migration plan 的每个 action 独立完成；同一 key 的一个 action 完成不能覆盖另一个 action。
6. legacy target 存在不等于迁移完成；对应 source 必须删除或被显式归档后才能记 action done。
7. `tmp/ledger.good` 的发布点全部使用同目录临时文件 + fsync + atomic rename；迁移只读冻结快照。
8. provenance 记录调用和 deterministic transform 的 artifact DAG；prepared/started 记录不能冒充真实执行或产物归因，终态必须有原子 commit marker。
9. durability 顺序固定：copy+delete 路径先写/fsync/rename target、fsync target parent，再 unlink source、fsync source parent；move 路径 atomic rename 后 fsync source/target 两侧 parent。两种路径都只在目录项 durable 后 append+fsync 后继 event；event 文件首次创建时也 fsync parent。event 不得先于其声称的目录项持久化。
10. 新建 epoch、archive、CAS、provenance 等持久目录统一走 `durable_mkdir`：逐级拒绝 symlink，创建后 fsync 其 parent；整条目录链 durable 前不得删除/改名任何唯一 source。

### 自动桥

1. `lineages` 只承载血缘身份；Path A/B 资格证据位于 `reentry_grants`，readiness 位于独立 `reentry_requests`。
2. 普通 ledger 导入只创建 lineage/candidate，不创建 grant/ready request。只有完整 Path A/B 资格门可写 active grant 并创建或激活 request。
3. 同一 lineage 的进化、复查、复活共用一个 reentry 名额和每轮一个 slot。
4. 过期 claim 对分配器可见；reclaim 后旧 token 不能提交。
5. 资格撤销后不能继续 claim，也不能提交已经完成但已失格的结果。
6. DB 内原子提交 verdict + committed；文件按 `candidate_id` 在事务外幂等 materialize。

## 3. P1 运行契约

### 3.1 入口顺序与启动真值表

入口先解析子命令和原始环境变量，不创建目录、不写日志、不迁移文件：

| 模式 | 条件 | 行为 |
|---|---|---|
| migration | `awr-side.sh migrate-legacy` | 不调 agent；先固定 migration release bundle，再按 hunt/legacy/new 顺序取得三把锁后执行 §4 |
| ledger adoption | `awr-side.sh adopt-ledger-baseline --source <git-object|file> --expect-sha <sha> --authority <ref>` | 不调 agent、不要求 judge env；持 hunt lock，以显式 authority/expected SHA 原子发布 baseline + attestation |
| lock recovery | `awr-side.sh recover-lock <hunt|legacy|new> --expect-digest <sha> --confirm-stale` | 不调 agent、不要求 judge env；只走独立 recovery guard 与下述 quarantine protocol |
| reopen | `awr-side.sh reopen <key> <research|judge>` | 不调 agent、不要求 judge env；校验参数、release/DONE/runtime state 后按 legacy/new 顺序持锁执行 §3.3 的归档状态变换 |
| promote | `awr-side.sh promote <key>` | 不走 dormant；验证 trusted final、formal pipeline policy 与 outer-confinement/DAG capability 后执行 §3.8，能力未落地则禁用 |
| dormant | 无子命令，且 `SIDE_JUDGE_CMD`、`SIDE_JUDGE_TRUSTED` 均未设置 | stderr 记录 dormant，`exit 0` |
| invalid | 两项只设置一项 | 点名缺项，`exit 2` |
| trusted | 两项均设置，research adapter 可解析且 §3.2 全过 | 按 legacy/new 顺序取得 inhibit/new lock，验证 migration gate，再进入主循环 |
| invalid | 两项均设置但 research/judge 任一 capability predicate 失败 | 点名席位和失败谓词，`exit 2` |

子命令必须在 dormant/env 判定前完整解析；未知子命令、缺/多参数或非法 key/seat 一律 `exit 2`，不得落入默认主循环。真值表按变量是否显式设置判断，不按 `judge_cmd=${SIDE_JUDGE_CMD:-$SIDE_CMD}` 的派生值判断。`SIDE_CMD` 单独存在仍是 dormant。进入 trusted 分支后，research 严格按显式 `SIDE_RESEARCH_CMD` -> 显式 `SIDE_CMD` -> `agy-builtin` 解析；显式空值非法，不静默回落。三种 research 选择都必须有已登记 outer-confinement profile，启动期分别 fail-fast 验证；`agy-builtin` 只允许作 research，不能充当 trusted judge。

进入 trusted 分支或 `promote` 在创建任何目录、调用 agent、写 reservation 前，先把 effective `REVIEWERS` 解析为十进制整数 `N>=2`（默认 3），落实 `PROGRAM.md`“verdict 不由任何单个 agent 决定”。同时要求 `5<=PRIOR_MIN_LINKS<=8`、`PRIOR_MIN_API>=1`、`MIN_READ>=5`、`AXIOM_MIN_CRACKS>=2`；配置只能收紧且不得与固定上界冲突。零/单席、低于 policy floor、非整数、越界或自相矛盾的值均 `exit 2`，不能用弱化 env/promotion policy bundle 绕过硬门。

新代码先在受控 `tmp/.lock-owners/<nonce>/` 建好并逐级 fsync owner directory，其中 receipt 编码 pid、process-start token、hostname、随机 nonce 与 schema version，且保留兼容的 `pid` 文件；随后以单次 atomic symlink 把 contested lockpath 指向该 owner directory。创建 lockpath 时 owner identity 已完整存在，不留 `mkdir -> write pid` 窗口。新 reader 先 `lstat/readlink`，只接受规范化后位于 `.lock-owners/` 的相对 target，再以 dirfd/`openat(O_NOFOLLOW)` 验证 receipt；绝不跟随任意 target。释放前完整匹配 nonce/receipt，unlink lockpath 并 fsync parent，最后回收 owner dir，防 ABA。

兼容 `pid` 使旧 `hunt.sh` 的 `cat "$LOCK/pid" && kill -0` 看到 migration live owner并退出，而不是把 symlink 当空 stale lock 后 `rm -rf`；旧 sidecar 的 `mkdir` 同样失败。全局顺序固定为 `tmp/hunt.lock -> tmp/agy-side.lock -> tmp/awr-side.lock`；migration 取三者，trusted/reopen 跳过 hunt 但仍按 legacy -> new。任一 live owner 存在即释放已取得锁并 `exit 2`。旧 directory lock 有 pid 且 `kill -0` 成功时保守视为 live；pid 已死才可归档清理。无 pid/格式不明的 legacy directory 无法安全判活，fail closed，不自动按锁龄删除。新 lockpath 只允许上述受控 symlink，遗留 directory 仅走兼容读取/recovery，不能由普通 acquisition 创建。

`recover-lock` 不尝试取得待恢复的 target lock。它先以新式 symlink receipt 取得独立 `tmp/awr-lock-recovery.guard`，再以 `lstat/openat` 固定 target lock 的 inode/uid/type/tree digest；命令行 `--expect-digest` 必须精确匹配现场，且 operator 显式给出 `--confirm-stale`。能解析 owner 时仍须验证 pid+start-token 已失效；owner 仍 live、remote owner 无法核实、现场在 guard 后变化或 target 为 symlink 时拒绝。确认后只把原 lock atomic rename 到 `tmp/lock-quarantine/<kind>-<digest>`，fsync 两侧 parent，并 durable 写 recovery receipt；不直接删除。随后释放 guard，下一次 migration 再走普通 ordered lock acquisition。相同 digest 的 recovery 重放只在 quarantine/receipt 后置条件完全匹配时幂等。

可信模式取得 legacy inhibit + new lock 后执行 migration gate：

1. `tmp/agy-side` 存在：`exit 2`，要求显式执行 `migrate-legacy`；主循环不得自行 rename。
2. `migrations/v1/DONE` 存在：校验 immutable migration receipt、其 `migration_bundle_id` 被当前 `P1_RELEASE` 的 trusted predecessor allowlist 接受，再对当前 queue 执行 runtime validator；三者都通过才放行。
3. migration epoch 存在但无有效 `DONE`：`exit 2`。
4. 无 epoch 且 queue outdir 非空：`exit 2`。
5. `tmp/agy-side` 不存在、无 epoch、queue outdir 不存在或为空：不得在当前两锁上下文生成 `DONE`。释放顺序反向校验 receipt 后，转入 pinned migration empty-bootstrap path，按 hunt/legacy/new 重新取三锁并重新检查 roots/epoch/inventory；只有三锁下仍为空才原子创建 expected=0、`snapshot_sha=none` 的 sealed plan、`done-all` 和 `DONE`。完成后释放三锁，再按 legacy/new 重取并从 gate 第 1 项重验。

`side.log` 固定为 `tmp/awr-side/side.log`，不放在 queue outdir。reopen/unfreeze 等正常运行归档固定进独立 `tmp/awr-side/runtime-archive/`，由 runtime event/CAS 管理，绝不写入或改变 `migrations/v1/archive/` 的 immutable digest。migration gate 之前的诊断只写 stderr，避免日志写入改变“空安装”判定。

### 3.2 可信裁判席

可信模式须同时满足：

1. `SIDE_JUDGE_TRUSTED=1`；
2. `SIDE_JUDGE_CMD` 显式设置，不从 `SIDE_CMD` 继承；
3. research 与 judge 使用不同 `call_id`、独立进程/context 和角色专用输入；允许同一 adapter 命令分别启动，两席不能共享 session/resume id 或可写 sandbox；
4. research/judge 外层都通过已登记的 OS sandbox/container profile：只把本 call 的固定 input bundle 与不可变 CLI runtime 以 read-only 暴露，只给单一 output staging dir 写权；拒绝读取真实 repo、其它 key/outdir、用户 home/config、SSH/Git 凭据与 host sockets，限制可 exec 程序和子进程。网络只经无用户凭据的显式 research/model egress policy。per-call mirror、cwd、prompt、Codex `workspace-write` 或 Claude permission allowlist 本身均不算 read/write confinement 证明；
5. judge 还须通过 adapter capability predicate，而非只看 basename。Codex adapter 在外层 confinement 内固定 mirror 为 workspace，使用 `--ignore-user-config` 与隔离 `CODEX_HOME`，验证 hooks/plugins/MCP 为空，再设置 `workspace-write`、`approval_policy=never`、network 显式、`--skip-git-repo-check --ephemeral`，拒绝 danger/full-access。Claude adapter 使用隔离 HOME/config、sanitized settings、显式空 hooks/plugins/MCP 和 `-p --strict-mcp-config`；
6. judge 的 resolved executable 只能是外层 profile 中的不可变 `claude`/`codex` bundle，拒绝 `agy*`、仓内同名遮蔽、`env`/`timeout` 前缀和未知 wrapper。research 的 `agy-builtin` 只有在其独立 outer profile 与最小 auth mount 通过同样的 read/write/process probe 时才合法。当前 `grok-worker.sh` 允许 git/gh/curl 并继承用户 hooks/plugins/MCP；在同等 read/write/process/credential confinement 验收前不加入白名单。

manifest 的 independence 分档：

- `verified`：两席 backend 和确切 model 均可解析，且组合不同；
- `asserted`：只能证明独立 invocation，模型独立性由 `SIDE_JUDGE_TRUSTED=1` 声明；
- 配置门未过：不产生 manifest，也不产生内容 verdict。

命令串不同只证明独立调起，不写成“已证独立模型”。trusted 只表示裁判资格通过，不自动表示配置可复现。

### 3.3 失败分类、quarantine 与反馈状态机

每次调用使用独立 stdout/stderr/rc 和临时产物，按以下优先级互斥分类：

1. `infra-failure`：`rc != 0`、空产物，或 stderr/产物命中网络、认证、配额故障。只重试/冷却/熔断，不生成 `.badN`，不增加 key counter。
2. `contract-failure`：`rc = 0`、产物非空、未命中 infra，但 `check_draft`/`check_judge` 失败。按席位写 `<key>.research.contract.badN` 或 `<key>.judge.contract.badN`。
3. `content-reject`：judge 产物 contract 合法且明确指出内容缺陷。只有该类写入 `## 裁判反馈` 并消耗 `SIDE_MAX_ROUNDS`。

单席 contract counter 达 `SIDE_MAX_BAD` 后写 `<key>.<seat>.quarantined`。marker 含 reason、count、updated_at 和调用配置摘要，只作审计，不自动触发 reopen。

`reopen <key> <seat>` 是显式管理动作，持共享锁执行：

1. 归档该席全部 `.contract.badN`；
2. 验证 outdir 已无该席 bad；
3. 归档 quarantine marker；
4. counter 从 0 重新计算。

状态机先评当前稿，再判断反馈上限：

```text
需要修订 -> research -> contract 校验 -> 安装 draft
draft     -> judge    -> contract 校验
judge=SA-可能                  -> finalize 达标
judge=还不行 且 rounds >= max -> finalize 未达标
judge=还不行 且 rounds < max  -> 回灌反馈，rounds += 1
```

`max=3` 时三次反馈均会被应用，rounds=3 的最新 draft 仍会重新 judge。不得使用 `rounds + 1 >= max` 提前收尾。

### 3.4 调用级 provenance

每次 research/judge 使用唯一 `call_id` 和不可变 invocation bundle。trusted sidecar 启动时先把 `P1_RELEASE` manifest 解析为只读、content-addressed release bundle：完整保存 entry/orchestrator/libs、roles、parsers、policies、rubrics、adapters、outer profiles/capability predicates、identity/CAS/current-pointer/selector 实现及 schema，逐项核对 manifest 后固定 `process_bundle_id`。运行期除 task/current pointers 等显式数据输入外，不再从工作树读取 executable policy bytes；每次 queue scan 和 call 在写 `prepared` 前重验 release bundle digest/schema 与进程 pin，缺失或变化立即 fail closed。工作树升级不影响已启动进程；切换 release 必须启动新进程。后续调用不得把单组件变化记成可接受 drift。每次调用再固定：

- 只从进程 release bundle 复制的 prompt、policy、rubric 和仓内 adapter；仓内代码从该只读副本执行；
- 只从 release bundle 实例化的 outer sandbox/container profile、read-only mounts、writable output、exec/egress policy 与 sanitized HOME/config/CODEX_HOME 的 hash；
- adapter 展开后的 final argv、resolved executable、backend、确切 model、CLI/package version；
- 会改变行为的 effective env（如 model、sandbox、turns、web 开关、binary path）以及实际加载的 settings、plugin、hook、MCP 清单和内容 hash；secret 只记来源/存在性和不可逆 fingerprint，不写明文；
- 冻结并实际传给 prompt/parser 的 `REVIEWERS`、`PRIOR_MIN_LINKS`、`PRIOR_MIN_API`、`MIN_READ`、`AXIOM_MIN_CRACKS` effective values；
- policy SHA、`process_bundle_id`、gate SHA、所有已载入仓内 lib SHA、adapter SHA；
- `SIDE_JUDGE_TRUSTED=1`、adapter capability predicate 的逐项结果和 independence 分档；
- git HEAD、role、key、round、时间、输入 artifact ids/SHA；
- repository-scoped `ledger_instance_id`、origin ledger snapshot artifact id/SHA/row count、row number、raw-row SHA、canonical story/lineage key 和稳定 `origin_stable_id`，用于把修订稿映射回原 lineage；snapshot SHA 只作审计上下文，完整 bytes 由 CAS artifact 保留。

policy/profile hash 对 invocation bundle 内实际使用的 release-bundle 副本计算；gate SHA 来自进程启动时固定的 `process_bundle_id`，并在 call preflight 与 release manifest 重比。外部 CLI 只有从不可变的 self-contained image，或调用全程只读且内容寻址的 executable + 完整 package/dependency tree 执行，才记 `execution_identity_pinned=true`。child 成功 exec 后的一次 path/version/hash 观察只能证明启动点，不能排除 Node/Python 等 launcher 在运行中混载被升级 module；这类非冻结 package 一律记 false。model、effective config 或执行身份取不到时写 `unknown`，不拿命令串代替。

`tmp/awr-side/provenance/<key>.jsonl` 以 append-only 事件记录调用生命周期：

1. `prepared`：bundle 已固定，尚不能证明 agent 启动；
2. `started`：child 通过成功 `exec` handshake 后追加，记录 pid/实际 image identity；仍不构成产物归因；
3. `completed`：子进程已返回，记录 rc、失败分类、输出 artifact id/SHA 和 contract 结果；
4. `installed`：contract 合法的 artifact 已由 call-bound current pointer 提交为当前 draft/judge。

事件使用 canonical JSON、确定性 event id 和行 checksum，追加后 fsync；只允许尾部出现一条 crash 截断记录，同 event id/内容的重放幂等，冲突或中间坏行 fail closed。调用输出先写 per-call 临时文件；输出 CAS object durable 后才可追加引用它的 `completed`。只有 completed 且 contract 合法的输出可安装；call-bound current pointer durable 后才追加 `installed`。若进程在 pointer rename 后、`installed` 前被杀，resume 只在 pointer 明确携带同一 `call_id` 和 artifact id/SHA 时补写 installed；cache/target SHA 相同不能触发补写。

所有 task/draft/judge/final/ledger-snapshot 内容先进入 content-addressed artifact store。每个可变 logical artifact 使用 `<key>.<role>.current.json` 作为唯一提交点，pointer 固定 `call_id/transform_event_id`、artifact id/SHA 和 sequence：先持久化 CAS object，再以 temp + file fsync + rename + parent-dir fsync 原子替换 pointer；人读的 `.task.md/.draft.md/.judge.md` 只是由 pointer materialize 的 cache，crash 后可从 CAS 重建。只有 pointer 明确绑定该 call/event 时才能补 `installed`；target cache 恰与 completed SHA 相同不能证明本次安装，禁止据此归因。被 current/final marker、migration receipt、promotion staging/attestation 或 DB artifact row 引用的 CAS object 不得 GC。

artifact DAG 除 agent call 外还记录 orchestrator transform：

- `task-created`：immutable ledger snapshot artifact + origin row -> 初始 task；
- `feedback-applied`：旧 task + 已安装 judge -> 新 task；
- `draft-installed` / `judge-installed`：调用输入集 -> 当前 artifact；
- `finalized`：终态 draft + 终态 judge + manifest -> `<key>.md`。

每个 transform 记录 input artifact ids、output id/SHA、`process_bundle_id` 和事件序号。finalize 从终态 artifact 反向遍历完整 DAG：manifest 的 producer calls 只含链上的 installed 调用，transform nodes 另列；prepared、started、未安装 completed、失败或已被替换的调用只留在 attempts，不得用于终态归因。同一 process 内 release component 不允许 drift；若 crash/resume 明确切换到另一个已完整验收的 release/process，跨 process 链逐项列出版本并标 `drift=true`。

终态 `<key>.md` 先完整写临时文件并校验 manifest/DAG，fsync 后 atomic rename 并 fsync 父目录；随后以同一 durable helper 原子写 `<key>.finalized` commit marker，内容含 final SHA、manifest SHA、terminal draft/judge ids。crash 发生在 rename 后、marker 前时，resume 校验整条 DAG 后补 marker。主循环只把 marker 与文件/manifest/DAG 全部匹配的成品视为终态，非空 `<key>.md` 本身不构成完成。

runtime validator 在每次 trusted 启动和 queue 扫描前执行。严格校验前只允许按确定性 event id 对 prepared filesystem transition 做恢复：postcondition 全匹配则补 commit event，前置状态仍完整则幂等续做，其它组合 fail closed。随后要求 terminal 有有效 final marker；新 draft/judge 可追到 installed event；task 变化有 transform；sealed `adopt-current-artifact` 可作为标记 `legacy-untrusted` 的 task/draft 根输入，但不能单独授予终态信任；frozen/unfrozen 有 migration/runtime receipt；未知或无 provenance 的 `<key>.md` fail closed。

只有 final argv、backend、确切 model、CLI/package、全部行为相关 env/config、policy、gate、adapter、执行身份均已固定且可读取，成品才标“配置可复现”。任一 `unknown`、未固定用户级 plugin/hook/MCP 或 `execution_identity_pinned=false` 时仍可审计和重评，但不得作复现声明。

### 3.5 judge 证据 contract

per-call mirror 只复制固定 prompt/config 与当前 key 的 current pointer 所指 `task.md`、`draft.md`；禁止 `cp "$outdir"/*.md`。其它 key、终态 `<key>.md`、`.legacy-frozen.md`、migration archive、quarantine/bad 均不得进入 agent sandbox，真实 repo/outdir 的绝对路径也由 outer profile read-deny。staging parent/output dir 由 orchestrator 预建且 parent 不给 agent 改名权限。调用结束并确认全部 child 已退出后，orchestrator 通过预开的 dirfd + exact basename 执行 `openat(O_NOFOLLOW|O_NONBLOCK)`，`fstat` 要求 expected uid、普通文件、`nlink=1`、无特殊 mode、size 在上限内；从同一 FD 完成 hash、parse 和 CAS copy，再复核 inode/size。symlink、hardlink、FIFO、device、socket、目录、路径替换或额外输出一律作为 contract/infra boundary failure，绝不以普通 path open/cp 回收。

基础 contract 保留现有早停门：

- `check_draft`：含 `## 修订版 idea`、唯一且合法的 `形态:`、至少 3 条带 URL 的检索记录，最后一个非空行严格为 `AGY-DONE`；
- `check_judge(task,draft,judge)`：恰有一个 `判定: SA-可能|还不行` 和一个合法 `确认形态:`；`还不行` 至少有一条 `- 缺陷:`；两种路径最后一个非空行均严格为 `AGY-DONE`。

形态使用稳定 enum：`mechanism-or-new-problem`、`math-exploration`、`cs-principle-transfer`、`bottleneck-probe`、`load-bearing-assumption`。`task-created` 从原 run artifact 固定 `origin_shape`；无法证明时写 `unknown`，不得由 research 覆盖。draft 声明 revised shape，独立 judge 必须确认。若 origin、draft、judge 任一为 `load-bearing-assumption`，或 task/draft 命中该形态的保留结构字段，effective shape 一律取该值并触发裂缝门；`unknown` 不能被 draft 单方降成普通形态。

`roles/awr-judge.md` 的 `SA-可能` 路径固定输出：

```text
确认形态: <上述 enum 之一>

## novelty 证据
- <neighbor-id> | <最近邻及 URL，5–8 条>
- 最强反例: <上述 neighbor-id> | <关键差异>
- API query: <arXiv 或 Semantic Scholar API URL>
- 实读篇数: <整数>

## feasibility 证据
- 最小否证实验: <至少 30 bytes 的完整描述>
- data: <非空>
- compute: <非空>
- signal: <非空>

## clear-accept gate
<至少 30 bytes，说明为何接近 6/6/8+>
```

仅当 `判定: SA-可能` 时，`check_judge` 才要求三节各恰好一次且按 novelty -> feasibility -> clear-accept 排序，并按节边界执行下列强证据门。`判定: 还不行` 只走前述 base contract：合法 `确认形态:`、至少一条 `- 缺陷:` 和末行 `AGY-DONE`；缺三节不能把它从 content reject 改判为 contract failure。`SA-可能` 至少验证：

- novelty 节有 `effective PRIOR_MIN_LINKS..8` 个 distinct neighbor identity；arXiv id/DOI 可提取时作为 identity，否则用去 fragment/tracking、规范 host/path 后的 canonical URL。计数排除保留字段，同一 paper 的 abs/pdf/version URL 或重复 URL 只算一次；`neighbor-id` 唯一；
- 有至少 `effective PRIOR_MIN_API` 条 distinct、经 URL parser 验证的 HTTPS query；模板允许重复 `- API query:` 行。arXiv 只接受 host=`export.arxiv.org`、path=`/api/query` 且 `search_query` 或 `id_list` 解码后非空；Semantic Scholar 只接受 host=`api.semanticscholar.org`、path=`/graph/v1/paper/search` 且 `query` 解码后非空。拒绝 bare domain、错 endpoint、空参数、fragment、userinfo，以及 `<...>`、`${...}`、`TODO`、`example` 等占位值；
- 节内恰有一行 `最强反例`，引用上述一个 neighbor-id 且另含非空关键差异；
- 整行 `^- 实读篇数: [0-9]+$` 且值 `>= effective MIN_READ`；
- feasibility 含最小否证实验，data/compute/signal 各自至少一个非空白字符；
- clear-accept 节正文达到长度门。

`roles/awr.md` 的修订稿固定含 enum `形态:`，judge 固定含 `确认形态:`。effective shape 为删公理的 `SA-可能` 另需 `## 裂缝核验`，每条严格匹配：

```text
- 裂缝: <evidence-id> | <来源 URL> —— 相符：<理由>
- 裂缝: <evidence-id> | <来源 URL> —— 不符：<理由>
```

parser 把 evidence-id + normalized URL 反查到 task/draft 中冻结的自报裂缝证据；未知来源拒绝。同一 normalized evidence 即使换 id 或重复行也只算一次，distinct `—— 相符` 须 `>= AXIOM_MIN_CRACKS`。prompt 模板与 parser regex 共用 fixture，防空格或字段名漂移；origin 标为删公理而 draft/judge 自报普通、重复 neighbor/crack、bare API endpoint、空 query 和 placeholder query 都有负 fixture。

这些是 honor-system 的结构门，不等于独立查重；正式 novelty 由自动桥重新执行主环 priorwork。

### 3.6 队列选择

P1 先建立与 #3 共用的 identity helper，并在仓根提交只读 `ledger.instance-id`；其规范化单行值是 immutable `ledger_instance_id`，升级/clone 不得重生成：

- `canonical_story_v1`：Unicode NFC、trim、内部连续空白压成单空格、统一换行，保留标点和引号；`origin_lineage_key = sha256("tsv-v1\0" + UTF8(canonical_story))`；
- `origin-row-v2`：`row_number` 是 header 后从 1 开始的 data-row 序号；`raw_row_sha` 对该 TSV row 的 exact bytes 计算，但排除一个结尾 `LF` 或 `CRLF`；`origin_stable_id = sha256("tsv-row-v2\0" + ledger_instance_id + "\0" + decimal(row_number) + "\0" + raw_row_sha)`。

snapshot SHA 只作 provenance。sidecar task、promotion receipt、#3 importer 必须调用同一 helper；物理文件行号、含 terminator 的 hash 或未版本化自实现均非法。文件缺失、重复行或值变化时 fail closed。人工确认两个 canonical story 属同一语义 lineage 时，resolution 必须写入 tracked promotion attestation，后续 receipt 复用其中既有 origin_lineage_key；无既有映射或证据歧义时不 promote。#3 再把这些 resolution 导入 `story_aliases`。

sidecar 只读完整的 `tmp/ledger.good` 快照：

1. 兼容 7/8 列，显式读取 `date source theme idea verdict reason overlap category`，不先按 source 过滤；
2. 按 `origin_lineage_key` 聚合全部 source，append-only 文件中末行获胜；本地 filename key 只作经 registry 校验的展示键，prefix collision 时扩展 hash，不能改变聚合身份；
3. 只选 winning row 同时满足 `source=hunt && verdict=accept-w-rev && overlap=low`；后来的 weekly/其它 source 行是该 lineage 的 tombstone，不能回退旧 hunt AwR；
4. 校验 winning row 的 origin fingerprint；
5. 减去当前 `near-sa-queue.tsv` 中的 story。

每次 scan 先把所读完整 `ledger.good` bytes 持久化为 immutable CAS snapshot object，记录 artifact id/SHA 与 data-row count；CAS object 与 parent directory durable 后才能写 `task-created`。选中 key 创建 task 时，把该 snapshot artifact、repository-scoped `ledger_instance_id`、winning row number、raw-row SHA、canonical story/lineage key 和 `origin_stable_id` 固化进 task provenance/DAG；后续 ledger 追加不改写 origin identity，只影响动态池资格。#3 从该 snapshot object 重验 origin row，并要求 current ledger 的前缀仍逐 row 匹配。

near-SA 文件会 prune，ledger 又不保存完整票向量，因此第 5 步只是 P1 的 best-effort 去重。按 `sa_votes` 的严格 A/B 分流由 #3 持久化 candidate/request 后完成。

evidence-incomplete 的 ledger verdict 为 reject，机械上不进入 sidecar；仍由 Path A 处理。design-fixable 保持粗标，语义资格由 research/judge 判断，不从 reason 前缀重建分类器。

### 3.7 `ledger.good` 原子发布

`hunt.sh` 所有初始化、direct-hit 入账和轮末定谳发布点统一调用一个 helper：

1. 在 `tmp/` 同文件系统创建唯一临时文件；
2. 把完整 ledger 写入临时文件并校验 header/行结构；
3. fsync 文件；
4. atomic rename 为 `tmp/ledger.good`；
5. fsync `tmp/` 目录。
6. 生成 canonical publisher receipt，至少含 publisher bundle id、target SHA/row count、source authority/ref/SHA、previous receipt SHA，以及适用的 run id、validated run/commit event ids 与 appended-row digest；receipt 先写临时文件并 fsync，再 atomic rename 为 `tmp/ledger.good.commit`，最后 fsync `tmp/`。
7. consumer 只接受 target bytes 与 receipt target SHA/row count 匹配、且 authority chain 可验证的 pair。crash 在 target rename 后、receipt commit 前会留下 mismatch，必须由同一权威 source 幂等补发，不能把新 target 配旧 receipt 当可信。

初始化的机器权威 source 仅限确切 git blob/object，或已有合法 publisher/run/commit receipt chain；`git show HEAD:ledger.tsv` 也先固定 object id 并经上述 helper，禁止直接 redirect。live `ledger.tsv` 只有结构/hash 不构成权威：无法建立上游 chain 时 fail closed，须由 operator 显式执行 `adopt-ledger-baseline`，指定 source、expected SHA 和 authority/attestation。该命令在 hunt lock 内用安全 FD 重验 source 后发布 target+receipt，receipt 标明 `authority=operator-adopt`、完整参数/HEAD/source identity/tool bundle；migration 不得代签。reader 只会看到旧版或新版完整且有匹配 authority receipt 的 pair。

### 3.8 过渡期人工 promote

#3 落地前，人工只通过 `promote` 管理命令交回主环：

1. 核对 final marker、三节证据、origin snapshot object/row fingerprint 和完整 artifact DAG；
2. 按 `PROGRAM.md:12` 重新检查原父本仍是 low-overlap、实验设计类可修死因，origin_lineage_key 尚未消费；novelty 封顶/已被占据者不得 promote；
3. `promote` 在短 `reentry-reservation` lock 内确认无现存 Path-A claim/promotion 后，为 origin_lineage_key 写 durable `pending` reservation，分配 promotion_id，给 candidate/round 绑定 promotion context、已验证 `N>=2` 的 `expected_reviewers=N` 与不弱于 §3.1 PROGRAM floors 的完整 policy/confinement bundle，在候选块记录 `进化自`、origin lineage 与具体 delta；主环重新执行 candidate generate/transform（若有）、priorwork 与 N 席评审，不继承旧票；
4. promotion context 抑制 Strong Accept 路径的普通 auto-publish。正式写 ledger 前，先在 promotion staging durable seal `precommit.json`：含 origin snapshot id/row bytes/stable+lineage ids、完整 sidecar final+manifest、候选块/delta、run/candidate、完整 installed producer DAG、N 席输入/输出与 call/process/context/session ids、confinement bundle/probe 结果，以及 deterministic predicted formal row bytes/hash。precommit 全部验证通过后，formal ledger/run commit 才能严格写入该 predicted bytes 并把 reservation 置 `committed`；随后比较实际 committed row/位置/commit id 与 prediction，seal tracked `attestations/promotions/<promotion_id>.json`。final attestation 引用 immutable precommit SHA 与实际 row/commit，使用 canonical JSON 与 SHA；
5. 持主环写锁的 helper 再幂等增加 `promoted.tsv` 唯一行。header 固定为 `promotion_id<TAB>date<TAB>local_key<TAB>origin_lineage_key<TAB>origin_stable_id<TAB>origin_snapshot_sha<TAB>origin_row_number<TAB>origin_row_sha<TAB>final_artifact_sha<TAB>run_id<TAB>candidate_id<TAB>committed_row_number<TAB>committed_row_sha<TAB>attestation_path<TAB>attestation_sha`。snapshot SHA 不参与唯一性；promotion_id、origin_lineage_key、origin_stable_id、candidate_id 分别 UNIQUE；
6. `publish.sh promote <promotion_id>` 采用 at-least-once invocation、exactly-once logical publication：固定 branch/ref、base、content digest 和 PR identity，提交 `ledger.tsv`、`promoted.tsv`、对应 attestation 和可选 `ideas/` 报告。promotion staging 的 `publication.jsonl` 以 `prepared -> started(exec handshake) -> completed(remote postcondition verified)` 记录每次 attempt；resume 先按 promotion_id 查询本地/remote branch、commit 与 PR，匹配 digest 时补 completed，不匹配时 fail closed，未完成时可用同一 promotion_id 重调。发布后端必须以 promotion_id/idempotency key 或等价唯一 head-ref 约束保证至多一个逻辑 PR。CI 从 tracked attestation 重算 final/manifest、candidate/delta、formal row、stable id 与 lineage key，不能依赖 gitignored sidecar/run 文件。普通 report/weekly 模式不得夹带 receipt/attestation。

`hunt.sh` 的 Path-A allocator、near-SA prune 和 generate 输入准备共用 reservation helper。普通 Path A 也必须在释放短锁前原子写 lineage claim，不能只先查后由 agent 选择；claim 随正式 ledger commit 转 consumed，只有 commit 前的显式安全 abort 才释放。promotion `pending` 在 formal commit 前只占用该 lineage，crash 后必须 resume 或显式 abort 才释放；任意 formal verdict commit 后状态转 `committed`，story-once 已消费，即使 attestation/PR 尚未发布也不能再次选择；receipt 合入后转 `published` 并由 `promoted.tsv` 长期封住。状态缺失/冲突时全局 reentry fail closed，不靠 agent 自律跳过。

manual promote 虽早于 #3，formal calls 仍必须实现 §5.5 等价边界：generate/transform 的 agent producers、research 与 N 个 reviewers 使用 registered outer sandbox/container，真实 repo、其它 key、兄弟席、home/config 与 prior/later reviewer output 均 OS read-deny；每席 mirror 只含冻结 candidate/priorwork/policy，输出按 §3.5 的 dirfd/FD contract 安全回收。各 role/seat 的 `call_id/process_instance_id/context_id/session_or_resume_lineage` 必须独立，`REV_STAGGER_SEC` 只影响调度，不能改变可见输入。formal commit 前从 sealed precommit DAG 重验 exact N、全部 inputs、confinement、producer independence 与 predicted row；能力 predicate 任一缺失时 `promote` 在 reservation 前 `exit 2`，不得回落到当前 repo cwd 的未隔离 `REV_CMD`。

crash 在 formal commit 前不写 receipt，因此不会假消费；sealed precommit 可幂等复用。commit 后、final-attestation/receipt/publication attempt 任一点 crash 时，恢复器先要求实际 row bytes/hash 等于 precommit prediction，再从该 promotion staging 补齐同一 promotion_id，不重跑候选，并可幂等重调 publication；不匹配即 fail closed。publication 失败阻止同 lineage 的新 promotion。只有 formal row 与 final attestation 在同一受控 git history 中可验证的 receipt 才是 #3 的跨安装已消费证据；canonical 相同的不同 rows、不同 append-only snapshots 或人工 alias 到同 lineage 都只能有一条。映射不唯一时停止，不静默创建新 lineage。

## 4. Legacy migration

当前迁移 fixture 为 89 个旧终态、35 个在修 key，其中一个在修 key 另有旧混合 `.badN`。数字只用于当前 fixture 验收；实际 action 集以冻结快照和 sealed plan 为准。

### 4.1 根目录纳管

`migrate-legacy` 在碰锁和数据根前先验证独立 `MIGRATION_RELEASE` manifest，把 entry/lock/durability/atomic-ledger-publisher/planner/selector/action/recovery、identity/CAS helpers 与全部 schema 固定为只读 content-addressed bundle；`migration_bundle_id` 是 manifest 与全体 bytes 的 canonical digest。manifest 还固定已升级 `hunt.sh` 的 hash：其 lock acquisition 对 non-directory/unknown owner fail closed，publisher 使用 §3.7 helper。磁盘 component 不匹配时 migration 禁止启动。薄 wrapper随后只 exec 该 bundle，不能从工作树混载 helper。

exact bundle 按 `hunt -> legacy sidecar -> new sidecar` 的全局顺序排他取得三把锁并持有到 `DONE`；旧 `hunt.sh` 或 `agy-side.sh` 的 directory lock 会与新 symlink acquisition 冲突，live owner 存在即退出，stale/unknown 格式按 §3.1 recovery 规则处理，不能边迁移边让 plain-`cp` writer 更新 `ledger.good`。锁齐后、任何 old/new root 或 ledger baseline mutation 前，先 durable 写 sibling intent `tmp/awr-migration-v1.bundle.json`（bundle id、manifest digest、schema、created event），fsync 文件与 `tmp/`；existing intent 只接受 exact match。

resume 先读 intent/epoch bundle receipt，并重新 exec 同一个保存的只读 bundle；当前 wrapper/release B 不得用 B 的 planner/action code续写 A 的 epoch。原 bundle 不可读或 digest 不符时在下一 source mutation/event 前 fail closed。当前 release 可在 trusted predecessor allowlist 中接受 A 产生的 DONE，但 action/event producer 始终仍是 A bundle。旧 `agy-side.sh` 仍持 old lock 时立即退出，不能只持 new lock 后搬正在写的目录。

冻结前还须证明 ledger publisher 已升级：`tmp/ledger.good` 必须有由 bundle 内 atomic publisher 在 `hunt` 锁下产生的 durable commit receipt（publisher bundle id、source authority/ref/SHA、previous receipt、target SHA、row count）。legacy plain-`cp` 文件没有 receipt，不可直接采用；migration 也不得仅凭 live `ledger.tsv` 的结构/hash自动 reseed。可机器验证的 git object 或完整 hunt run/commit receipt chain 可由新 helper重建；其余情况 fail closed，要求先单独执行 §3.7 的 `adopt-ledger-baseline --expect-sha ...`。migration intent 只引用已存在的权威 adoption/publisher receipt，不自签上游信任。

锁内根目录处理：

- old root 存在、new root 不存在：确认两者都是本地真实目录而非 symlink，在同一文件系统 atomic rename old -> new，随后 fsync `tmp/` parent；rename 后被杀，重跑从 new root 继续；
- old/new 同时存在：不在 planning 前搬对象；冻结 snapshot 后基于两棵树构建 logical merged inventory。old object 先纳入 sealed `import-old-object` transport action：目标不存在则原子迁入，目标同 SHA 则删去重复 source，目标不同 SHA 则在 seal 前 fail closed 并列出冲突。每个 transport 后还须在 seal 时生成依赖它的 freeze/archive/reset/adopt 等 semantic action；全部 semantic action 与 import done 后，才以独立 `remove-old-root` action 删除空 old root；
- inventory/open 使用 root dirfd + `openat(O_NOFOLLOW|O_NONBLOCK)` 和同一 FD hash；只接受 expected uid、`nlink=1` 的 regular file 与受控真实目录。symlink、hardlink、FIFO/device/socket、路径含 `..`/control、normalized path 逃出 old/new/migration roots，或 scan/open 间 inode 替换：fail closed；
- 主循环和 dormant 分支均不执行兼容 rename；trusted 新安装 bootstrap 必须先确认 old root 不存在。

`awr-side.sh` 顶层现有的无锁 rename 必须删除。

### 4.2 冻结输入与 sealed plan

epoch 位于 `tmp/awr-side/migrations/v1/`：

```text
MIGRATION-BUNDLE.json # migration_bundle_id + manifest digest
ledger.snap       # 决策唯一 ledger 输入
plan.jsonl        # canonical JSON action objects，不可变
events.jsonl      # canonical JSON append-only action lifecycle
archive/          # legacy 原件与反馈归档
DONE              # bundle id + plan digest + done-all 摘要
```

执行顺序：

1. old-only root 已原子改名、both-root 尚未合并且两棵树都在三把锁内稳定后，先把 sibling intent 原子复制/校验为 epoch `MIGRATION-BUNDLE.json` 并 fsync；再要求 `tmp/ledger.good` 及其 atomic publisher receipt 存在，header/列结构、receipt target SHA/row count、source authority chain 与实际 bytes 全部匹配。按 §2 的 durability 不变量 9–10 原子复制这些 bytes 为 `ledger.snap`，记录行数和 SHA，并以相同 bytes 注册 immutable snapshot artifact id；snapshot metadata 同时引用 publisher/adoption receipt SHA。bundle receipt 是 epoch producer identity，ledger snapshot 是决策数据输入；任一缺失或非法时 `exit 2`，不静默回退到未收据的 live 文件。后续 adopt/task semantic action 均把该 snapshot artifact + origin row 作为 DAG 输入。
2. 仅从 `ledger.snap` 和锁内文件树枚举 action。禁止在 planning/resume 读取 live `ledger.good`。
3. 全部 action 先写 `plan.jsonl.tmp`，fsync 后 atomic rename 为 `plan.jsonl` 并 fsync epoch 目录；计算 object count 和完整文件 digest，再向 `events.jsonl` append+fsync 带 `migration_bundle_id` 的 `plan-sealed`。
4. crash 留下的 `plan.jsonl.tmp` 直接丢弃；已有 `ledger.snap` 但无 sealed plan 时，仍用同一 snapshot 从头构建 plan。
5. crash 发生在 plan rename 后、`plan-sealed` 前时，resume 校验 plan 完整性与 frozen inventory 后补同一确定性 event；event 已在而 plan 目录项缺失属于 durability 违约，fail closed。
6. `plan.jsonl` 一旦 sealed 不再增行。snapshot 缺失、plan digest 不符或 sealed plan 外出现未知 legacy object 时 fail closed。

每行是一个 canonical JSON object，至少包含：

```text
migration_bundle_id, epoch_id, action_id, key, action, source_path, source_sha256, target_path, depends_on, eligibility_snapshot
```

path 以 JSON string 编码但输入层直接拒绝 NUL、TAB、LF、CR 和其它 control character；normalized path 必须留在允许 root。action/event hash 使用 canonical length-prefixed bytes，不对可歧义的分隔字符串取 hash。

inventory relative path 按 unsigned UTF-8 bytes、`LC_ALL=C` 排序后编号，tree digest 与 `epoch_id = sha256(length-prefixed(schema_version, migration_bundle_id, ledger_snapshot_sha, inventory_digest))` 可确定重算；canonical JSON 使用固定规范。`action_id` 从不含自身的 canonical、length-prefixed action spec 计算：schema version、migration_bundle_id、epoch_id、action、key、stable inventory ordinal、normalized source path、source SHA、logical target kind、eligibility digest。先算 `action_id = sha256(spec)`，再由 logical target kind + action_id 派生 concrete archive/target path；plan row封存 spec digest、action_id、target path 和 dependencies。不得把 concrete target path 反向放入 id 输入造成自引用。一个 key 可有多行：例如 `6afd532f9125` 同时有 `reset-feedback` 和 `archive-legacy-bad`，两行 action_id 不同，必须分别完成。

action 类型：

- `archive-terminal-for-rejudge`
- `freeze-terminal`
- `reset-feedback`
- `archive-legacy-bad`
- `archive-legacy-log`
- `remove-legacy-judge`
- `adopt-current-artifact`
- `import-old-object`
- `remove-old-root`
- `move-side-log`
- `remove-legacy-ledger-snap`

每个 old/new outdir object 必须恰好被 semantic action 的 source/postcondition 解释；`import-old-object` 只负责 transport，不能单独满足 live queue 语义。每个会被改写/移动的 imported object 在 plan 中指定唯一 consuming semantic successor；其它 dependent 只能读。transport target 在 consumer 开始前必须保持 exact SHA。consumer 在首次 mutation 前从同一安全 FD 验证 target，并 append+fsync `input-accepted` event，绑定 import action_id/source SHA、consumer action_id 与 input digest；此 durable handoff 后才可 rewrite/archive/remove。optional transport 与后续 semantic action 的依赖在 `depends_on` 固化并按 DAG 执行。无需修改的 draft/task 也用 `adopt-current-artifact` 固化迁移时的路径和 SHA，并创建 producer=`migration:<migration_bundle_id>:<action_id>`、trust=`legacy-untrusted` 的 current pointer，不能依靠完成前的宽松 allowlist 绕过 sealed inventory。adopt 是一次性 migration receipt；后续内容变化由 runtime artifact DAG 管理，不要求永远保持迁移时 SHA。终态分箱直接复用 §3.6 selector 对 `ledger.snap` 的语义：先按 lineage 聚合全部 source，再要求 winning row 是 `source=hunt && verdict=accept-w-rev && overlap=low`；在池终态 archive/reset 后交可信主循环重判，不在池终态冻结为 `legacy_status: frozen-out-of-pool`。旧 agy 的达标/未达标不参与分箱。

### 4.3 Action-level recovery

`events.jsonl` 的状态键是 `action_id`，不是 key；每条 event 显式携 `migration_bundle_id`，且必须与 epoch receipt/plan 相同。每个 action 的 latest valid event 独立推进 `planned -> working -> done`；中间步骤可写细分 event，但 `done` 只在 action postcondition 全部成立后追加并 fsync。event 是 canonical JSON，带确定性 `event_id=sha256(length-prefixed(migration_bundle_id,action_id,state,postcondition_digest))` 和行 checksum；crash 后重复追加同一 event_id 视为幂等，内容冲突 fail closed。只允许文件尾出现一条校验失败的截断记录，持锁恢复时截去；中间坏行一律停止。

通用规则：

- source 存在时先校验其 SHA 与 sealed row 一致；不一致立即停止；
- target 通过临时文件 + fsync + atomic rename 创建，并在任何 source 删除或 done event 前 fsync target parent；
- target 已存在时校验格式、action_id/source SHA 和内容 digest；不一致停止。`import-old-object` 的普通 immutable payload若 planned target 已是无 symlink 的 regular file 且 exact SHA 与 source/plan 相同，不要求改写 payload 加 metadata；action binding 由 plan + checksum event 的 transport receipt 承担，删除仍匹配的 duplicate source并 fsync source parent 后才可 done。transport done 后、consumer 尚未 handoff 时仍要求 target exact；durable `input-accepted` 后则改用 phase-aware postcondition：consumer event 必须精确绑定 import/source SHA，且 consuming successor 的当前 postcondition成立，不再要求已被合法消费的中间 target 存在；
- source 的删除、归档或 deterministic rewrite 是 postcondition 的一部分；删除/rename 后 fsync source parent，target 存在不能提前记 done；
- 重跑先检查 postcondition，再从缺失步骤继续；不以“看到 target”推断完成。

`freeze-terminal` 的完成条件：

1. `<key>.legacy-frozen.md` 存在且 metadata/digest 正确；
2. 原 `<key>.md` 不存在。

两项通过后才追加 action done event。

若 crash 发生在 target rename 后、source 删除前，resume 校验 target，删除仍匹配 sealed SHA 的 source，再记 done。若 target 已存在且 source 仍在，禁止直接 done。

`archive-terminal-for-rejudge` 使用带 action_id 的 archive 路径，完成条件为 archive digest 正确、原 `<key>.md` 不存在、旧 judge/反馈 reset action 已分别 done。已有可信 manifest 的新终态不能被当作 legacy source；此情况须由 sealed source SHA 明确区分。

`reset-feedback` 将全部旧 `## 裁判反馈` 原文归档，临时文件重写 task 后 atomic rename；draft 保留。`archive-legacy-bad` 把旧混合 `.badN` 移出 outdir，不进入新 research/judge counter。

### 4.4 `done-all` 与启动放行

只有以下条件在 migration commit 时全部成立才 append+fsync `done-all`，再用 durable helper atomic 写 `DONE` 并 fsync epoch 目录：

1. `plan.jsonl` digest 与 `plan-sealed` 一致；
2. sealed plan 的每个 action_id 都有至少一个 checksum/postcondition 一致的幂等 done event，且无冲突 event；
3. 每个 action 的 postcondition 重新检查通过；import transport 使用上述 phase-aware handoff rule，不能在 semantic successor 合法 rewrite/archive 后仍要求中间 target exact；
4. old root 已不存在；
5. queue outdir 无 sealed plan 未解释的 legacy/housekeeping 文件，且任何 import transport 都已有依赖的 semantic action 达到 live terminal postcondition；
6. `DONE` 写入 migration bundle id/manifest digest、plan digest、expected action count、done action count 和 snapshot SHA；任一 event producer id 不同则不得完成。

`DONE` 是迁移时刻的 immutable receipt，不是 live queue 的永久 SHA 快照。重复执行 migration 时先从 intent/receipt exec exact pinned bundle，再校验 plan/events/DONE 的 producer id、immutable archive digest、old root 已消失；已由正常主循环产生 successor event 的 task/draft/final/frozen 状态交 runtime validator，不重新要求旧 live target 保持原 SHA 或仍存在，也不重放已完成 source action。任何单 key 的部分 action 完成均不能产生 `done-all`。

### 4.5 条件解冻

`frozen-out-of-pool` 不是科学终态。trusted 主循环对最新原子 `ledger.good` 调用 §3.6 同一 lineage/all-source selector，发现 winning row 重新满足池资格时，在共享锁内：

1. 归档并清除 task 中旧反馈，原子重写 task并记录 task transform；保留 draft；
2. append+fsync `unfreeze-prepared` runtime event，固定 frozen action_id、marker SHA、位于 `runtime-archive/unfreeze/` 的 event-addressed target 和新 task artifact id；
3. 校验 prepared 输入仍匹配后，把 `.legacy-frozen.md` atomic rename 到该 runtime archive target 并 fsync 两侧父目录；marker 移除是提交点；
4. append+fsync 确定性 `unfreeze-committed` event；
5. crash 后，source marker 仍在则按 prepared event 幂等续做；source 已无、archive 与 task transform 全匹配则补 committed event；source/archive 其它组合停止。runtime validator 先完成该恢复，再判断 live state，不要求 migration 的 frozen marker 永久存在。

## 5. 自动回灌桥（存储里程碑 #3）

### 5.1 最小 schema

| 表 | 关键约束 | 职责 |
|---|---|---|
| `lineages` | `lineage_key PK NOT NULL`；`root_candidate_id UNIQUE NOT NULL`；deferred FK/commit constraint 要求 root candidate 属同一 lineage | 不可变血缘身份；每条 lineage 只指定一个确定性根 candidate |
| `story_aliases` | canonical version/hash/bytes、lineage 均 `NOT NULL`；`UNIQUE(canonical_version, canonical_hash)` | 每个历史/修订 story 只能属于一个 lineage |
| `candidates` | candidate id/lineage/policy fields `NOT NULL`；`origin_stable_id UNIQUE NULL`；`expected_reviewer_count CHECK(N>=2)` | 每个历史 row/新提交各一条；claim 时先建 placeholder 并冻结独立 N 席配置 |
| `reentry_grants` | identity/FK/path/gate/evidence/rule/priority/state 均 `NOT NULL`；deterministic id 与 fact `UNIQUE` | Path A/B 独立资格证据 |
| `reentry_requests` | lineage/state/generation `NOT NULL` 且 lineage `UNIQUE`；claimed fields 按 state 成组 NULL/NOT NULL | readiness、story-once、claim；不复制 grant 资格事实 |
| `round_slots` | `round_id NOT NULL UNIQUE`；`slot_kind NOT NULL CHECK(slot_kind='reentry')`；state `NOT NULL`；binding fields 按 state 成组约束 | 三种 reentry 共用、每轮至多一个 commit |
| `reviews` | candidate/slot/producer call 与 artifact/policy refs 均 `NOT NULL`；两个复合 `UNIQUE` | 正式评审票据与独立 producer；同一 batch call 可服务不同 candidate |
| `artifacts`/`invocations` | artifact/call id、type/state/content/provenance refs `NOT NULL` 并用幂等键 | 可审计输入、输出与 provenance |
| `import_epochs` | epoch id/plan SHA/input manifest SHA/state `NOT NULL`；plan SHA `UNIQUE` | sealed historical union plan 与单事务导入 receipt |
| `materialization_outbox` | candidate/payload version+hash/state/generation/projection sequence `NOT NULL`；candidate `UNIQUE`；processing token/lease 按 state 约束 | 只承载新 bridge commit 的可 fencing、幂等文件 effect |

除明确声明可空的 `origin_stable_id` 及非 claimed/processing 状态字段外，所有 identity、FK、state、slot、review 与 outbox binding 列显式 `NOT NULL`，不依赖 SQLite `UNIQUE`/`CHECK` 对 NULL 的宽松语义。state-dependent `CHECK` 要求：claimed/processing 时整组 binding/token/lease 全非空，其它 state 按规范全空或保留 immutable audit fields；`slot_kind=NULL`、`round_id=NULL`、review slot/call NULL 均拒绝。每个 DB connection 先执行并验证 `PRAGMA foreign_keys=ON`，startup 跑 `foreign_key_check` 和 schema SQL/hash 校验；pragma 关闭、constraint/schema 漂移时在任何事务前 fail closed。

普通 ledger 导入不得逐 row 先落 provisional lineage。importer 必须先完成 §5.2 的 sealed equivalence/union plan，再在一个事务内写 `lineages`、`story_aliases`、`candidates` 及需要封住 story-once 的 historical-committed requests。未消耗的普通 identity import 不创建 request/grant，不得初始化成 ready。每条历史 canonical story 只注册到 plan 已确定的 lineage；同 canonical hash 但 canonical bytes 不同按 hash collision fail closed。

资格门事务写一条带证据和 rule version 的 grant，再按该 lineage 的 active grants 派生 request：

- request 不存在且至少一条 active grant：创建 `state=ready`；
- request 为 inactive 且重新出现 active grant：条件更新为 ready；
- request 为 claimed/committed：不创建第二行；claimed 的撤销语义见 §5.3。

历史已消耗 lineage 导入为 `state=committed, commit_kind=historical-import` 的 request，不创建 materialization outbox；它只封住 story-once，不代表新 bridge effect。

### 5.2 lineage 导入

新数据在首次 ledger commit 时由 orchestrator 生成不可变 `lineage_key`；进化、复查、复活只复制，禁止从改写后的 story 重算。`candidate_id` 每次提交新建。

历史 TSV canonicalization 固定为：Unicode NFC、trim、内部连续空白压成单空格、统一换行；标点和引号全部保留。NFKC 会兼容折叠不同命题，不使用。每个历史 data row 先由 §3.6 同一 helper 得到 `origin_stable_id`，存入对应 candidate；import candidate id 用 domain-tagged、length-prefixed `sha256("candidate-import-v1", origin_stable_id)` 确定性生成。snapshot SHA 只作 provenance，不进入 row identity，append 新行后旧 row id 不变。导入重试按 ledger instance + row number + raw SHA 读回既有 candidate；既有位置内容被改写/插行视为违反 append-only 契约并 fail closed。canonical hash 相同但 canonical bytes 不同则保留两边原文作审计并 fail closed，必须显式升级 canonical/hash schema，不能自动另分 key。

历史关系在任何 DB write 前恢复：

- 先为冻结 ledger 中每个 row 建 node；exact canonical、已验证 run archive `进化自` 父指针、tracked promotion attestation 和显式人工 mapping 形成 union edge。高相似只输出 mapping 候选，不能自动成边；归档缺失或父指针不可解析时停止相关 node，禁止 silent 新 lineage；
- 对全部 edge 做确定性 connected-components/parent-DAG 校验。component 若含一个既有 lineage，以它为唯一 anchor；含多个既有 lineage、父环或互相冲突的 mapping 时 fail closed，不能在 importer 内暗合并。无既有 anchor 时，唯一无父 ancestor 是 root；exact-canonical 的多个无父重复 row 取最小 `(row_number, origin_stable_id)`。若不同 canonical 的多个无父 node 由人工 mapping 合并，mapping 必须显式指定 root；
- 新历史 lineage key 对 root canonical bytes 调用 §3.6 同一 `canonical_story_v1/origin_lineage_key` helper；`lineages.root_candidate_id` 指向 root candidate。相同 canonical 的多个 row 各保留自己的 candidate/origin_stable_id，但 aliases 只把该 canonical 映到同一 lineage；
- 写 plan 前先把 ledger、run/archive parent-pointer inputs、promotion attestations/promoted receipt、人工 mapping/version 全部作为 immutable CAS snapshots 持久化，并 fsync objects 与 parent；canonical input manifest 只引用这些 artifact ids/SHA。plan 以 canonical JSON 固定 input manifest、nodes、verified edges、components、root/key、consumed 判定和 plan digest，作为 immutable CAS artifact durable 后才允许 DB write；live mapping/source 后续变化不得参与 resume；
- 只有 durable plan 全部无冲突后，单一 DB transaction 才 insert-or-verify `import_epochs(epoch_id,plan_sha,input_manifest_sha,state=done)`、全部 lineages/aliases/candidates/historical-committed requests，并让每条 import result 可追到 epoch/plan。任何 alias/anchor 与既有数据不符时整笔 rollback，禁止先建 `lineage_R` 再把 R 归并到 L；crash 前无 DB commit 则从 sealed plan 重放，commit 已成功则 epoch/done 与所有结果同在，不能出现只有 union 结果却无决策 plan 的状态；
- 同 lineage 有至少两个 candidate 行时按现行 story-once 口径标为已消耗；`promoted.tsv`/attestation 也导入为已消耗证据；
- 已发 key 永久冻结，canonicalization 版本升级不重算旧 key。

sidecar 的 local filename key 只作路径，不进入 DB 唯一约束。每个 task/final manifest 必须携带 §3.4 的 origin snapshot/row/raw SHA；导入时从该 origin snapshot 验证 row，按稳定的 row number + raw SHA 得到 `origin_stable_id`，解析到唯一 import candidate，再读取其 DB `lineage_key`。sidecar `origin_lineage_key` 是 §3.6 对所见 canonical story 的分组键；DB `lineage_key` 是 sealed union plan 的组件身份，只有该 row 是 component root 时才要求两者字节相同，child/人工归并 row 一律通过 origin candidate 映射，不能靠 key 相等猜测。当前 ledger snapshot 允许只在该行之后追加，不能要求 snapshot SHA 与 origin 时刻完全相同。改写稿 `R != L`、fingerprint 歧义或原 row 对不上时 fail closed，禁止对 R canonicalize 后另建 lineage；R 只能作为原 lineage 的 story alias。`promoted.tsv` 使用 §3.8 的 origin、formal commit 与 final artifact 字段，不只存 story 文本。

### 5.3 Grant、request 与动态资格

```text
无 active grant -> 无 request / inactive
active grant    -> ready --claim(bound grant)--> claimed --commit--> committed
                    ^                    |
                    |                    +--bound grant 撤销并 fence-->
                    |                         ready（仍有其它 grant）/ inactive
                    +----------------重新获得 active grant----------------+
```

near-SA queue 只是发现提示，不是 grant。Path A 的 grant 按 `PROGRAM.md:12` 固定成三种结构化谓词，并保存 evidence candidate、ledger row、reason_class、attempt count、rule/policy version：

1. `A-evolve`：latest `accept-w-rev && overlap=low`，死因是结构化实验设计类（强基线/统计功效/estimand/归因对照），非 novelty 封顶/已被占据，lineage 尚未消费；
2. `A-recheck-awr`：latest accept-w-rev 的死因是结构化查重薄弱类，原 story 重交，lineage 尚未消费；
3. `A-recheck-evidence`：latest reject 的 `category=evidence-incomplete`，且持久证据证明 `all_reviewers_sa_before_hard_gate=1`，lineage 尚未消费。

legacy free-text reason 无法唯一映射结构化 class 时进入人工映射，不能由 agent 猜测后激活。Path B grant 只由通过 final marker/runtime validator 的可信 `SA-可能` 生成，并在 grant 事务中重新验证 origin lineage 尚未消费、latest ledger 仍是 `source=hunt && accept-w-rev && overlap=low` 的结构化实验设计类可修死因，且非 novelty/ceiling 封顶；须带 origin lineage、latest eligibility row、final artifact、可信 judge gate 和 bridge policy version。所有 Path A/B grant 都是对 current latest ledger/consumption/evidence 的派生事实：任何 ledger append、promotion/commit 或资格证据变化都在同一事务重跑相关 gate，条件失效即撤销；Strong Accept、novelty-dead、high-overlap 或被占据的新 winner 会撤销旧 A grant。claim 前和 formal commit 内还须从 bound evidence + current latest row 重算所绑定 gate，不能只相信 `active=1`。自动桥启用前先合入 `PROGRAM.md` 的第三类父本规则。

`grant_id = sha256(canonical_length_prefixed("reentry-grant-v1", lineage_key, path, gate, evidence_id, rule_version))`；参与 identity/UNIQUE 的列全部 `NOT NULL`。资格事务以唯一键 upsert，同一事实重试不增行，并在激活新证据时撤销同 path/gate 下已被取代的 grant。active 状态只能由结构化 gate evaluator 写入，allocator 不自行补 grant；变长字段边界拆分的 golden fixture 必须得到不同 id。

每条 path/gate 独立保存 active/revoked 状态。allocator view 对每个 lineage 固定 `winner_grant = first(ORDER BY priority, grant_id)`，再按 `(winner_grant.priority, requested_at, lineage_key)` 排 lineage；claim 必须绑定该 winner 的确切 `grant_id`，不能只按 `MIN(priority)` 排完后任取 active grant。request 不保存可漂移的 eligibility/priority cache。撤销 B 不能清掉仍有效的 A，反之亦然。每次 claim 绑定一个具体 `claimed_grant_id`，不能用另一条未参与该 candidate 的 grant 替代。candidate/ledger append 与 grant 更新同一事务推进 request 状态：

- 撤销的 grant 未绑定当前 claim 且仍有其它 active grant：request 保持当前合法状态，只更新 effective priority；
- 最后一条 grant 在 ready 时撤销：`ready -> inactive`；
- claimed 所绑定的 grant 被撤销：同事务递增 `claim_generation`、清空 claimed grant/token/candidate/round/lease、把原 candidate 标 abandoned、把匹配 slot 标 cancelled；若还有其它 active grant 则回 ready，否则 inactive。运行中的旧 worker立即被 fence；
- inactive 重新合格：创建全新 ready generation，不能恢复旧 token；
- committed 永不回 ready。

Strong Accept、novelty-dead、无完整 Path A/B predicate 的 high/medium/near-SA lineage 没有 active request，不占名额。

### 5.4 Claim、lease 与 slot

SQLite 使用 `BEGIN IMMEDIATE` 串行化 allocator 写者；其它数据库使用等价 locking。A-evolve、A-recheck、Path B 一律申请 literal `slot_kind='reentry'`；`reentry_kind` 只存 candidate/grant，不参与 slot 唯一键。allocator 先处理当前 round 的 slot：

1. slot 为 `committed`：该 round 名额永久占用，不看 lease，不再分配；
2. slot 为 `claimed` 且 lease 未过期：本轮不再分配；
3. slot 为 `claimed` 且已过期：锁住 slot，按其中的 lineage/request generation/token 条件 fence 原 request；仍有 active grant 则原 request 回 ready，否则 inactive，原 candidate 标 abandoned，slot 标 `expired`；
4. slot 为 `cancelled/expired` 时才可由下一 claim 覆盖；fence 原 request 与释放 slot 必须同一事务。不得把 slot 从 A 换给 B 而保留 A 的有效 token。旧 attempt 由 candidate/request 审计记录保留。

slot 可用后，事务执行：

1. join active grants，只保留 `state='ready' OR (state='claimed' AND lease_until < DB-now)`；对每个 lineage 以 `(priority, grant_id)` 取唯一 winner，再按 `(winner.priority, requested_at, lineage_key)` 选择 lineage，并在事务内重算该 winner 的 current gate；
2. expired claimed request 先把旧 candidate 标 abandoned、递增 request generation，令旧 token 失效，并把仍匹配旧 token 的 prior-round slot 标 expired；旧 round slot 不阻断新 round reclaim；
3. 绑定上一步唯一 winner `grant_id`，创建 `candidate_id` placeholder，固定其 lineage、grant/evidence、`reentry_kind`、origin canonical story/artifact、允许的变更规则，以及 round policy 中已验证 `N>=2` 且满足全部 PROGRAM floor 的 `expected_reviewer_count=N`、exact slot set `1..N`、reviewer config/policy bundle id；运行中 `REVIEWERS` 改变不回写该 candidate；
4. 每次 claim 都递增 request generation，并写 `state=claimed, claimed_grant_id, claim_round_id, claimed_candidate_id, claim_generation, claim_token, lease_until`；
5. round slot 写 `state=claimed` 和同一 `(round_id, lineage_key, grant_id, candidate_id, request_generation, token, lease)`；任一条件更新/唯一约束失败则整个事务回滚。

所有后续写入匹配 request 的 lineage/candidate/round/generation/token/state，并匹配当前 `claimed` slot 的同组字段。`A-recheck-awr`/`A-recheck-evidence` 在 priorwork 前及 commit 时都要求 candidate canonical story 与 origin 完全相等，补查/补证只能进入 evidence artifact；`A-evolve` 固定 parent candidate/artifact SHA、origin lineage、candidate artifact SHA 与结构化 delta，delta 每个 changed field 携 parent/candidate 的 before/after hash，deterministic diff 证明至少一个允许字段真实变化，完整 candidate artifact 与 parent 相同则拒绝；Path B placeholder 同时绑定 trusted final artifact、其 manifest 指向的 terminal draft id/SHA、origin parent artifact 与版本化 extractor/diff，不能用非空文字 delta 代替实际变更。evolve/Path B 在 priorwork 前先查 candidate canonical hash 是否已映射到其它 lineage，命中即停止；该检查只是省调用，最终唯一性由 commit 事务保证。worker 续租使用短事务和 DB clock，条件匹配整组字段后同时延长 request/slot lease；任一行未更新即停止该 worker。reclaim 或 grant 撤销后旧 worker 的 token 永久失效。Path A generate 只填充已 claim 的 candidate placeholder，不另建 candidate 或 story-once 账。

### 5.5 Commit 与 materialization

candidate pipeline 先形成 role-separated invocation DAG：candidate source 是 installed generate call 或有版本的 recheck/Path-B deterministic transform；deterministic transform 继续反向追到所有影响 candidate 内容的 agent producers。priorwork 必须来自 installed `role=research` call；每张 review 必须来自 installed `role=review` call。所有 generate/research/review invocations 都使用与 §3.2/§3.5 等价的 registered outer read/write/process confinement、sanitized config 和 FD-based safe output ingestion；每个 formal reviewer mirror 只含冻结 candidate/priorwork/policy，OS read-deny真实 repo、兄弟席输出、其它 artifact/home。candidate 上游 agent、formal research、所有 formal reviewers 的 role 集合两两 disjoint，任何两席不得共享 `call_id/process_instance_id/context_id/session_or_resume_lineage`；同一 call/install edge 不能占多个 role/slot。相同内容 SHA 只有在确有不同 installed producer call 时才可被不同席引用。

冻结的 N 席完成后，短事务内：

- join 校验 candidate.lineage/grant 与 request/slot 的 lineage/grant/candidate/round/generation/token 全相同，request 仍 claimed；从 bound evidence + current latest ledger/consumption 重算该 Path A/B grant 仍合格，而非只查 active bit，并重验 §5.4 对 recheck/evolve/Path B 的 story、parent/delta 或 final-artifact 约束；
- 先要求冻结 `N>=2` 且 policy bundle 不低于 PROGRAM floors；从 candidate 反向验证完整 role-separated invocation DAG，priorwork 与冻结 slot set `1..N` 恰好齐全、无额外 slot，每票都有 contract 合法、confinement predicate 全过的 installed producer，输入 candidate/priorwork/policy SHA 一致，并满足 distinct producer/process/context 约束；空/单席集合或 cardinality 不等于 N 立即 fail closed；按 `(candidate,slot)` 与 `(candidate,producer_call)` 两个唯一键幂等写 reviews/artifacts；
- 事务内调用版本化 deterministic aggregator；aggregator 先断言 `N>=2` 且 cardinality=N，再从冻结票据取最低票并重算 novelty/feasibility 等硬门，空/单席集合没有 identity/default verdict；formal verdict 只写该计算结果，caller 传入值至多作一致性断言；
- 对 candidate canonical story 原子 insert-or-verify `story_aliases -> 当前 lineage`；若唯一键已指向其它 lineage，整个 commit 失败并转人工 lineage resolution，不能把同一 story 挂到两条 lineage；
- 同时把 request 与匹配 round slot 都置 `committed`，并插入 `materialization_outbox(candidate_id, effect_payload, state=pending)`；
- 任一谓词失败，整个事务无正式 effect；外部结果只归档为 attempt。

事务提交后，consumer 用短事务 claim `pending` 或 lease 已过期的 `processing` row，递增 generation、写随机 token/DB-clock lease；同一时刻只有匹配 token 的 consumer 有权推进。payload 固定 schema version、canonical bytes/hash 与 candidate id。单对象 effect 以 candidate_id 为唯一 target key：temp file fsync -> atomic replace -> target-parent fsync；existing same hash 也须重验文件并 fsync target parent，different hash fail closed。只有 durable target 重读 hash 后才能 mark。

集合型 TSV/export 禁止 blind append。DB 维护单调 `projection_sequence`，只在改变导出 projection 的业务事务（formal candidate/ledger commit、适用的 bulk import）递增；outbox claim/lease/renew/mark 等内部状态事务绝不递增。export 写 immutable `<projection_sequence>-<hash>` object，严格按 object temp/file fsync -> rename -> object-parent fsync -> pointer temp/file fsync -> rename -> pointer-parent fsync 提交；所有 claim/steal 与 pointer publish 都先取得同一 kernel advisory export lock。consumer 从 DB 当前全量 projection 构建 desired `(projection_sequence,hash)`；持锁后重验 outbox token、DB current projection sequence 与 desired sequence。pointer 不存在或 sequence 较小时才发布；pointer 与 desired 的 `(sequence,hash)` 完全相同表示 effect 已 durable 完成，无需再次 publish，即可仅条件 mark 当前 outbox row。相同 sequence、不同 hash 是确定性违约并 fail closed；pointer sequence 较大时丢弃旧 snapshot，从当前 DB projection 重建，且只有能从同一 DB 确认较新 deterministic export 已包含本 candidate commit 时才可 mark，否则 fail closed。这样多个 pending row 可由一次最新全量 export 合并覆盖，而不会因必须递增 pointer 永久 pending。

object、object parent、pointer、pointer parent 或单对象 target parent 任一 durability 步骤未完成时不得把 row 置 materialized。全部后置条件重读后，consumer 才以 candidate/generation/token 条件 mark；续租/收尾条件更新失败即停止。crash 在 effect 后、DB mark 前时，新 claim 不改变 projection sequence，新 token 验证同 `(sequence,hash)` durable target 后只补 mark，不重复 effect。历史 `commit_kind=historical-import` 不创建 outbox，因此不会被当成新 bridge effect。DB commit 后 crash 不重复正式 verdict，双 consumer 或任一 materialization crash point 只产生一个 candidate-keyed effect。

## 6. 交付顺序

1. Phase 0：启动真值表、hunt/legacy/new ordered-lock migration 硬门、失败分类、artifact DAG/原子 final commit、runtime validator、末稿必判、mirror 白名单、带 durable receipt 的 `ledger.good` 原子发布。
2. Phase 1：更新 `roles/awr.md`、`roles/awr-judge.md` 和 `check_draft`/`check_judge`，加入最强反例及逐节 gate。
3. Legacy migration：删除顶层 `agy-side -> awr-side` rename；先固定独立 migration release bundle，再以贯穿 intent/epoch/action/event/DONE 的 producer id 实施 sealed action plan，完成迁移后才放行 trusted 主循环。
4. Phase 2：latest-wins 单池 selector、7/8 列兼容、near-SA best-effort 去重。
5. 运维入口：同一提交更新 `README.md` 与 `awr-side.sh` 头注；所有可信示例同时设置 `SIDE_JUDGE_TRUSTED=1` 和显式 `SIDE_JUDGE_CMD`。Grok 在 OS 级副作用隔离验收前不列为 trusted judge。人工通道同时落 `promote` 管理命令、`publish.sh promote`、path guard 与 CI receipt 校验。
6. 存储 #3：先落 lineage/grant/request/outbox schema 和 TSV 导入，再接 claim-bound candidate/slot、正式评审 commit 和 materialization。

Phase 0、1、migration、2 是实现/测试拆分，不是可独立启用的 production states。直到 entry、outer profiles、roles/parsers、identity/CAS、provenance/current pointers、migration、selector 的兼容版本全部通过并原子发布同一个 `P1_RELEASE` manifest 前，trusted 分支固定 `exit 2`，不得调用 agent 或生成终态；`migrate-legacy` 可单独恢复但不解锁主循环。启动期把实际 component hashes/schema versions 与 release manifest 精确比对并固定完整只读 release bundle；每次 queue scan/call preflight 重验该 bundle 与 process pin，任一 mixed/old/mutated/missing component 均 fail closed，release 升级必须重启。只设置 judge 两变量的示例仅在 `agy-builtin` research outer profile 已登记并通过 probe 后成立；否则示例必须同时给合法 `SIDE_RESEARCH_CMD`。

Phase 0–2 不改 `PROGRAM.md`、`brainstorming_policy.md` 的 human-only 协议。自动桥落地时同步加入“复活稿再入场”：与进化/复查共用一个 request/slot，按 lineage 至多一次，不继承旧票，重新执行 priorwork 与冻结的 N 席评审。

## 7. 验收矩阵

### P1

- 启动矩阵覆盖 dormant、单变量误配、非法 backend、trusted、migrate；dormant 对 queue tree 的 before/after hash 相同且 fake agent 调用数为 0。trusted/promote 的 `REVIEWERS=0/1`、负数、非整数或越界值，以及 `PRIOR_MIN_LINKS<5`、`PRIOR_MIN_API<1`、`MIN_READ<5`、`AXIOM_MIN_CRACKS<2` 均在 agent 调用/reservation 前 `exit 2`。Phase 0/1/2 mixed component、旧 role + 新 parser、新 role + 旧 parser 和未登记 research profile 均在 agent 调用前拒绝。trusted probe 必须无法按绝对路径读取真实 repo/其它 key/home，无法写 staging 外路径或调用 git/gh，并证明 Codex/Claude 用户 config、hooks、plugins、MCP 未加载。
- live hunt/legacy process 持锁时 migration 拒绝；只有 `migrate-legacy` 按固定顺序同时排他取得 hunt/old/new locks 后才可检查 baseline 或迁目录。trusted 只在 legacy inhibit + new lock 下运行；空安装必须释放并转三锁 migration bootstrap。旧进程在 absent-check 与 empty `DONE` 之间尝试取得 old lock/创建 root 的 fixture 中，三锁重验必须看到并拒绝或由 inhibit 阻止，不能 seal 虚假 expected=0 receipt。migration 持兼容 owner-dir symlink hunt lock 时，旧 hunt acquisition 必须读到 live pid、退出且不得 unlink lock；磁盘 `hunt.sh` 未匹配 cutover hash时 migration 先拒绝。旧 plain-`cp` writer 暂停在合法行边界时 migration 拿不到 hunt lock；writer crash 后留下无 receipt 的合法截短 prefix也必须拒绝。agent 阶段向 live `ledger.tsv` 写入结构合法假行后 SIGKILL 的 fixture 中，migration 不得自动 reseed；只有可验证 git/run authority，或独立显式 operator adoption 的 exact SHA attestation可建立 baseline。release A seal/部分 action 后 crash、wrapper 升到 B 时，resume 必须重新 exec 保存的 A migration bundle并让全部 plan/event/DONE producer id 保持 A；A bundle 缺失/变异或当前 P1 release 不信任 A receipt 时在 exec/source mutation 前拒绝，B 不得混写。old/new 并存时 non-conflicting object 全部进 sealed import actions，异 SHA collision fail closed；hardlink 到其它 key/ledger、symlink、FIFO/device 与 scan/open swap fixture 均在 planning 前拒绝。
- lock acquisition 在 symlink syscall 前/后 kill 都可恢复且 receipt 不为空；stale receipt 按 pid+start-token 清理，未知 legacy directory 只能走显式 recover-lock，任何 fixture 均不得产生双 owner。无 judge env 的 `reopen` 仍能持 new lock 完成指定 seat 归档且 fake agent=0；未知 legacy lock 只有 exact digest + explicit stale confirmation 经 recovery guard quarantine/receipt 后，后续 migrate 才能取得普通锁，live/remote-unverifiable owner 必须拒绝；`promote` 不得误入 dormant。
- `SA-可能` missing「最强反例」、近邻不足、API 缺失/裸 endpoint/空或占位 query、实读不足、feasibility 空字段、clear-accept 空节、形态降级、裂缝格式漂移、缺 `AGY-DONE`，以及 reject 缺 `- 缺陷:`，均被 fixture 拒绝；合法 shape + `- 缺陷:` + `AGY-DONE`、不含三节的 `还不行` 必须分类为 content reject 并可回灌反馈。收紧为 `PRIOR_MIN_LINKS=7`、`PRIOR_MIN_API=2`、`MIN_READ=10`、`AXIOM_MIN_CRACKS=3` 时，parser 使用冻结 effective values，6/1/9/2 的证据分别拒绝。
- provenance crash fixture 覆盖 prepared 后 kill、started 后 kill、completed 后未安装、current-pointer rename 后未记 installed、final rename 后未写 marker；新 completed call 与旧 current cache 内容同 SHA、但 pointer 未绑定新 call 时不得补 installed。输出回收 probe 覆盖 symlink 指向其它 key、hardlink、FIFO/device、超大文件与 lstat/open swap，均不得读出或安装。manifest 只归因完整 artifact DAG 上的 installed calls/transforms，非空终态无 marker 时拒绝。
- 启动后单独改 role/parser/policy/adapter/outer profile 等任一 release component，下一 queue scan/call 只能继续使用已固定旧 bytes 或在 exec 前拒绝，不能把 mixed release 当作普通 drift；升级必须新进程。外部 model/effective config/execution identity 跨调用变化时 manifest 分列实际 invocation bundle SHA 并标 drift；finalize 时再改工作树不改变 manifest。argv/env/settings/plugin/hook/MCP 或外部 CLI/package dependency tree 任一未固定时不得标“配置可复现”；仅 post-exec 观察非冻结 Node/Python package 的 fixture 必须得到 false。
- infra 不涨 bad；contract 只涨对应 seat；人工 reopen 归档 bad 后再归档 marker；content reject 才涨 rounds。
- rounds=3 的最新 draft 仍被 judge，judge 输出与 finalize 的 draft SHA 一致。
- `ledger.good` 每个发布点的并发 reader 只读到完整旧版或完整新版。
- `origin-row-v2` golden fixture 覆盖 header、LF/CRLF、append 后旧 row、snapshot SHA 改变与重复 raw row；P1 task/receipt 和 #3 importer 对同一 data row 得到同一 `origin_stable_id`。
- selector 先对全部 source 做 lineage latest-wins；旧 hunt AwR 后追加 weekly SA/reject 时不再入池，后续合法 hunt row 才可重新成为 winner。
- durable directory fixture 在 epoch/archive/CAS/provenance 每级 mkdir 与 parent fsync 前后注入 crash；任何 source mutation 前整条 target path 可达。migration 在 snapshot、plan build、plan seal、target rename、target-parent fsync、source delete、source-parent fsync、event append 各 crash point 可恢复；both-root fixture 验证 imported terminal 仍必须经过 semantic action。unfreeze 在 task rewrite、prepared event、marker rename、parent fsync、committed event 各 crash point可恢复；尾部截断和同 event_id 重放幂等，第二次完整运行零迁移动作。
- target 已存在而 legacy source 尚在时，resume 删除/reconcile source 后才记 done。imported task 在 durable handoff 后被 `reset-feedback` rewrite、imported terminal 被 archive/freeze 后，done-all 通过绑定 transport/source SHA 的 `input-accepted` 与 successor postcondition验收，不因中间 target 消失而卡死。
- 同 key 两个 sealed action 只完成一个时不得写 `done-all`；每个 action_id 完成且 postcondition 通过后才写 `DONE`。
- canonical action-spec golden fixture 先得到稳定 action_id、再派生带 id 的 archive path；重复 planning 字节一致且不存在 target-path/id 自引用。legacy filename 含 TAB/LF/control 时在 planning 前安全拒绝，不能注入额外 plan/event object。unfreeze 后重跑 migration 时 epoch archive digest 不变，runtime archive 只由 matching event 验证。
- sealed plan 外未知文件、snapshot/digest 不符、source SHA 漂移均阻止 `DONE`；`DONE` 后新增/变化的 queue object 必须有合法 runtime successor/provenance，不能凭 DONE 获信。
- 当前 fixture 的 latest-wins in-pool 终态全部进入可信重判；out-of-pool 终态均有单一 `legacy_status` marker；在修 task 的旧反馈全部清零；旧 bad 不进入新 counter。
- 每个 P1 `SA-可能` 均满足 `check_judge`、可信配置 manifest 和 producer chain，可人工重评；其数量不作成功指标。
- promotion context 会拦截主环普通 auto-publish；pending reservation、formal-committed-but-unpublished、published receipt 三种状态下，普通 Path A 都不能再次选择该 lineage，只有 commit 前显式 abort 可释放。formal commit 前 crash 不产生 consumed receipt；commit 后 attestation/receipt/publication 各 crash point 可按同一 promotion_id 重放 invocation，最终仅有一个匹配 content digest 的 branch/commit/PR logical effect，且 publication journal 达到 completed。promotion 的 generate/research/N-reviewer mirrors 在 `REV_STAGGER_SEC>0` 时仍无法用绝对路径读取真实 repo/兄弟席/先席输出，复用 call/process/context/resume lineage 或未隔离 `REV_CMD` 均在 formal commit 前失败。最终 PR 同时持久化 formal ledger row、对 origin_lineage_key 唯一的 `promoted.tsv` receipt 与可独立验 hash 的 attestation；相同 canonical story 的不同 rows/snapshots 不能各 promote 一次，普通 publish 模式不能夹带 receipt/attestation。

### 自动桥

- 普通 SA、novelty-dead、无完整结构化谓词的 high/medium/near-SA 导入只创建 lineage/candidate，不创建 grant/ready request；三种 Path A gate 各有正反 fixture。
- A/B 同时 active 后撤销未绑定当前 claim 的一路仍保持另一路；撤销当前 bound grant 会原子 fence claimed token，并按剩余 active grant 转 ready/inactive；重新合格只产生新 generation。同一 lineage 多 grant 以 `(priority,grant_id)` 固定 winner，A/B 同优先级并发重跑仍绑定同一 grant/candidate policy。
- A-evolve/A-recheck 激活后若 append 的 latest winner 变成 Strong Accept、novelty-dead、high-overlap 或不再满足结构化 gate，append 事务撤销 stale A 并 fence 已绑定 claim；旧 worker formal commit 为零 effect。Path B 使用同一动态重验规则。
- worker 在旧 round claim 后崩溃，新 round能选中 expired claim 并换 generation/token；旧 token commit 影响行数为 0。
- 同 round 的 evolve/recheck/Path B 混合并发只竞争一个 `reentry` row；过期 slot 从 A 换给 B 时 A 在同事务被 fence。slot 一旦 committed，即使 lease 时间过去也不能再分配，本轮并发或串行均最多一个 commit。
- candidate 属错误 lineage、recheck 改写 origin story、evolve/Path B artifact 未真实变化或 delta/diff 不匹配、Path B 不匹配 trusted final，或 candidate/token/generation/round/slot、role DAG、confinement、producer independence、reviewer slot、输入 SHA、caller verdict 任一校验失败时 commit 为零 effect；复制一份 review/call/install edge 填多席、candidate producer 与 formal research/reviewer 复用 resume lineage、reviewer 绝对路径读取兄弟席/真实 repo、未隔离 `REV_CMD` 回落的 fixture 必须失败。N=0/1 或弱于 PROGRAM floor 的 candidate/schema/aggregator 均拒绝；N=5 时只到 slots 1–3、缺 4/5 或中途把环境改回 N=3 均不得 commit。
- DB commit 后、文件回写前 kill：重启不重复 verdict，只重放 outbox；historical-import committed request 不创建 outbox。
- 两个 outbox consumer 并发、claim 后 kill、effect rename 后 kill、lease 过期接管和 stale token 收尾 fixture 中，existing same-hash target 只补 mark，different-hash target fail closed。object file/object parent/pointer file/pointer parent/单对象 target parent 每个 fsync 前后注入 crash，DB mark 只能晚于全部 durable postconditions。集合 export 另覆盖旧 consumer 构建 S 后暂停、新 consumer 发布 S+1、旧 consumer 恢复；kernel lock + token/projection-sequence recheck 必须拒绝旧 pointer rename，TSV 不回退或重复 row。A(seq1)、B(seq2) 同时 pending 时，A 可发布最新 seq2/hash 并 mark A，B 随后把相同 `(seq2,hash)` 认作已完成并 mark B；一次 full export 合并的全部 pending rows 最终均 materialized。effect 后 crash 再 claim/renew/mark 不递增 projection sequence，只验证同 pointer 后补 mark。
- 历史复查、进化父指针、sidecar origin fingerprint、`promoted.tsv` 和人工映射 fixture 均保持 story-once；无法证明的父子关系 fail closed。历史 L -> R 必须在首次 DB write 前由 sealed union plan 归入同一 lineage；exact canonical 的重复 rows（含真实 ledger 重复样本）生成多个带各自 `origin_stable_id` 的 candidates、一个 deterministic root 和一个 lineage，重跑 import 字节/行数不变。全部 input snapshots/plan CAS durable 后、DB commit 前后 kill 的 fixture 中，resume 只用原 epoch plan；外部 mapping 改变不改结果，epoch/result/done 要么全无、要么同事务全有。
- Path B/evolve 修订稿若 canonical 到已有其它 lineage，或两条 lineage 并发生成同一新 story，至多一条 alias/commit 成功；另一条为零正式 effect。
