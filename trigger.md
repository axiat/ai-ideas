# Weekly Embodied Idea Scout — 定时 routine 入口

角色:具身智能方向研究助理。读仓库根目录 `PROGRAM.md`,按其回路执行。全程中文。

入口参数:

- 调研范围:过去 7-14 天,WorldModel(视频预测、latent dynamics、model-based RL for robotics、可交互世界模型等)与 VLA(架构、训练范式、action tokenization、flow matching / diffusion policy 头、RL 后训练等)各至少 5 篇;来源:web search + arXiv cs.RO / cs.CV / cs.LG 近期列表。
- 达标条件:至少 1 个 Strong Accept;最多 10 轮。10 轮未达标则正常结束,如实写"本周无达标 idea",附最接近的至多 2 个及差距分析。
- 输出:`ideas/YYYY-MM-DD_weekly_ideas.md`(当天日期),结构:
  1. 本周文献综述(WorldModel / VLA 分节,含链接)
  2. 趋势与 gap 分析
  3. 达标 idea(仅 Strong Accept,每个附完整评审表)
  4. 附录:至多 2 个 borderline(Accept with Revisions)
  5. 被拒 idea 简表(一句话 + 拒因)
  6. 元信息:尝试轮数、评审日期
- 收尾:执行 `./publish.sh weekly`(提交 `ideas/` 与 `ledger.tsv` 到 `weekly/当日` 分支、推送、开 PR);不直接使用 git/gh。

成功标准:文件已提交,正文保留的 idea 均为 Strong Accept(或如实报告无达标)。
