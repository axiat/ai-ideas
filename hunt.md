# Idea Hunt — 主动触发入口

`./hunt.sh` 驱动的流水线,不再由单个 agent 一趟跑完。协议见 `PROGRAM.md`;各角色 prompt 在 `roles/`。全程中文。

## 一轮的流程

`hunt.sh` 按序调起三个**互不共享 context** 的独立进程,再由脚本自身(非 agent)聚合定谳:

1. `roles/generate.md` —— 先发散 10 个候选,再自筛 4–6 个差异最大的 idea(只生成,不查重、不打分)。
2. `roles/research.md` —— 对抗式查重,尽力证明每个 idea 已有人做过,产出独立的近邻工作证据;每个 idea 至少 3 条带链接近邻。
3. `roles/review.md` —— 打分,跑 `REVIEWERS` 次(默认 3),每位裁判默认 Reject、互不通气、也不知道停机条件。
4. **bash 聚合**:每个 idea 取各裁判的**最低** verdict(Strong Accept 需全票),追加进 `ledger.tsv`。
5. 有全票 Strong Accept → `roles/report.md` 组装 `ideas/YYYY-MM-DD_hunt.md`,脚本调 `./publish.sh` 发布。

verdict、ledger 写入、publish 都归 orchestrator;agent 只写 `tmp/`(草稿)与 `ideas/`(报告),改不了判定。

## 参数

- 停机条件:当日出现 ≥1 全票 Strong Accept 即停;之前持续循环,不询问人。
- 幂等:`ideas/` 已有当日达标报告则直接结束(供重入)。
- 前段空产出或查重结构不达标:先按正常无达标区间短重试,连续 `EMPTY_MAX` 次才升级异常冷却。
- 调研范围:不限时间窗,允许经典与跨领域来源。
- 输出结构:1 关键文献 · 2 达标 idea(完整评审表 + 定向查重记录)· 3 被拒简表 · 4 元信息(尝试轮数、评审日期)。
