# 开发规划

状态：持续维护
更新：2026-07-12

目标：将 `ai-ideas` 建成可校准、可审计、与研究主题解耦的研究 Harness，并逐步补齐并发、可移植部署和产品入口。

编号沿用原计划；`6` 保留未分配。

## 计划总览

| ID | 分类 | 计划 | 优先级 | 主要依赖 |
|---|---|---|---|---|
| 0 | Harness 工程 | 尽可能将确定性逻辑固化为 `.sh`，减少自然语言控制；优化 Claude / Codex 适配代码 | P0 | 无 |
| 1 | 研究质量 | 优化 autoresearch 成功率（w/ sol） | P0 | 0 的判定与观测基础 |
| 2 | 架构 | 解耦 Harness 与研究主题、生成内容 | P1 | 0 |
| 3 | 存储 | 评估并迁移 `ledger.tsv` 至轻量数据库 | P1 | 2 的数据边界 |
| 4 | 执行 | 允许安全并发 | P2 | 2、3 |
| 5 | 文档与结构 | 重写用户友好的 `README.md`，并决定是否重构仓库结构 | P1 | 2 的边界设计 |
| 7 | 交付 | 容器化 | P2 | 2、5 |
| 8 | 产品 | 产品化 | P3 | 0–7 的稳定接口 |

## A. 研究质量

### 1. 优化 autoresearch 成功率（w/ sol）

#### 当前基线

- `ledger.tsv`：209 个 idea，155 AwR，54 Reject，0 SA。
- `tmp/hunt.metrics.tsv`：96 个候选完成三席评审，288 张票中仅 6 张 SA；3 个候选得到 `2×SA + 1×AwR`，无全票 SA。
- 59 次有指标记录的尝试中，32 次到达 verdict，27 次停在 empty/fail。
- `tmp/sa-potential-ideas.md` 是动态候选池；其中的 `SA-可能` 未获正式主环确认，不计作 SA。

#### 优化指标

| 指标 | 定义 |
|---|---|
| 校准正确率 | gold positive / negative 能否被稳定区分 |
| 候选质量率 | 正式候选中至少获得 1 张 SA 票的比例 |
| near-SA 转化率 | `2,2,1` 或 `1,2,2` 修订后转为全票 SA 的比例 |
| 最终 SA 命中率 | 正式候选中全票 SA 的比例 |
| 运行完成率 | 尝试中到达 verdict 的比例 |

运行完成率与 SA 命中率分别统计；机制可用不等于研究质量已经提高。

#### P0：统一判定与完整观测

- [x] 以 `brainstorming_policy.md` 的 clear-accept 标准为唯一 SA 定义，消除 `rubric.md`、各 role prompt 与 sidecar 的冲突口径。（2026-07-12：rubric Step 8 与 Integrity gate #5 改为指向 policy；review.md 撤销「且能冲 oral/spotlight」加严；awr-judge/trigger/README 错误指向修正）
- [x] 建立具身领域 gold set，覆盖普通 method、benchmark/new problem、删承重假设和 direct-hit 阴性。（五 case + `expect` 机器判读 + `calib/run_all.sh` 批跑打分；具身删公理正式阳性待 2026 秋会议揭晓，现以跨域探针 pos-axiom-adam 代位）
- [x] 分开验证冻结 `ideas + priorwork` 的 verdict 校准，以及允许真实检索的端到端校准。（冻结=`calib/run_all.sh`，端到端=`calib/run_e2e.sh` 检索召回侧；阳性对照无端到端跑法——已发表工作会被真检索判成自占据）
- [x] 为每次运行生成稳定 `run_id` / `candidate_id`，记录来源、backend、policy 版本、阶段时间和退出原因。（run_id=启动时间+pid+轮次，candidate_id=`<run_id>/I<n>`；manifest + `stages.tsv`）
- [x] 按运行保存 `ideas`、`priorwork`、三席票向量、完整理由、聚合结果和检索故障；ledger 只保留摘要。（轮终点归档 `tmp/runs/<run_id>/`：tmp/round 全量 + manifest + ledger 增量 + 逐阶段日志）

验收：已知 A 类阳性可复现地全票通过，direct-hit 阴性保持全票 Reject；任一 ledger 结论可还原输入与判定过程。

#### P1：提高候选质量与 near-SA 转化

- [ ] 生成负责发散；独立 selector 按 novelty 证据、clear-accept 上限、最小否证实验和可执行性排序。
- [ ] `roles/research.md` 只报告 prior-work 覆盖事实，不提前裁决 clear-accept 上限。
- [ ] 区分 `direct-hit`、`medium-overlap` 与检索不完整；检索不完整先补查，不进入正式定级。
- [ ] 将非 SA 分为 `novelty-dead`、`evidence-incomplete`、`design-fixable`、`ceiling-limited`。
- [ ] 保存 revision lineage 和明确 delta；near-SA 队列优先于盲目扩池。
- [ ] direct-hit / CRITICAL 才进入永久禁复活集合，其余结论保留可审计的复查条件。

验收：至少一张 SA 票的候选比例提高；每个 near-SA 均有补证、修订、重评或判死的终态。

#### P1：重建 AwR 复活链路

- [ ] 入队顺序为主环 near-SA、low-overlap 且 design-fixable、evidence-incomplete。
- [ ] 裁判席使用独立可信模型；通过时逐项保存 novelty、feasibility、clear-accept gate 的证据。
- [ ] `SA-可能` 只授予主环复审资格；修订稿重新执行 priorwork 与三席评审。
- [ ] 保存 backend、模型与 policy 版本；第三轮反馈后的最新版必须重新评审。

