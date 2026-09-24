# PUTNet 校准证据包

检索与阅读日期：2026-09-22 至 2026-09-23。用途：回溯式匿名 idea review 的资料准备。本包不提供正式接收票，也不以发表 venue 代替机制和实验判断。

## 1. 身份、时间与材料可得性

目标论文为 **Phase Consistency Prior Driven RGB-D Salient Object Detection**，作者为 Jingyi Xu、Xin Deng、Minglang Qiao、Lai Jiang、Mai Xu，DOI 为 `10.1109/TPAMI.2026.3729831`，IEEE document ID 为 `11673835`。

| 时间字段 | 已核实事实 | 证据与限制 |
|---|---|---|
| 论文电子出版 | 2026-09-01，ahead of print | [PubMed](https://pubmed.ncbi.nlm.nih.gov/42678862/)；已下载的 PubMed XML 中 `ArticleDate DateType="Electronic"` 同样为此日。 |
| DOI 建档 | 2026-09-01 19:06:11 UTC | [Crossref 原始元数据](https://api.crossref.org/works/10.1109/TPAMI.2026.3729831)。`published` 只有年份 2026，没有投稿或接收字段。 |
| 现有仓库创建记录 | 2025-05-29 12:37:19 UTC | [官方 GitHub API](https://api.github.com/repos/JingyiXu404/PUTNet)。它证明当前仓库的创建时间；**不能单凭该字段证明当日已设为 public**。 |
| 最早可检查的提交 | `523ca0b9`，2025-05-29 12:38:38 UTC；`e950adef`，同日 12:41:37 UTC | [提交历史 API](https://api.github.com/repos/JingyiXu404/PUTNet/commits?per_page=100)。初始 README 已含完整论文题目。初版核心更新函数有 `release after accept` 占位。提交时间也不能独立证明当日公开可见。 |
| 后续代码更新 | `5bbd2319`，2026-07-04 09:09:05 UTC | [固定提交代码](https://github.com/JingyiXu404/PUTNet/tree/5bbd2319)。该版补充了可检查的更新计算。 |
| 投稿、修回、接收日期 | **未知** | 当前取得的 PubMed、Crossref、README 均未给出。不能把仓库创建或 README 文案解释为正式投稿或接收日期。 |

**回溯时点采用 2025-05-28 日末 UTC，属于保守的“最早现存作者产物前”截止日。** 最早公开可见日期尚未完全证实，因此不能把 2025-05-29 写成已证实的首次公开日。若实验要求严格按首次公开日复现，这一日期不确定性应保留。采用更早截止日，可以避免把 2025 年下半年和 2026 年工作泄漏到先验检索中。

目标全文检索覆盖：完整题目与 DOI 搜索、官方仓库文件树、IEEE landing page 与 stamp 链接、Crossref 所列 PDF 地址、OpenAlex 开放版本索引。**未取得合法公开全文或作者稿。** [OpenAlex](https://api.openalex.org/works/https://doi.org/10.1109/TPAMI.2026.3729831) 返回 `is_oa=false`、`oa_url=null`、`any_repository_has_fulltext=false`；此结果只能描述索引状态，不能证明作者稿绝不存在。OpenAlex 的 `2026-01-01` 是仅年份记录的标准化日期，不能覆盖 PubMed 的明确电子出版日。

## 2. 目标机制：摘要证据与代码证据分开

**论文与主实验的阅读等级为 abstract-limited；另有官方代码的静态阅读。** 摘要报告 RGB/depth 输入与 saliency 输出的相位成分差异可近似为 Laplace 分布。作者据此建立 RGB–Depth–Saliency 相位一致性模型，先更新 saliency phase，再通过 global phase-to-image transform 得到预测图。摘要称九个 RGB-D SOD 数据集上的定量和定性结果优于比较方法。[论文摘要](https://pubmed.ncbi.nlm.nih.gov/42678862/)

2026-07-04 代码提供以下可独立检查的实现信息，不能替代论文推导或实验验证：

- `utils.py::amp_pha` 提取 FFT 幅度与相位；`IFFT_xp` 对单位幅度、原相位的复谱作 IFFT，并取实部。随后更新流程接收的是这种相位重建表示。因此，现有证据不足以将论文中的 phase gap 直接解释为原始角度数组的逐点差。
- `Stage_p` 按可学习权重组合两种输入表示与辅助变量；`Update_av` 使用 soft-thresholding 和对偶变量式更新，`Update_m` 使用学习到的网络。该实现与稀疏残差正则和优化展开相容，但原始目标函数、分布参数估计及推导对应关系尚未从全文核验。
- 更新后的 `Pz` 与 RGB、depth 一起输入 `CPNet`。该模块含三条 SwinTransformer 分支、可学习多模态权重、CoordAtt 和大核卷积。因此，摘要中的 global phase-to-image transform 不能简化成“直接 IFFT 就输出最终 mask”。

来源：[PUTNet.py 固定版本](https://github.com/JingyiXu404/PUTNet/blob/5bbd2319/PUTNet.py)、[utils.py 固定版本](https://github.com/JingyiXu404/PUTNet/blob/5bbd2319/utils.py)。主要核查位置为 `HeadNet`、`Update_av`、`CPNet`、`Stage_p`、`PUTNet.forward`。未运行训练或推理，未验证代码与出版版本完全一致。

## 3. 截止日前的七个相关邻作

“读方法”在下表中严格区分论文正文与代码阅读。四篇已读论文的摘要及相关方法正文，两篇为出版社摘要加官方核心代码，一篇为摘要及出版社 highlights；**没有把七篇统一计为全文精读**。

| 邻作及日期 | 实际阅读覆盖 | 与目标相交的机制 | 可以成立的差异判断 |
|---|---|---|---|
| **LGCNet: Laplacian Gradient Consistency Prior for Flash Guided Non-Flash Image Denoising**；TIP 2024，电子出版 2024-11-07；核心代码提交 2024-05-28 | 出版社完整摘要、官方 `MDN.py` 的更新与展开结构；未获得论文全文 | 同一作者团队已提出跨模态差异在变换域近似 Laplace 分布，并将一致性模型的求解展开为网络。代码含差分算子、soft-thresholding、辅助及对偶变量更新。 | “统计拟合 → Laplace 一致性先验 → 可解释展开网络”已有直接前序。目标的可能增量在相位重建表示、RGB–Depth–Saliency 三方关系与显著分割任务；不能只因把 gradient 换成 phase 就认定两者完全等价。 |
| **DFENet: Deep Fourier-embedded Network for RGB and Thermal Salient Object Detection**；arXiv v1 2024-11-27，**使用 v2 2025-02-10** | v2 摘要、§III-A–E 方法、部分实验与消融表；非全篇逐页精读 | MPA 在 Fourier 域处理模态特征的幅度及相位；FEB 强化相位/轮廓表示；CFL 利用双模态的相位信息加权预测与标注之间的频谱误差。 | 学习相位、频域跨模态融合及输入/输出频谱关联已存在。v2 未显示基于 RGB–Depth–Saliency Laplace 残差的显式相位更新模型。v2 主要实验为四个 RGB-T 数据集，不能把后来的 RGB-D 结果倒用于截止日前。 |
| **Frequency-aware feature aggregation network with dual-task consistency for RGB-T salient object detection / FFANet**；PR 146，2024-02；Crossref 建档 2023-10-18 | 出版社摘要及 highlights；**method-unread** | FMFA 提取和聚合高低频互补线索；HF-SDM 使用高频线索预测 signed distance map，配合区域/轮廓双任务一致性。 | 频率先验与一致性约束的结合已存在；可读材料描述的是高低频分解和任务一致性，尚不能确认其完整公式与目标模型的关系。 |
| **WaveNet: Wavelet Network With Knowledge Distillation for RGB-T Salient Object Detection**；TIP 2023，电子出版 2023-05-16 | 出版社摘要、官方 `Wavenet.py` 相关模块及训练损失；代码版本提交 2023-08-23；论文方法正文未读 | WaveMLP 提取特征、SwinNet 蒸馏、跨模态 KL 约束与双树复小波融合；代码用共享低频项和各模态高频项重建，再作融合。 | 可训练频域融合与跨模态一致性已经结合。该工作约束模态特征并进行小波重建，已读材料未建立输入—saliency 相位残差的 Laplace 统计模型。 |
| **基于空－频域混合分析的 RGB-D 数据视觉显著性检测方法**；岳娟等，《机器人》39(5)，2017-09；收稿/录用/修回为 2017-03-26 / 06-16 / 06-22 | 原刊 PDF 摘要、§3.1–3.4 方法及部分实验说明 | 将 depth、强度、颜色对立通道组成四元数，保留相位、平滑幅度后逆变换生成多尺度显著图；再用超像素与元胞自动机融合。 | RGB-D 显著检测的频域融合、相位保留及频谱到显著图重建均早已出现。目标若有增量，应落在学习和更新输出相位的统计模型，不能主张首次在 RGB-D SOD 使用频域或相位。 |
| **Spatio-temporal Saliency Detection Using Phase Spectrum of Quaternion Fourier Transform / PFT–PQFT**；Guo、Ma、Zhang，CVPR 2008 | 原论文摘要、§2.1–2.3、§3.1–3.2，以及实验评价定义 | 将幅度设为常量，仅用原相位逆变换，并对能量图作 Gaussian 平滑；四元数版加入颜色、强度和运动。 | “相位对显著位置有用”和“phase-to-saliency 重建”均为明确先例。该方法没有学习输入与真值之间的 Laplace 相位一致性，也不是 RGB-D 监督分割网络。 |
| **Visual Saliency Based on Scale-Space Analysis in the Frequency Domain / HFT**；TPAMI 35(4)，2013；DOI 2012；作者稿 arXiv 上传 2016-05-06 | 作者稿摘要、§3.4、§4.1–4.4 方法；未读全部实验 | 通过 Gaussian 核平滑幅度谱构造频谱尺度空间，保留原相位与 eigenaxis，逆变换产生候选显著图，再依据熵选择尺度。 | Fourier 全局重建和相位保留已有系统方法。目标的候选增量是学习输出相关相位与跨模态残差约束；不能将“保留相位并重建”本身作为新发现。 |

逐项原始来源：

1. LGCNet：[IEEE 摘要与日期](https://ieeexplore.ieee.org/document/10746360/)、[官方代码](https://github.com/JingyiXu404/LGCNet/blob/901fb450/MDN.py)、[核心文件提交日期](https://api.github.com/repos/JingyiXu404/LGCNet/commits?path=MDN.py&per_page=100)。
2. DFENet：[版本历史](https://arxiv.org/abs/2411.18409)、[截止日前 v2 全文](https://arxiv.org/html/2411.18409v2)。v2 模型名为 DFENet；最终 v3 改为 FreqSal，2025-11-04 的十数据集扩展属于截止后信息。
3. FFANet：[出版社摘要](https://www.sciencedirect.com/science/article/pii/S0031320323007409)、[Crossref 日期](https://api.crossref.org/works/10.1016/j.patcog.2023.110043)。Crossref 建档时间不作为精确首次公开时间。
4. WaveNet：[IEEE 摘要与日期](https://ieeexplore.ieee.org/document/10127616/)、[官方核心代码](https://github.com/nowander/WaveNet/blob/af3c9423/networks/Wavenet.py)、[提交日期](https://api.github.com/repos/nowander/WaveNet/commits?path=networks/Wavenet.py&per_page=100)。
5. RGB-D 空－频混合：[原刊页面](https://robot.sia.cn/cn/article/doi/10.13973/j.cnki.robot.2017.0652)、[原刊 PDF](https://robot.sia.cn/cn/article/pdf/preview/10.13973/j.cnki.robot.2017.0652.pdf)，方法式 (6)–(18)。
6. PFT–PQFT：[CVPR 2008 论文 PDF](https://iacl.ece.jhu.edu/proceedings/cvpr2008/papers/375.pdf)，PFT 式 (1)–(3)、PQFT 式 (17)–(22)。
7. HFT：[作者稿及期刊信息](https://arxiv.org/abs/1605.01999)、[作者稿 PDF](https://arxiv.org/pdf/1605.01999)，尺度空间式 (19)–(20)、重建式 (29)、Algorithm 2。

这七篇覆盖直接统计先验、可训练双模态频域网络、RGB-D 频域显著检测和 phase-only 重建四条相关路线。检索不是穷尽检索，未发现完全同构先例不构成“首次”的证明。

## 4. 明确排除的截止后信息

| 检索中出现的材料 | 日期 | 排除原因 |
|---|---|---|
| FreqSal / DFENet v3 | 2025-11-04 | v2 可纳入，v3 新增 RGB-D 等实验和修改内容排除。 |
| Mamba-DFAN | SSRN 2025-07-02 | 晚于保守截止日；只读取了摘要，未纳入机制判定。 |
| SFCINet：Lightweight Spatial-Frequency Collaborative Interaction Network for RGB-D SOD | 2026-06 | 学习 amplitude/phase、用频域先验调制空间分支，但在截止后。 |
| PSRNet：phase-guided frequency-domain structure reconstruction for RGB-T SOD | 2026 年公开，检索页面显示 2026 年 7 月附近 | 含 phase-consistency 和结构重建，与目标措辞接近，但不能作为 2025 年已有工作。精确日期未进一步核验。 |
| S³AM：Reliability-Calibrated Frequency Adapter | arXiv 2026-08-18 | 截止后；不纳入回溯评估。 |

日期来源：[FreqSal 历史](https://arxiv.org/abs/2411.18409)、[Mamba-DFAN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5335445)、[SFCINet 出版社](https://www.mdpi.com/1424-8220/26/12/3708)、[PSRNet 出版社](https://www.frontiersin.org/journals/neurorobotics/articles/10.3389/fnbot.2026.1863193/full)、[S³AM](https://arxiv.org/abs/2608.17475)。本表中的工作只用于时间防泄漏说明。

## 5. 已知实验与尚未知的信息

| 问题 | 当前证据能支持的内容 |
|---|---|
| 目标是否报告实测结果 | 作者摘要报告九个 RGB-D SOD 数据集上的定量及定性优势。只能写成“作者报告”，不能写成已复核的比较结论。 |
| 数据集和主表 | 九个数据集的完整名单、划分、各指标、绝对与相对提升、比较方法、训练数据及测试设置均未从论文核实。 |
| Laplace 观察 | 摘要报告近似 Laplace；尚不清楚拟合对象是原始 Fourier 角度还是 phase-only 重建图，也不清楚样本、跨数据集稳定性及与 Gaussian/其他分布的比较。代码使后者成为有根据的可能解释，仍不能替论文定论。 |
| 机制贡献 | 不清楚移除统计先验、替换相位表示、替换分布、取消 phase 更新、只保留第三分支以及容量匹配控制的结果。**这是证据缺失，不是认定论文没有做消融。** |
| 成本与鲁棒性 | 当前代码可见三条 Swin 分支；FLOPs、参数量、速度、深度噪声/错配结果及训练成本未知。不能仅凭结构断言效率差或增益来自容量。 |
| 可复现性 | 官方推理代码存在，当前 README 仍写模型在接收后发布；本次未取得并验证权重，也未运行复现。README 可能未及时更新，不能据此认定作者未发布权重。 |

## 6. 可直接用于匿名 review 的机制描述

候选方法研究 RGB-D 显著分割中的输入—输出关系，假设 RGB/depth 与 saliency 的相位相关表示之间存在可由 Laplace 分布近似的残差，并据此构造可学习更新过程，再由多模态网络恢复显著图。现有代码支持 soft-threshold 更新与三路特征融合，但原文统计验证、优化推导和模块贡献尚未核实。

截至保守时点，相位重建显著图、RGB-D Fourier 融合、学习幅相特征与跨模态 Laplace 一致性展开都已有前序。现有证据下值得单独核验的贡献是：该统计关系是否对 RGB–Depth–Saliency 三方有效，以及将其用于输出相位更新是否产生超过现有相位/频域模块的可归因收益。九数据集优势目前只有摘要陈述，材料尚不足以判断提升幅度和原因。

## 7. 本地审计材料

同目录保存了 `put-repo.json`、`put-commits.json`、`put-earliest.json`、`put-crossref.json`、`put-pubmed.xml`、`put-openalex.json`，以及目标的 2025/2026 代码快照。已下载的邻作 PDF 及提取文本为 `pqft2008`、`rgbd2017`、`dfe2025v2`、`hft2013`；LGCNet 与 WaveNet 保存了官方核心代码和提交日期元数据。上述文件只用于证据审计，外部 review 可使用前文原始来源链接。
