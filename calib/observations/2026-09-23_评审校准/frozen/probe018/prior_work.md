## I4

Search Terms: 直接命中／问题措辞：`language models prior knowledge context conflict counterfactual context training memory robots location bias`、`robot VLA "memory" "location bias"`；机制及修复：`"context-memory" "knowledge" conflict counterfactual fine tuning`、`"Large Language Models with Controllable Working Memory"`、`"DisentQA"`、`"Entity-Based Knowledge Conflicts"`；基线领域：`"RoboMME" memory bias`、`"MemER" robot memory "bias"`；跨学科：`episodic memory statistical prior spatial location bias learned frequency`；工程实现：`"memory" "prior" "counterfactual" robot policy github`。
- Query: https://api.semanticscholar.org/graph/v1/paper/search?query=parametric%20knowledge%20context%20conflict&limit=8&fields=title,abstract,url
- Query: https://api.semanticscholar.org/graph/v1/paper/search?query=parametric%20knowledge%20context%20conflict&limit=3&fields=title,abstract,url
- Query: https://export.arxiv.org/api/query?search_query=all%3Acontext%20AND%20all%3Aconflicts&start=0&max_results=5

API Outcome: Semantic Scholar 网页抓取失败，直接 GET 重试返回 HTTP 429。arXiv 网页抓取失败，直接 GET 返回 200，已阅读五条召回摘要；召回包含 context-memory conflict、context compliance 和 conflict-aware decoding。仅摘要读取的条目未计入 Papers Read，也未据此判定具体机制重合。

Nearest Work:

- Entity-Based Knowledge Conflicts in Question Answering | https://arxiv.org/html/2109.05052 | 已读摘要、§2 的实体替换、§3.3 的 memorization ratio、§4.2–4.4。论文形式化参数知识与上下文冲突，改变训练检索质量和模型规模以测量依赖变化，并以替换实例训练降低参数答案依赖。 | 已覆盖训练经历影响外部证据响应及反事实数据修复；其干预不是同骨干下的位置频率，输出是文本答案。
- Large Language Models with Controllable Working Memory | https://arxiv.org/html/2211.05110 | 已读摘要、§3 的 KAFT 构造、§4.2–4.5。方法把相关、反事实、无关及空白上下文分开监督；§4.2 观察更强模型可能更忽略冲突上下文，§4.5 通过移除反事实样本和改变上下文噪声做消融。 | 已覆盖“参数知识使外部记忆纠正不充分”以及反事实上下文监督这两个部分；尚未测量机器人位置先验强度与准确历史记忆的交互曲线。
- DisentQA: Disentangling Parametric and Contextual Knowledge with Counterfactual Question Answering | https://arxiv.org/html/2211.05655 | 已读摘要及 §2.1–2.3。同一个问题配原始、替换、空白或随机上下文；替换答案实体后改变上下文答案标签，同时保持参数答案标签，训练模型分别输出两类答案。 | 已覆盖通过配对反事实上下文使输出随有效外部证据变化的训练机制；该文没有动作输出或位置均衡微调对照。
- Studying Large Language Model Behaviors Under Context-Memory Conflicts With Real Documents | https://arxiv.org/html/2404.16032 | 已读摘要、§3 的闭卷筛选／开卷测试、§4.4 的遮蔽及添加参数答案干预、§5 讨论。论文在真实正确文档下正式定义 correct update 和 retain parametric，并发现错误参数答案出现在文档时会增加更新失败。 | 已命名 parametric bias，并直接研究准确外部证据的纠正失败；它也报告真实文档通常能纠正旧答案，不能支持“强先验总会阻止纠正”的无条件表述。
- RoboMME: Benchmarking and Understanding Memory for Robotic Generalist Policies | https://arxiv.org/html/2603.04639v1 | 已读摘要、§3–4 的任务与记忆方法、§6、附录 B.5 和 C.2。Permanence 任务包括遮挡和换位；同一 backbone 比较记忆形式，另用正确及带坐标的 oracle 子目标估计高层信息提供后的表现。 | 已覆盖相同当前观测需要不同历史动作的机器人评估对象和 oracle 记忆相关对照；正文未报告按 1/3、0.7、0.95 训练频率操纵参数位置先验。
- MemER: Scaling Up Memory for Robot Control via Experience Retrieval | https://arxiv.org/html/2510.20328 | 已读摘要、§3 的历史关键帧方法、§4.1 Q3 及 §5。Q3 解释模型会依赖专家示范中的规范子任务顺序，错误文本历史还会压低视觉历史的使用。 | 已出现训练顺序先验与历史证据使用的机器人因果解释；这是顺序／模态实验，未隔离位置频率与准确情节记忆的交互。
- Semantic Influences on Episodic Memory Distortions | https://bpb-us-w2.wpmucdn.com/web.sas.upenn.edu/dist/2/204/files/2021/02/Tompary-and-Thompson-Schill-2021-Semantic-influences-on-episodic-memory-distortions.pdf | 已读摘要、Experiment 1 的 Method 和 Experiment 2／General Discussion。研究使同类别图像通常出现在相近位置，同时将部分图像随机移位，用打乱类别与位置对应的控制组区分空间记忆误差和朝类别中心的偏移。 | 人类记忆研究已实验分离位置经验与特定情节信息的影响；它研究内生记忆重建，没有向策略提供已知准确的外部位置记录。