验收：每个 sidecar 通过项都有可审计理由和正式主环 verdict。

#### P2：减少无效运行

- [ ] 分别记录结构、API/网络、模型和内容失败；基础设施失败不得形成永久 idea 结论。
- [ ] 单候选失败只重试该候选，合格的前段产物支持安全 resume。
- [ ] 保存超出 `SHORT_MAX` 的候选及 selector 分数，支持后续重排。
- [ ] 每个配置 epoch 只改一个变量，在固定校准集和评审预算下比较。

验收：运行完成率提高，同等正式评审数量下调用成本下降；`SA-可能` 数量不作成功指标。

## B. Harness 工程

### 0. Shell-first 与 Claude / Codex 适配

自然语言负责研究判断；参数校验、状态迁移、重试、聚合、归档和安全边界由脚本执行。

- [ ] 盘点 prompt 中可机械判定的控制逻辑，并迁移至 `.sh` 或共享库。
- [ ] 定义统一 agent adapter 接口：输入、环境变量、退出码、超时、能力声明、隔离和产物回收。
- [ ] 为 Claude 与 Codex 建立独立适配层，复用命令解析、临时镜像、日志和错误分类。
- [ ] 使用 fake agent 与小型 shell probe 覆盖解析、超时、失败恢复、路径边界和产物完整性。

验收：同一阶段可通过配置切换 Claude / Codex；非法配置快速失败；关键不变量不依赖模型遵守自然语言指令。

### 2. 解耦 Harness 与研究主题、生成内容

- [ ] Harness 只负责生命周期、调度、锁、重试、聚合、存储和发布。
- [ ] 研究主题包负责 context、brainstorming policy、rubric、role prompts 和主题资源。
- [ ] 运行产物与源码配置分离，避免状态写回主题定义。
- [ ] 用最小合成主题 fixture 测试 Harness，不依赖具身领域内容。

验收：新增或切换研究主题无需修改 Harness；主题 prompt 变化不影响调度与存储测试。

### 3. `ledger.tsv` 轻量数据库评估与迁移

优先评估 SQLite；保留 TSV 导入导出，不直接删除现有 ledger。

- [ ] 明确 idea、run、candidate、review、artifact、revision lineage 的最小 schema。
- [ ] 比较 SQLite 与继续使用 TSV 的查询、并发写、迁移和维护成本，形成迁移决定。
- [ ] 若迁移，先提供一次性导入、双读校验和稳定 TSV export，再切换主写入路径。
- [ ] 数据写入支持事务、唯一约束、幂等 resume 和 schema version。

验收：可查询历史运行、完整票据和修订链；重复执行不产生重复记录；现有 TSV 工作流仍可导出。

### 4. 允许安全并发

- [ ] 先并发候选级 research / review，再评估轮次级并发；每个任务使用独立工作目录和日志。
- [ ] 提供全局并发上限、backend 限流、资源锁、取消和失败重试。
- [ ] 聚合与持久化保持幂等，避免重复票据、文件覆盖和 ledger 写冲突。
- [ ] 固定输入下，并发与串行得到相同的候选集合和 verdict。

验收：并发运行无文件冲突和重复写入；中断后可安全 resume；结果不依赖完成顺序。

## C. 交付与产品

### 5. 重写 README，并决定仓库结构

- [ ] README 按用户路径组织：项目用途、前置条件、快速开始、核心配置、输出位置、恢复方式和故障定位。
- [ ] 详细内部机制链接到专门文档，README 不复制开发规划和策略正文。
- [ ] 在完成 Harness / topic 边界设计后评估目录重构；只有新边界能降低耦合时才迁移。
- [ ] 若重构，提供旧入口兼容层或一次性迁移说明，并同步脚本引用与文档链接。

验收：首次使用者可按 README 跑通最小示例并定位产物；目录结构能直接表达 Harness、主题、adapter 与运行状态的边界。

### 7. 容器化

- [ ] 固定 shell 与系统工具依赖，提供最小镜像和可复现构建。
- [ ] 明确 agent CLI、认证信息、仓库源码和运行产物的挂载边界。
- [ ] 提供容器 smoke test，验证生成、评审、resume 和导出路径。
- [ ] 比较 host 与 container 的产物格式和退出语义。

验收：干净环境可用单一入口启动最小流程；认证不写入镜像；host 与 container 产物兼容。

### 8. 产品化

产品化以稳定 CLI 和可审计运行记录为起点，UI 在接口与存储稳定后评估。

- [ ] 固化 `init`、`run`、`status`、`resume`、`review`、`export` 等用户动作及退出语义。
- [ ] 提供版本化配置 schema、示例主题、运行注册表和结果浏览入口。
- [ ] 建立可发布版本、升级路径和端到端验收流程。
- [ ] 基于实际使用流程决定是否增加本地 UI 或服务化入口。

验收：新项目可初始化、运行、恢复、审计和导出；版本升级不破坏已有配置与运行记录。

## 实施顺序

1. `0 + 1`：固化控制面，统一判定，补齐可观测性和校准。
2. `2 + 3 + 5`：划定架构与数据边界，完成存储决策和用户文档设计。
3. `4 + 7`：在隔离和事务基础上增加并发与可移植部署。
4. `8`：基于稳定 CLI、配置和运行记录形成产品入口。

在判定、观测和架构边界稳定前，不以扩大 ledger、提高并发或增加 UI 代替 SA 成功率验证。
