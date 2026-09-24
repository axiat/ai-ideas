## I1

Search Terms: 直接命中／问题表述：`robot interrupted actions pending execution belief state delayed observations action completion memory`、`robot "interrupted" "episodic memory" execution`；机制：`POMDP delayed action outcome observations execution monitoring options interruption`、`robot "pending" "actions" "belief state"`；相邻领域和基线：`"Delayed Observation Planning in Partially Observable Domains"`、`robot experience memory unfinished actions execution logs belief CRAM NEEM`、`"Between MDPs and semi-MDPs" "interrupting"`；执行协议与应用：`site:docs.ros.org actions canceling feedback result state machine`、`Deep Episodic Memory`、`Online Replanning in Belief Space`。
- Query: https://api.semanticscholar.org/graph/v1/paper/search?query=robot%20interrupted%20execution%20belief%20state&limit=8&fields=title,abstract,url
- Query: https://export.arxiv.org/api/query?search_query=all%3Adelayed%20AND%20all%3APOMDP&start=0&max_results=5

查询结果：2026-09-23，网页抓取工具访问上述 API 均失败；第二个 URL 随后经 `curl` 返回 HTTP 200、有效 Atom XML、totalResults=59 和 5 条摘要。D-POMDP 原文所在官方论文集因超过网页工具大小限制而抓取失败；随后经 `curl` 读取 PDF，并通过 `pdftotext` 的标准输入／输出在内存中提取和阅读印刷页 1235–1236，没有保存下载文件。

Nearest Work:

- The CRAM Cognitive Architecture for Robot Manipulation in Everyday Activities（2023） | https://arxiv.org/abs/2304.14119 | 已读摘要及[§III-F、§IV-B.2](https://arxiv.org/html/2304.14119v1)。机器人执行时持续记录感知、控制及语义叙述形成 NEEM；§IV-B.2 最后一段明确说明 active NEEM 在执行时即可用于诊断与恢复，其最新时刻对应当前 belief state。 | 直接占用“活动尚未完成时，经验记录即可服务后续控制”的前提移除和恢复用途。该段没有给出动作块被截断、回执延迟时的候选状态分布及其受控收益。
- Delayed Observation Planning in Partially Observable Domains（Varakantham、Marecki，AAMAS 2012 extended abstract） | https://www.ifaamas.org/Proceedings/aamas2012/print/volume3.pdf | 已读全文 pp.1235–1236；[作者机构记录](https://ink.library.smu.edu.sg/sis_research/1606/)核对标题与摘要。§2 定义观测延迟随机变量，明确允许未收到动作结果观测时执行后续动作；§3 将问题近似转换为扩展 POMDP，§3.1 在延迟观测到达时更新信念与策略。 | 候选的“结果未定即可决策，后续回执再更新状态分布”已有命名问题及方法先例。实验为 Tiger、maze 等离散基准，并未训练机器人情景记忆写入器或研究中断动作块前缀。
- Deep Episodic Memory for Verbalization of Robot Experience（2021） | https://h2t.iar.kit.edu/pdf/Baermann2021.pdf | 已读摘要、§III、§IV。§IV（PDF 第5页）把 started、running、interrupted、success、failure 纳入机器人经历流，动作或状态改变会立即建立新采样窗口；§III 使用 LSTM 记忆编码器与 Transformer 问答模块。 | 已将未完成和中断执行状态纳入情景记忆，记忆记录不局限于完整成功／失败标签。§III-A 的编码字段举例较简略；结果是经历问答，不能据此声称已证明即时恢复控制的收益。
- Deep Episodic Memory: Encoding, Recalling, and Predicting Episodic Experiences for Robot Action Execution（2018） | https://arxiv.org/abs/1801.04134 | 已读摘要、[§III-A、§III-B、§IV-F](https://arxiv.org/html/1801.04134v3)。编码器只读取视频前缀，两个解码器分别重建前缀和预测后续帧；测试时不提供未来帧，记忆嵌入用于经验匹配与动作复用。 | 覆盖“部分经历已可形成可用记忆并预测尚未发生后果”的表示机制。方法没有明确维护未决结果的概率分布，机器人示例也不是取消和延迟回执实验。
- Online Replanning in Belief Space for Partially Observable Task and Motion Problems（2020） | https://arxiv.org/abs/1911.04577 | 已读摘要、[§§III–VI](https://arxiv.org/html/1911.04577v2)。系统在混合 belief state 上规划动作与观测，获得新观测后重新规划，并优先复用上一计划尚未执行部分的结构，以保留任务进展。 | 在抽屉和厨房操作中覆盖不确定状态、执行进度保留及后续动作修订；§VI 的流程在每个动作执行后重规划，没有报告候选的动作内中断和迟到回执。
- ROS 2 Actions 设计文档 | https://design.ros2.org/articles/actions.html | 已读 Goal States、Cancel Goal Service、Get Result Service 和 Feedback Topic。协议区分 EXECUTING、CANCELING 与终止状态；接受取消请求只表示尝试取消，终止情况由状态／结果接口提供，反馈可在最终结果前发布。 | 工程协议已区分未决执行、取消请求及最终结果，支持候选的异步中断场景。反馈内容由应用定义，协议本身没有保证可重建实际执行的控制周期前缀，也没有实现概率情景记忆。

