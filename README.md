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
