# CHANGELOG

## 2026-07-05 中断恢复(未提交,待 review)

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

**待人工**:`trigger.md` 已改,远端 cloud routine "Weekly Embodied Idea Scout" 的 prompt 需手动同步。
