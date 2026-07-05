# Weekly Embodied Idea Scout — 定时 routine 入口

角色:具身智能方向研究助理。读仓库根 `PROGRAM.md`,按其回路执行。全程中文。

> **本入口是单个云端 agent,拿不到 `hunt.sh` 的进程级隔离。** 反串通只能靠下面的自律纪律近似(默认 Reject、对抗查重、三遍取最低、SA 硬门槛自检),弱于 hunt 的独立裁判取最低票。严格度以本文件为准,不得自行放宽。无 orchestrator 代写 ledger,本 routine 自己记账。

## 调研范围

过去 7-14 天,WorldModel(视频预测、latent dynamics、model-based RL for robotics、可交互世界模型等)与 VLA(架构、训练范式、action tokenization、flow matching / diffusion policy 头、RL 后训练等)各至少 5 篇。来源:web search + arXiv cs.RO / cs.CV / cs.LG 近期列表。

## 分阶段执行(强制按序;后一阶段不得回头救前一阶段的结论)

1. **生成**:读 `ledger.tsv`,避开所有已有行(含已拒的;唯一例外:进化/复查共用 1 个名额,父本资格见 `brainstorming_policy.md`——进化限 overlap=low 且死因属实验设计类的 accept-w-rev 行,查重薄弱型只能复查、同 story 至多一次),先发散 10 个候选,再自筛 4-6 个差异最大的 idea。已反复被拒的套路(如 CPU/数据库/内存机制迁移)本轮至多 1 个。每个 idea 标注主题(policy 主题词表)并附**最小否证实验**(数据 × 算力 × 预期信号;点名最强基线、给样本量与预期效应)。只描述 idea,**禁止**在此阶段写任何「novelty 强 / 没人做 / 能过」之类自评。
2. **预筛(只杀不保)**:对每个候选做 1-3 分钟廉价 direct-hit 预查(≥1 条 arXiv/Semantic Scholar API 检索记录)。存在**单篇**工作、读摘要即可确认覆盖头条 → 按 reject 记账(reason 以「预筛直接占位:<链接>」开头,overlap 记 high),不进深查;拿不准一律放行,keep 不构成任何 novelty 结论。存活至多取 3 个进入后续阶段。
3. **对抗式深查重**:对每个存活 idea,尽力证明已经有人做过——先 direct-hit 猎杀,再按问题表述/方法机制/相邻领域三类铺开检索。每个找最相近 5-8 篇,写实读摘要/方法与**实读篇数**,附**最强反例**行(单篇最近邻 + 差异是否足以支撑 clear-accept),且每个 idea 块至少 5 条带链接近邻、至少 1 条 API 检索记录(实际 query URL)。编号自查须实际打开链接核对标题。后续 novelty 结论**只认这一步的证据**,不认生成阶段的说法;查重薄弱本身按 MAJOR 计。
4. **打分(独立跑 3 遍,取最低票)**:默认结论 **Reject**。每遍当作一次全新的对抗评审,按 `rubric.md` 8 步完整走、用 `brainstorming_policy.md` 校准,给出 verdict ∈ {strong-accept, accept-w-rev, reject}。三遍各记一行,**该 idea 的最终 verdict = 三遍最低**。
   - MAJOR **只增不减**,无视 idea 自带的任何 defense/缓解说辞;含 ≥2 MAJOR 封顶 accept-w-rev,含 CRITICAL → reject。
   - novelty 仅由第 3 步证据支持:任一近邻工作与头条发现重叠、又给不出 clear-accept 级差异 → novelty 封顶,不得 SA;实读不足或编号自查存疑 → novelty 记未证实,同样封顶。
   - 可行性基线:单人执行 + 默认 1×H100 80G;生命周期内单人做不完 → 封顶 accept-w-rev,依赖追加算力须显式注明。feasibility 只认该 idea 的最小否证实验,缺失或不可执行按 MAJOR 计、封顶 accept-w-rev。
5. **Strong Accept 硬门槛自检**:某 idea 要留在正文,必须**同时**满足——三遍**全部** strong-accept、有该 idea 的定向查重块(含 API 检索记录)、实读篇数 ≥ 5、附最小否证实验、写了完整 rubric 8 段评审。任一不满足 → 降级 reject,移出正文。做出来达不到 clear accept(≈6,6,8)/冲不了 oral 的,一律不给 SA。
6. **记账**:所有生成过的 idea(无论 verdict,含预筛杀的)各写一行进 `ledger.tsv`(7 列 schema 见 PROGRAM.md,行末 overlap 列取查重的重叠判定,只追加、不改历史行);正文只保留 Strong Accept。

## 达标条件

至少 1 个 Strong Accept;最多 10 轮。10 轮未达标则正常结束,如实写「本周无达标 idea」,附最接近的至多 2 个及差距分析。**宁可无达标,不可放水凑数。**

## 输出 `ideas/YYYY-MM-DD_weekly_ideas.md`(当天日期)

1. 本周文献综述(WorldModel / VLA 分节,含链接)
2. 趋势与 gap 分析
3. 达标 idea(仅 Strong Accept,每个附完整评审表 + 三遍 verdict 小表 + 定向查重记录)
4. 附录:至多 2 个 borderline(accept-w-rev)
5. 被拒 idea 简表(一句话 + 拒因)
6. 元信息:尝试轮数、评审日期、每个 SA 的三遍 verdict

## 收尾

执行 `./publish.sh weekly`(提交 `ideas/` 与 `ledger.tsv` 到 `weekly/当日` 分支、推送、开 PR);不直接使用 git/gh。

成功标准:文件已提交,正文保留的 idea 均为 Strong Accept(或如实报告无达标)。