Strongest Counterexample: [Large Language Models with Controllable Working Memory](https://arxiv.org/html/2211.05110) — §3 和 §4 已研究参数知识对冲突上下文响应的限制，并用 KAFT 的反事实上下文监督改善 controllability。候选的具体差别是固定骨干与训练预算、随机改变位置频率，并以正确／错误记忆的行动成功率差之差作为估计量；该文没有这个机器人交互实验。

Overlap: high — 参数知识影响外部证据纠正及反事实训练增强证据响应已被明确研究，候选的受控位置频率与准确情节记忆交互尚未在已读正文中出现。

Occupation Check: Longpre 等 §3.3 已定义 memorization ratio，Li 等 §3.7 已定义 controllability，Kortukov 等 §3.2 与 §4.4 已定义并干预知识更新／parametric bias；它们不是候选成功率差之差的相同估计量。候选未指名目标论文，本次读取机器人近邻的讨论与消融：MemER §4.1 Q3 已提出规范顺序先验及文本压制视觉的解释；RoboMME §6 讨论固定资产和单骨干的局限，没有给出所述位置先验因果实验。

Opposing / Conditional Evidence: Kortukov 等 §5–6 发现真实正确文档中的知识更新通常成功，失败显著依赖错误参数答案是否在上下文出现；其 §4.4.1 Table 5 显示遮蔽旧答案可以减少保留旧答案，但不保证正确更新同步改善。RoboMME 附录 C.2 的 oracle grounded subgoal 对空间任务有明显帮助，因此外部准确空间信息可被策略有效利用；这既不是位置先验干预，也不能排除候选的条件性交互。人类空间记忆论文的 Experiment 2 还显示偏置随记忆置信度变化，不能直接等同于给定准确外部记忆后的行动偏置。

Payoff / Implementation Check: 修复部分的最近占用者是 KAFT 和 DisentQA 的反事实上下文监督。[Apple 的官方 substitution framework](https://github.com/apple-aiml-research/ml-knowledge-conflicts)已实现固定问题、替换上下文答案及相应标签的数据生成。检索覆盖 VLA 位置偏置、RoboMME、MemER、上下文冲突微调和人类位置先验，未确认同一三位置取物设置下普通记忆微调、位置均衡微调及成对历史微调的已发表统一比较。最接近的机器人比较平台为 RoboMME 的 MME-VLA／oracle 子目标对照；上述三种修复在候选指标下尚无已验证最强者。候选的 10 点交互和 5 点修复收益均为预期阈值，不能转述为既有实验结果。

Papers Read: 7
arXiv ID Check: yes
