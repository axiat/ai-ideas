# I4
Verdict: reject
CRITICAL: 0
MAJOR: 1
Headline: 受控位置频率实验尚未被所供文献完全覆盖，但现有论证不足以支持其独立研究价值，修复也未形成可归因的新贡献（candidate.json；prior_work.md）。
Occupation: prior_work.md 所述 Controllable Working Memory §3–4 已覆盖参数知识限制外部证据响应及反事实上下文监督，DisentQA §2.1–2.3 已覆盖配对上下文与对应标签训练；MemER §4.1 Q3 已提出机器人训练顺序先验影响历史使用的解释。未覆盖的具体差别是 candidate.json 中固定骨干与预算、操纵位置频率，并测量记忆正确性对行动成功率的交互；不能据此判定整个候选已被占用。
Experiment: candidate.json 提供三档训练位置频率、逐项配对的 300 个检查点、正确／错误／空白记忆及三个种子，能够实施有界的行为交互检验；三种微调共享额外样本与更新预算。10 点交互和 5 点修复收益均为预测，所供 prior_work.md 也明确说明它们不是实验结果；实验尚未运行本身不构成缺陷。
Estimand: candidate.json 的正确减错误记忆成功率差之差，对齐训练频率干预与记忆正确性的总行为交互。其已列出的空白条件与首次动作响应可帮助区分正确记忆带来的收益、错误记忆造成的损害及动作选择变化；该总效应本身不能单独识别内部参数机制。现有材料不足以认定此行为估计量无效。
Payoff: MAJOR 1 是独立增量的价值支持不足。candidate.json 的剩余测量贡献是位置频率下的交互曲线，但 prior_work.md 已分别提供参数知识影响证据响应和机器人训练先验影响历史使用的研究；候选未说明该曲线将区分什么重要的新解释或纠正什么具体评价结论。修复仍采用 prior_work.md 中 KAFT／DisentQA 已有的反事实监督原则；即使超过普通或位置均衡微调 5 点，也不能据此归因出超越该已知原则的新贡献。这里缺少支持，尚无证据证明实验不会出现所预测的效果。
Feasibility: candidate.json 将任务限定为三位置取物，列出每策略 12,000 条轨迹、额外 3,000 条微调样本、四张 A100 和 120 GPU 小时。材料没有证明存在不可修复的资源障碍；具体骨干及单次训练耗时未给出，因此该预算仍是待验证的执行假设，不另计资源性 MAJOR。
History: unavailable
Assessment: {"coverage":"not-covered","coverage_evidence":[{"source":"candidate","quote":"该候选独立操纵参数中的位置先验与外部位置记忆，检验二者对取物行动的交互作用。"},{"source":"prior-work","quote":"候选的具体差别是固定骨干与训练预算、随机改变位置频率，并以正确／错误记忆的行动成功率差之差作为估计量；该文没有这个机器人交互实验。"}],"causes":[{"code":"value-insufficient","evidence":[{"source":"candidate","quote":"将训练位置偏置与测试记忆正确性拆开，估计外部记忆纠正收益随参数先验强度变化的曲线。"},{"source":"candidate","quote":"修复使用成对反事实记忆训练，让相同当前观测下的动作随有效历史位置改变。"},{"source":"prior-work","quote":"已覆盖“参数知识使外部记忆纠正不充分”以及反事实上下文监督这两个部分；尚未测量机器人位置先验强度与准确历史记忆的交互曲线。"},{"source":"prior-work","quote":"已出现训练顺序先验与历史证据使用的机器人因果解释；这是顺序／模态实验，未隔离位置频率与准确情节记忆的交互。"}]}]}
Reason: 依据 review_contract.md，普通测量探针至多达到 borderline，仍需独立价值支持；本候选保留了未覆盖的受控交互实验，但 candidate.json 与 prior_work.md 尚未支持其超出已有现象局部量化的知识收益，修复也未提出超越最近方法的可归因差异，因此唯一 MAJOR 是净新增价值不足，当前既不满足 strong-accept，也不足以支持 accept-w-rev，判为 reject。