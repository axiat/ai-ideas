# Brainstorming & Acceptance Policy（每周 idea routine 必读，优先级高于 rubric.md 的默认尺度）

## 发散要求

- `research_context.md` 只是可选的灵感来源，**不是约束**。每轮生成的 idea 中至多 1-2 个与用户现有工作（DSRL / π0.5 栈）直接相关，其余必须跳出该栈，在 WorldModel / VLA 乃至具身智能更大范围内自由发散。
- 优先级排序：纯 novelty（改变问题定义或基础机制，Transformer 级别的野心）＞ 问题发现 + 初步探究 ＞ 现有方向的增量改进。

## 允许的 idea 形态（可以非常初步）

idea 不要求完整方法与实验设计，以下四种形态均合法：

1. **纯新机制 / 新问题**：只要问题成立、确认无人做过、存在一条可探索路径即可。
2. **问题发现 + 初步数学探究**：发现瓶颈或反常现象后，给出初步的数学 formulation、复杂度分析或 back-of-envelope 推演。
3. **经典计算机科学原理迁移**：把体系结构 / 计算机组成的经典思想套到 WM/VLA 的训练或推理上——局部性原理（缓存、分块）、log 域计算（logarithmic number system、Mitchell 近似等）、流水线、投机执行、稀疏性、内存层级、预取等——用于效率或架构创新。
4. **瓶颈定位实验**：设计小规模 probe 实验，验证某个瓶颈到底出现在哪，为后续工作铺路。

## 评审校准（覆盖 rubric.md 的 verdict 尺度）

以 A 类会议（NeurIPS / ICLR / CoRL）及机器人强会（RSS / ICRA）的 reviewer 尺度校准：

- **Strong Accept** ⇔ 做出来有较大概率达 **clear accept**（约 6,6,8）及以上；能冲 oral/spotlight（8,8,6+，无强反对）更佳。
- **Accept with Revisions** ⇔ borderline accept：上限低，价值有限。
- 评分权重：**novelty 与"是否已有人做过"是第一位的**；feasibility 与工程完整度可放宽（idea 允许非常初步），但 novelty 存疑必须定向查文献，不得凭印象放行。
- 任何候选 Strong Accept 在定级前必须做一轮定向查重（web search / arXiv 检索最相近的 3-5 篇），发现强重合即降级。

## 保留规则

- 报告正文只保留 **Strong Accept**；重试循环的达标条件是至少 1 个 Strong Accept。
- borderline（Accept with Revisions）至多附 2 个作参考，标注"仅供一览"，不计入达标。
