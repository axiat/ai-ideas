# ai-ideas

具身智能(WorldModel & VLA)idea 调研回路。仿 [karpathy/autoresearch](https://github.com/karpathy/autoresearch) 的分层:固定的回路协议 + 薄入口 + 实验台账。

固定层(人改,agent 只读):

- `PROGRAM.md` — 回路协议与不动项,所有入口共用
- `rubric.md` — idea 评审标准(整合自 idea-evaluator skill)
- `brainstorming_policy.md` — 发散规则与 verdict 校准
- `research_context.md` — 研究背景,可选灵感

入口层(每个触发场景一份,只含参数:调研范围、达标/停机条件、输出格式):

- `trigger.md` — 每周定时 routine("Weekly Embodied Idea Scout",远端 prompt 与此同步)
- `hunt.md` — 主动触发,循环直到找到 1 个 Strong Accept

产出层(agent 写):

- `ledger.tsv` — 所有生成过的 idea 台账(含被拒的),跨轮查重依据
- `ideas/` — 达标报告(`YYYY-MM-DD_weekly_ideas.md` / `YYYY-MM-DD_hunt.md`)

## 用法

**每周定时**:cloud routine "Weekly Embodied Idea Scout" 自动运行,远端 prompt 即 `trigger.md` 的内容;报告以 `claude/*` 分支 PR 提交,CI 自动合并。改了 `trigger.md` 需手动同步到远端 routine 配置;改 `PROGRAM.md` / `rubric.md` / `brainstorming_policy.md` 无需同步,routine 每次从仓库读最新版。

**主动找 idea(单次)**:任一 agent CLI(claude / codex / opencode 等)会话里:

```
读 hunt.md,开始
```

可加一句限定主题,如「读 hunt.md,主题限定 VLA 推理效率,开始」。会话持续"调研 → 生成 → 评审 → 记账"循环,找到 1 个 Strong Accept 才停,中途不询问;可手动打断。结果在 `ideas/YYYY-MM-DD_hunt.md`。

**主动找 idea(挂机,订阅额度友好)**:`./hunt.sh` 外层循环反复调起 agent,直到当日达标报告出现;额度耗尽导致会话中断时按间隔(默认 150 分钟)重试,额度刷新后自动继续——回路状态在 `ledger.tsv` 里,新会话不重做已评审的 idea。换 agent 用 `AGENT_CMD`:

```bash
./hunt.sh                                                 # 默认 claude -p
AGENT_CMD='codex --search -a never exec -s workspace-write' ./hunt.sh
AGENT_CMD='opencode run' ./hunt.sh
```

无人值守的边界分三层:

1. **工具策略**:claude 走 `.claude/settings.json` allowlist——文件写入只放行 `ideas/` 与 `ledger.tsv`,Bash 只放行 git/gh 前缀与无参 ls/date,未匹配操作在无头模式下自动拒绝(前提:本仓库已 trust,`~/.claude.json` 中 `hasTrustDialogAccepted: true`)。codex 走 OS 级 sandbox(`-s workspace-write` 写限仓库、`-a never` 不询问、`--search` 内置搜索),不需要也不建议 `--dangerously-bypass-approvals-and-sandbox`。两者都不是"固定层只读"的完整保证:git 前缀命令理论上可携带重定向,codex sandbox 只防仓库外。
2. **回路守卫**:`hunt.sh` 每轮结束校验本轮改动只落在 `ideas/` 与 `ledger.tsv`——越界的未提交改动自动回滚,越界的已提交改动停止循环留人工处理;全程日志在 `hunt.log`(gitignored),异常退出与"跑完但无达标"分类记录,连续异常达 `MAX_FAILS`(默认 12)即停,不做无限静默重试。
3. **CI 守卫**:auto-merge workflow 不看分支名,只按路径判定——本仓库分支的 PR 改动完全落在 `ideas/**` 与 `ledger.tsv` 内才自动合并,越界则跳过并留言,固定层改动必须人工 merge。

**看结果**:达标 idea 在 `ideas/` 报告正文(仅 Strong Accept,附评审表与查重记录);历史上所有想过的 idea(含被拒的及拒因)在 `ledger.tsv`。

**调 harness**:改评审严格度 → `brainstorming_policy.md`(verdict 校准);改发散方向 → 同文件发散要求节;改回路规则 → `PROGRAM.md`;改单次运行的范围/停机条件/输出格式 → 对应入口文件。agent 不会改这些文件,只写 `ideas/` 和 `ledger.tsv`。
