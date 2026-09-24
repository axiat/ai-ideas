# I1
Verdict: reject
CRITICAL: 0
MAJOR: 2
Headline: 中断前缀记忆的具体组合尚未被 supplied record 完全覆盖，但其独立研究价值缺少支持，机制归因也缺少必要对照（candidate.json；prior_work.md）。
Occupation: prior_work.md 中，CRAM §IV-B.2 已用执行中的 active NEEM 支持诊断与恢复，D-POMDP §2、§3.1 已允许结果观测到达前决策并随后更新信念，EMV §IV 已将 interrupted 状态纳入经历流。这些来源占用了宽泛的假设移除、恢复用途及延迟信念更新机制；候选要求的中断前缀、未决分布与学习控制收益的组合仍有未覆盖部分，不能据此判定整个贡献已被占用。
Experiment: candidate.json 的三项模拟任务、2400组成对种子、中断率与回执延迟交叉条件构成可执行的性能检验。配对95%置信区间上限低于5个百分点能够否定预设增益命题；它不能否定所有较小收益。无中断条件、完整历史循环策略及单独报告的贝叶斯上界均提供有用参照。
Estimand: MAJOR 2：candidate.json 同时提出成功率提升和“收益来自部分执行语义的保留”。匹配输入、历史长度及决策时刻有助于性能比较，但等待、单结果预测与未决表示同时改变了不确定性的处理方式。完整历史循环策略没有明确实现 prior_work.md 中 D-POMDP 的延迟信念机制，已知转移模型的贝叶斯上界也不能替代同信息条件下的可训练对照。因此，当前比较尚不能区分通用延迟信念更新的收益与中断经验表示的新增作用；需要保留不确定性处理能力的对照或相应表示消融。
Payoff: MAJOR 1：prior_work.md 已支持未完成经验的在线使用及延迟结果下的信念更新；candidate.json 尚未给出具体分析、区分性实例或观察，说明其剩余表示设计相对这些机制提供何种独立知识或方法收益。预期提高5个百分点是待测阈值，不能作为收益依据。当前支持不足以达到 SA，也尚未达到 AwR 所需的独立研究价值。
Feasibility: candidate.json 将工作限定为6000条训练轨迹、三项模拟任务、2张A100共48小时及2–3人。该方案给出了明确资源约束； supplied artifacts 没有显示独立的资源失效条件，因此不另计可行性缺陷，也不因实验尚未运行扣分。
History: unavailable；未提供可核验的 history_summary.json。
Assessment: {"coverage":"not-covered","coverage_evidence":[{"source":"candidate","quote":"记忆条目保留已执行前缀、未决后果和候选状态分布；新回执到达后再收缩分布。"},{"source":"prior-work","quote":"候选另外要求保存中断技能前缀和未决结果分布、随迟到回执修订，并在匹配输入和决策时刻的学习控制实验中测量收益，CRAM 的该段没有提供这组实验。"},{"source":"prior-work","quote":"实验为 Tiger、maze 等离散基准，并未训练机器人情景记忆写入器或研究中断动作块前缀。"}],"causes":[{"code":"value-insufficient","evidence":[{"source":"candidate","quote":"待核验的问题是未决执行记录能否直接承担经验记忆；与部分可观测控制、执行监测和事务式技能记录的区别仍需研究。"},{"source":"candidate","quote":"预期未决条目较最强可训练对照提高至少5个百分点"},{"source":"prior-work","quote":"完整技能结果先于一切经验使用并非上述文献的共同要求；CRAM 和 EMV 均记录执行过程，D-POMDP 明确放宽立即得到结果观测的假设。"}]},{"code":"design-invalid","evidence":[{"source":"candidate","quote":"待检验命题是，未决经验能够支持立即决策，且收益来自部分执行语义的保留。"},{"source":"candidate","quote":"比较等待定案、预测单一结果、未决条目，以及读取完整历史的循环策略；以已知转移模型的贝叶斯过滤器作为上界。"},{"source":"prior-work","quote":"§2 定义观测延迟随机变量，明确允许未收到动作结果观测时执行后续动作；§3 将问题近似转换为扩展 POMDP，§3.1 在延迟观测到达时更新信念与策略。"}]}]}
Reason: Reject。prior_work.md 的高重合使候选不满足假设移除的专门条件，须按普通候选评估。两个独立 MAJOR 分别是剩余贡献价值缺少支持，以及机制归因未排除已有延迟信念处理这一实质替代解释；补齐后一项对照不会自动解决前一项价值问题。candidate.json 的有界实验值得检验，但其可执行性和预测增益不足以支持 SA 或 AwR；材料也未显示无法在首篇论文范围内修复的 CRITICAL。