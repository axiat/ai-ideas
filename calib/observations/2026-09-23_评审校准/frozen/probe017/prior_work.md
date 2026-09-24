## I5

Search Terms: 直接命中／问题措辞：`LLM verifier correlated errors shared context memory policy verifier robot`、`"verifier" "correlated errors" language models`；机制及修复：`"Chain-of-Verification" bias verification independently`、`"shared evidence" "correlated" verifier`、`"robot" "verification" "memory" failure detection`；相邻领域及基线：`"common mode" "failure" "independent" software Knight Leveson`、`"DoReMi" "Detecting" robot arxiv`、`"LLMs Cannot Self-Correct Reasoning Yet" arxiv`；工程实现：`"verification" "independent" "chain-of-verification" github lastmile`。
- Query: https://api.semanticscholar.org/graph/v1/paper/search?query=verifier%20correlated%20errors&limit=8&fields=title,abstract,url
- Query: https://api.semanticscholar.org/graph/v1/paper/search?query=verifier%20correlated%20errors&limit=3&fields=title,abstract,url
- Query: https://export.arxiv.org/api/query?search_query=all%3Averification%20AND%20all%3Alanguage&start=0&max_results=5

API Outcome: Semantic Scholar 网页抓取失败，直接 GET 重试返回 HTTP 429。arXiv 网页抓取失败，直接 GET 返回 200 和五条摘要；该宽查询大部分结果属于语音或形式验证，近邻主要通过问题与机制检索找到。正文阅读不以 API 元数据替代。

Nearest Work:

- Chain-of-Verification Reduces Hallucination in Large Language Models | https://arxiv.org/html/2309.11495v2 | 已读摘要及 §3.1–3.3。§3.3 的 Joint、2-Step 和 Factored 变体比较验证是否可见原始回答；作者明确指出共同上下文可能使验证重复原有幻觉，独立回答验证问题可减少这种干扰。 | 已覆盖共享错误内容影响验证及通过独立重建答案修复的机制；它没有随机操纵机器人策略／验证器的历史记忆，也没有固定动作后的误接受率指标。
- Correlated Errors in Large Language Models | https://arxiv.org/html/2506.07962 | 已读摘要、§3.1 的条件错误一致率及回归方法、§4 的 judge 实验。§4 以 judge 自己的答案代理真值，指出两个模型同意同一个错误答案时，错误会被记作正确，从而抬高被评模型的准确率。 | 已覆盖相关错误导致错误通过判定的机制和评估问题；该实验不是将同源／独立错误记忆随机注入固定失败动作的显式 verifier。
- DoReMi: Grounding Language Model by Detecting and Recovering from Plan-Execution Misalignment | https://arxiv.org/html/2307.00329 | 已读摘要及 §III-A–C、Algorithm 1。同一高层 LLM 根据历史生成下一技能及约束，VLM 从实时图像判断约束是否满足，并在违反时触发重规划。 | 已覆盖具身规划与验证、历史相关约束和独立视觉反馈；它在技能执行中检查约束，未隔离共享错误记忆造成的执行前误接受。
- AGM: Achievement-Grounded Memory for Closed-Loop Agents with Frozen VLA Policies | https://arxiv.org/html/2608.29537v1 | 已读摘要、Method、附录 E。方法用事件相对的前后多视角图像及点跟踪验证物理结果，再更新进度。附录 E 明确承认误接受会写入并未发生的进度，误拒绝会浪费重复执行预算。 | 以传感器证据约束记忆写入已有具身实现，是修复部分的近邻；它验证已执行结果，未报告策略与验证器共享错误记忆的交互实验。
- MemER: Scaling Up Memory for Robot Control via Experience Retrieval | https://arxiv.org/html/2510.20328 | 已读摘要、§3.1–3.3、§4.1 Q3 和 §5。高层 VLM 从相机历史选择关键帧并生成子任务；Q3 明确观察到错误文本记忆引起过度依赖，使模型忽略视觉记忆。 | 历史证据检索及避免沿用错误摘要已有直接机器人近邻；MemER 的对象是动作提议和记忆模态，没有独立动作验证器。
- Large Language Models Cannot Self-Correct Reasoning Yet | https://arxiv.org/html/2310.01798 | 已读摘要、§2、§3.1–3.2。方法区分纯内部反馈与 oracle 正误反馈，以生成、批评、重答流程及同预算比较评估自纠错。 | 已覆盖缺乏独立证据时验证／纠错失败的实验现象；未将共享记忆设为随机处理变量，不能由该文推导候选预期的同源错误交互大小。
- An Experimental Evaluation of the Assumption of Independence in Multi-Version Programming | https://people.cs.rutgers.edu/~uli/cs673/papers/EvaluationMultiVersionProgramming86.pdf | 已读摘要、§1–2 的实验设计。研究对根据同一规范独立编写的 27 个程序施加共同测试输入，发现共同失败明显多于独立性假设的预期；§1 讨论了以输出一致替代独立正确性验证会漏掉共同错误。 | 软件可靠性领域已命名并实验研究 dependent errors／common-mode failures；它检验程序版本的错误依赖，没有检验情节记忆来源。

Strongest Counterexample: [Chain-of-Verification](https://arxiv.org/html/2309.11495v2#S3.SS3) — §3.3 已明确给出共享原始内容使验证重复相同幻觉的因果解释，并实现屏蔽原始回答的独立验证。候选将共享内容具体化为错误情节记忆，并以固定失败动作、匹配独立错误和成功动作接受率来测量额外交互；CoVe 没有这一实验。

Overlap: high — 共享错误内容使验证重复错误及独立验证的修复机制已在 CoVe §3.3 出现，机器人固定动作上的同源记忆交互量仍未在已读正文中找到。

Occupation Check: “相关错误导致错误通过复核”已有 Correlated Errors §4 的明确表述和软件可靠性前例；这些来源不能单独确定记忆共享的因果效应。CoVe 的成功也提供了条件差异：同一个模型在分离上下文后仍可减少幻觉，因此“使用同一模型”本身不能替代候选的共享错误记忆变量。

Payoff / Implementation Check: 独立事实重建的最近机制来源是 CoVe；机器人传感器证据验证的最近来源是 AGM；历史关键帧检索的最近来源是 MemER。[LastMile AI 的 CoVe 模板](https://github.com/lastmile-ai/aiconfig/tree/main/cookbooks/Chain-of-Verification)已提供可用的多阶段验证 notebook 和配置。已检索独立上下文、视觉历史重建、机器人约束监测和共同失效，尚未确认在相同 512-token、相同历史片段、匹配成功动作接受率条件下比较“时间／传感器来源约束重建”的发表结果；上述实现也没有该条件下的统一排名。候选的等预算历史重建验证器尚无具体公开实现名称，不能将任何近邻称为该指标下已确认的最强基线。10 点误接受率增加是候选预期，不是来源结果。

Papers Read: 7
arXiv ID Check: yes