Strongest Counterexample: [The CRAM Cognitive Architecture](https://arxiv.org/html/2304.14119v1) — §IV-B.2 明确使用执行中的 active NEEM 及其当前 belief state 进行故障诊断与恢复，已经允许经验在完整活动结果确定前被使用。候选另外要求保存中断技能前缀和未决结果分布、随迟到回执修订，并在匹配输入和决策时刻的学习控制实验中测量收益，CRAM 的该段没有提供这组实验。

Overlap: high — 在线可用的未完成经验已见于 CRAM，结果观测到达前决策并随后更新信念已由 D-POMDP 明确定义，但所读来源没有验证候选的同条件机器人成功率优势。

假设与应用占用核查：完整技能结果先于一切经验使用并非上述文献的共同要求；CRAM 和 EMV 均记录执行过程，D-POMDP 明确放宽立即得到结果观测的假设。最接近候选恢复用途的是 active NEEM，最接近延迟结果处理的是 D-POMDP。后者采用模型规划，不能替代候选要求的最强可训练对照。所读文献没有在三项候选操作、相同输入及中断率下排列可训练方法，5 个百分点收益仍是待测阈值。Options 中断检索命中了相关原文片段，但全文地址未成功解析为 PDF，因此未计入近邻或 Papers Read。

Papers Read: 5
arXiv ID Check: yes

计数说明：五篇论文均读取摘要与方法；另读 ROS 2 官方协议，不计入 Papers Read。三个 arXiv 摘要 URL 均现场打开并核对标题；其余两篇使用官方论文集和作者机构 PDF。

## Crack Evidence Verification

候选的 Crack Evidence 字段没有提供任何 URL，因此原始证据链接数量为零，无法核验其所称“控制周期回执能够标识已执行动作前缀”的具体实现。以下是本次检索新增的来源核验，不表示候选原本已经提交这些证据；原始直接证据缺项仍保留。

- https://arxiv.org/html/2304.14119v1 | Verification: supports — URL 可达。§IV-B.2 明确说明 active NEEM 在执行时即可用于诊断和恢复，直接支持经验使用可以早于完整活动结束；该段未验证延迟回执下候选分布表示的收益。
- https://design.ros2.org/articles/actions.html | Verification: partial — URL 可达。Goal States、Cancel Goal Service 与 Feedback Topic 支持最终结果前存在反馈和未决取消状态，但反馈由应用定义，未保证识别每个已执行控制周期，因此只部分支持候选所称的实现条件。
- https://www.ifaamas.org/Proceedings/aamas2012/print/volume3.pdf | Verification: supports — URL 经 `curl` 可达，已读 pp.1235–1236 的 §2 与 §3.1；模型允许结果观测到达前继续决策，并在观测到达时更新信念，直接支持放宽立即得到结果的要求。它不构成机器人动作前缀回执或 learned episodic memory 的验证。
