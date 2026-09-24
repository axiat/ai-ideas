# TLCNet 可审计证据包

核查日期：2026-09-22 至 2026-09-23。本文仅整理来源、贡献差分与待验证项，不给 SA 评级。

## 1. 论文身份、时间与材料覆盖

- 正式题目：**Rethinking Multi-modal Image Super-resolution: The Key Role of Cross-modal Consistency Prior**。作者：Jingyi Xu、Xin Deng、Yutong Wang、Lai Jiang、Mai Xu。TPAMI，DOI `10.1109/TPAMI.2026.3723450`，IEEE document `11654513`。[PubMed](https://pubmed.ncbi.nlm.nih.gov/42594006/)
- **已核实在线发表日期：2026-08-13。** Europe PMC 的 `electronicPublicationDate`、`firstPublicationDate` 均为该日，PubMed 的 publication status 为 `aheadofprint`。Crossref 创建记录为 `2026-08-13T19:07:02Z`，与其一致。Crossref 仅提供年级别 published 字段；其 license start `2026-01-01` 以及 OpenAlex 的 `2026-01-01` 均不用于推定首发日期。[Europe PMC](https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=EXT_ID:42594006&format=json&resultType=core)；[Crossref](https://api.crossref.org/works/10.1109/TPAMI.2026.3723450)
- **投稿、修回、接收日期：未取得。** 没有读到正式论文首页，不能用在线发表日期替代接收日期。
- **目前取得的是摘要与官方代码，全文分析状态为 `abstract-limited + code-inspected`。** 本轮检索了正式题目、仓库旧题目、TLCNet、DOI、作者组合；核查 PubMed、Europe PMC、Crossref、OpenAlex、Semantic Scholar 与作者 GitHub。未找到可读取的合法公开全文或作者稿。IEEE 页面未返回可读正文；官方仓库无 PDF；Europe PMC 标注 subscription required、无 PMC/作者稿/PDF。该检索结果不证明全文不存在。[IEEE](https://ieeexplore.ieee.org/document/11654513/)；[官方仓库](https://github.com/JingyiXu404/TLCNet)
- 官方仓库仍使用旧标题 **Rethinking Guided Image Super-resolution: Key Roles of Cross-Modality Consistency Priors**。该标题与正式摘要中的 TLCNet 对应，但 README 的接收说明尚未更新，不能作为当前接收状态依据。

### 更早的代码记录与比较截点

[GitHub API](https://api.github.com/repos/JingyiXu404/TLCNet) 显示仓库创建于 `2025-10-12T07:29:23Z`。[首个上传代码提交](https://github.com/JingyiXu404/TLCNet/commit/81b2463fcea00cca9e1f9cc4ad2224a2240540be) 日期为 `2025-10-12T07:30:26Z`，含 `model.py` 与训练脚本。旧 `model.py` 的多个核心更新函数仍为 `release after accept` 占位；可见 Laplacian kernel、Generate_H 等结构，但不能从此版本恢复完整方法。仓库何时转为 public 未核实，因此 **2025-10-12 是目前可追踪的最早代码历史日期，尚不能称为已证实的论文首次公开日期**。

当前固定代码快照为 [fb4825d8991050b8883dade2faeced7e744d0da5](https://github.com/JingyiXu404/TLCNet/tree/fb4825d8991050b8883dade2faeced7e744d0da5)，提交于 2026-06-28。本证据包的七篇比较论文均已于 2024 年或更早公开，因而同时早于代码历史日期和正式在线发表日期。未以 2026 年发表的新作反推 TLCNet 投稿时的新颖性。

## 2. TLCNet 的可确认主张与代码证据

**摘要所述方法。** 作者声称，guidance 与 target 图像的 Laplacian response 差值更适合用 Student-t 分布描述，并据此构造 TMC 一致性先验。TLC 模型同时包含可学习正则项及 Multiplicative Degradation（MD）矩阵，再将迭代求解展开为 TLCNet。作者报告九个数据集、三类 MISR 任务，以及中间特征可视化和 guidance 的 causal analysis。[摘要](https://pubmed.ncbi.nlm.nih.gov/42594006/)

**该摘要未给出**拟合优度、自由度与尺度估计方法、完整优化目标、baseline 数字、效应量和消融表。本轮无法核实这些内容，不能据此断言论文没有相关实验。

**官方 RGBD 实现能够独立确认的结构：**

- `laplace_weight` 使用固定 3×3 八邻域 Laplacian kernel：中心 8，周围 −1；`Update_n` 包含 FFT 域运算。
- `Generate_H` 接收观测 x、guidance y 和当前重建 z，经三个 NetD 分支及注意力融合得到 H；H 进入数据更新。代码中的 H 为输入相关的空间张量，但没有全文时，不能将其直接解释为一般稠密采样矩阵或任意物理退化核。
- `Update_bv` 对两路 Laplacian responses 的差及辅助变量使用可学习 NetD；`Update_z` 是带参数条件的残差 U-Net 型正则模块；各 stage 交替更新多组变量。代码支持有优化结构的多阶段实现，尚不足以验证每个神经更新都严格等价于 Student-t 目标的精确求解。[固定版本代码](https://github.com/JingyiXu404/TLCNet/blob/fb4825d8991050b8883dade2faeced7e744d0da5/RGBD/TLCNet.py)

**测试脚本的覆盖。** 固定版本列有 RGBD 的 NYU、RGBDD、Middle、Lu，RGBT 的 `CIDIS_200`，以及 Pan 的 qb、gf2、wv3、wv2。此处保留代码标识，未将其未经核对地写成论文正式数据集全名。它们与摘要的三类任务及九个数据集数量相容，但不能替代论文实验表。RGBD/RGBT 脚本采用 5 stages，Pan 脚本采用 3 stages。[RGBD](https://github.com/JingyiXu404/TLCNet/blob/fb4825d8991050b8883dade2faeced7e744d0da5/RGBD/test.sh)；[RGBT](https://github.com/JingyiXu404/TLCNet/blob/fb4825d8991050b8883dade2faeced7e744d0da5/RGBT/test.sh)；[Pan](https://github.com/JingyiXu404/TLCNet/blob/fb4825d8991050b8883dade2faeced7e744d0da5/Pan/test.sh)

**复现状态。** 本轮仅静态读取，未训练、未推理、未复现指标。当前仓库树未包含 checkpoints，README 未提供预训练权重下载，test 脚本引用本地 `cpts/`。这属于公开交付材料的可复现性限制；论文中训练、权重及实验是否充分仍需全文核实。

## 3. 固定近邻集合

| 论文与公开时间依据 | 本轮材料覆盖 | 与 TLCNet 的直接关系及已存在部分 |
|---|---|---|
| **LGCNet — Laplacian Gradient Consistency Prior for Flash Guided Non-Flash Image Denoising，TIP 2024**。正式首页：received 2024-05-28，accepted 2024-10-27，published 2024-11-07。[DOI](https://doi.org/10.1109/TIP.2024.3489275) | 已读取给定全文的引言、§III 方法、§IV 主结果及主要消融文字；表格图像未全面逐项复核。 | 同团队最接近的研究路线：先做跨模态残差分布分析，再引入显式一致性项、ADMM 求解、网络展开与中间变量可视化。LGC 使用一阶梯度差的 **Laplace 概率分布 / L1**，任务为 flash/non-flash 去噪；TLC 摘要声称二阶 Laplacian response 差的 **Student-t**，并进入多模态 SR。 |
| **DCTNet — Discrete Cosine Transform Network for Guided Depth Map Super-Resolution，CVPR 2022**。arXiv v1 2021-04-14，CVPR 版本 2022。[日期](https://arxiv.org/abs/2104.06977)；[全文](https://openaccess.thecvf.com/content/CVPR2022/papers/Zhao_Discrete_Cosine_Transform_Network_for_Guided_Depth_Map_Super-Resolution_CVPR_2022_paper.pdf) | 全文已取得，已读 §3，查阅 §4 主表和消融。 | **最直接的目标函数近邻。** Eq.(1) 已对深度与 RGB 的 Laplacian response 构造加权二次一致性项，并以 DCT 求解；网络学习边缘权重与跨模态特征。因此 Laplacian response 一致性本身已有明确先例。 |
| **DeepM²CDL — Deep Multi-Scale Multi-Modal Convolutional Dictionary Learning Network，TPAMI，2023 DOI/2024 卷期**。[PubMed](https://pubmed.ncbi.nlm.nih.gov/37983156/)；[Crossref](https://api.crossref.org/works/10.1109/TPAMI.2023.3334624) | 摘要与官方 README；未取得全文。本轮核实 Crossref created 为 2023-11-20，正式卷期为 2024-05。 | 多层、多尺度、多模态字典学习和迭代展开；字典先验与稀疏特征先验由网络学习，覆盖 restoration/fusion。因此多任务可解释展开与 learned priors 均已有同团队前作。未据摘要推定其具体跨模态约束与 TLC 等价。 |
| **CU-Net — Deep Convolutional Neural Network for Multi-modal Image Restoration and Fusion**。arXiv 2019-10-09，后发表于 TPAMI。[日期与全文](https://arxiv.org/abs/1910.04066) | PDF 已取得；读摘要、§3 MCSC 模型及网络对应部分，未逐表复查所有实验。 | 以 common/unique sparse features 分离跨模态共有信息和各自特有信息，模型派生网络覆盖深度 SR、引导去噪及融合。因此显式抑制 guidance 中不适用信息与跨任务框架已有先例。它没有直接替代 TLC 的特定残差分布命题。 |
| **DKN/FDKN — Deformable Kernel Networks for Guided Depth Map Upsampling**。arXiv 2019-03-27。[日期与全文](https://arxiv.org/abs/1903.11286) | PDF 已取得；读引言、§3 和结构说明，未完整复查实验表。 | 从 RGB/depth 输入预测逐像素稀疏采样位置及权重，形成空间可变的恢复算子。因此“输入自适应、空间非均匀”这一广义能力已有先例；**恢复端采样算子与 HR→LR forward degradation 的建模角色不同**，不能将二者直接判为等价。 |
| **DADA — Guided Depth Super-Resolution by Deep Anisotropic Diffusion，CVPR 2023**。arXiv 2022-11-21。[日期](https://arxiv.org/abs/2211.11592)；[全文](https://openaccess.thecvf.com/content/CVPR2023/papers/Metzger_Guided_Depth_Super-Resolution_by_Deep_Anisotropic_Diffusion_CVPR_2023_paper.pdf) | PDF 已取得；读 §3、§4 设置、Table 1 与跨数据集 Table 3。 | CNN 学习扩散权重，并通过逐步 adjustment 保证输出下采样后恢复观测 LR 深度；提供显式测量一致性和跨数据集实验。它是检验 TLC 的 forward-model 解释与泛化主张的重要同任务参照。 |
| **DCDicL — Deep Convolutional Dictionary Learning for Image Denoising，CVPR 2021**。[正式全文](https://openaccess.thecvf.com/content/CVPR2021/papers/Zheng_Deep_Convolutional_Dictionary_Learning_for_Image_Denoising_CVPR_2021_paper.pdf)；[作者稿](https://www4.comp.polyu.edu.hk/~cslzhang/paper/DCDicL-cvpr21-final.pdf) | PDF 已取得；读 §3.1–3.2，核查网络与 adaptive dictionary 消融相关片段。 | 已明确学习系数先验与字典先验，按每幅输入调整字典，使用 HQS 展开。它是 learned regularization / input-adaptive inverse-problem unfolding 的方法谱系参照，任务为单模态去噪，相关性低于前三项。 |

## 4. 最接近的重叠与可能的独立贡献

### 已有部分

DCTNet 的 §3.1 Eq.(1) 为：

`F = 1/2 ||H−L||² + λ/2 ||𝓛(H)−𝓛(R̃)⊙W(R̃)||²`。

其中 𝓛 是 Laplacian filter，W 选择 guidance 中适于深度 SR 的结构。其 §3.2 进一步学习特征、边缘权重及 DCT 模块参数。故 TLC 的可审计差分应落在 **该类一致性残差的概率形状与惩罚形式、MD forward degradation 以及验证范围**，不能仅落在“从像素移到 Laplacian 域”上。[DCTNet §3](https://openaccess.thecvf.com/content/CVPR2022/papers/Zhao_Discrete_Cosine_Transform_Network_for_Guided_Depth_Map_Super-Resolution_CVPR_2022_paper.pdf)

LGCNet 已呈现“统计观察 → 显式 prior → 优化推导 → 展开网络 → 多数据集与可视化”的完整研究链。它的 Laplacian 一词指 **Laplace 分布**；TLC 中 Laplacian response 指 **空间微分算子**，两者需要分别核对。LGCNet §III 使用梯度残差 L1 项，§IV-C 还将梯度约束与像素约束在同一 TV/ADMM 设置下比较。论文报告 FAID 上三个噪声等级的均值 PSNR 改善分别为 1.38/1.19/1.10 dB；这证明前作已有机制对照的形式，不能作为 TLC 的实测收益。[LGCNet](https://doi.org/10.1109/TIP.2024.3489275)

### 摘要支持的候选新增

1. **跨模态 Laplacian response 残差的 Student-t 统计假设。** 若拟合与泛化证据充分，该假设提供了相对于 quadratic / Laplace penalties 的明确可检验变化。对一般 Student-t 模型，其负 log-density 对大残差的惩罚增长较慢；这是通用数学解释，本轮未读到 TLC 具体公式，不能推定其自由度、尺度或实现完全采用某一标准形式。
2. **在 SR 目标中引入输入相关的 MD forward model。** 摘要与 Generate_H 代码共同支持存在独立退化建模模块。其能否识别真实非均匀退化、是否只充当灵活补偿项，以及是否与 prior 分工明确，仍需论文推导和受控实验。
3. **将同一方法设计扩展到三类 MISR。** 三类测试目录和九组数据标识支持实现覆盖较宽。它是否使用统一权重、是否逐任务训练及泛化幅度均未核实；任务数量本身不能证明跨任务迁移。

**当前支持的工程结论：** TLCNet 与既有研究路线联系紧密，但摘要提出了可具体区分的残差分布和退化模型变化。现有材料不足以确认这些变化的独立效应，也不足以将其归结为只有模块替换。需要把技术差分的存在与差分有效性的证据分开评价。

## 5. 实验信号、比较条件与尚未核实内容

| 核查对象 | 当前可信信号 | 公平判断所需内容；本轮状态 |
|---|---|---|
| 整体效果 | 作者摘要报告三任务九数据集，公开测试脚本覆盖一致。 | TLC 的实际指标、第二名差距、失败样例、复现实测均未取得。不能写出具体增益，也不能把作者报告直接当独立复现。 |
| Student-t 主张 | 统计分布假设明确，并有对应 Laplacian 差值模块。 | 应查看同一 Laplacian residual 的 Gaussian/Laplace/Student-t 对照、held-out 拟合、参数自由度与尺度、图像间稳定性；本轮无法确认论文是否已覆盖。 |
| 先验独立收益 | DCTNet 提供同任务 Laplacian quadratic consistency 参照，LGCNet 提供跨模态 residual distribution 参照。 | 关键对照是在一致 backbone、训练预算与退化模型下，仅改变 residual prior；还需与无显式 prior 的 learned regularizer 比较。本轮未看到 TLC 消融表。 |
| MD 独立收益 | H 根据 x/y/z 生成并进入更新，表明实现有独立模块。 | 需要 fixed degradation、仅 MD、仅 TMC、两者联合及已知非均匀退化条件；模型容量应可比。当前不能断言这些消融缺失。 |
| 泛化与真实退化 | 测试配置跨 depth/thermal/pansharpening。 | 需区分多任务分别训练、跨数据集迁移、跨退化迁移和真实传感器实验。DADA 已有跨数据集及 LR consistency 指标，可作为检查形式的参照。 |
| 解释性 | 摘要提到 causal analysis，代码保留中间变量。 | 未取得 guidance intervention 的具体定义、随机/错位/遮挡处理方式、量化结果，以及其能支持多强的因果解释。可视化或命名对应不能单独确认求解等价性。 |
| 公平基线 | 近邻包含同团队 LGC/DeepM²CDL，以及同任务 DCTNet、DKN、DADA。 | LGC/DCDicL 原任务不同，需适配后才能比较 SR 效果；不得把它们原论文 PSNR 与 TLC 的 SR 指标横向拼接。DCTNet/DADA 也应统一数据划分、退化、倍率、单位、预训练和计算量。 |

DCTNet 的 Table 4 已区分去掉 DCT 模块、去掉可学习参数、改变共享滤波配置等因素。DADA 的 Table 3 已分别报告跨数据集高分辨率误差和下采样后的 LR consistency。它们证明相应机制验证方法在 TLC 时间线之前已经可用；它们没有证明 TLC 未做这些实验。[DCTNet](https://openaccess.thecvf.com/content/CVPR2022/papers/Zhao_Discrete_Cosine_Transform_Network_for_Guided_Depth_Map_Super-Resolution_CVPR_2022_paper.pdf)；[DADA](https://openaccess.thecvf.com/content/CVPR2023/papers/Metzger_Guided_Depth_Super-Resolution_by_Deep_Anisotropic_Diffusion_CVPR_2023_paper.pdf)

## 6. 本地材料索引

以下路径仅供此次校准审计，不作为面向读者的文献引用。

- `tlc-crossref.json`、`tlc-europepmc.json`、`tlc-europepmc-core.txt`：TLC 题目、摘要、首发表日期与全文可用性。
- `tlc-repo.json`、`tlc-commits.json`、`tlc-tree.json`、`tlc-old-tree.txt`：仓库与版本时间线。
- `tlc-old-model.py`：2025-10-12 代码版本；核心更新仍有占位。
- `tlc-rgbd-model.py`、`tlc-rgbd-test.sh`、`tlc-rgbt-test.sh`、`tlc-pan-test.sh`：固定 commit 的实现与测试配置。
- `dctnet.pdf/.txt`、`dad.pdf/.txt`、`dcdicl.pdf/.txt`、`cunet.pdf/.txt`、`dkn.pdf/.txt`：从 CVF/arXiv 正式公开来源下载的邻作。
- LGCNet 本轮读取文件：`/tmp/jingyi_xu_research/lgc.pdf` 与 `lgc.txt`，为上游提供的全文；本轮未重新确认其原下载位置。
- `deepm-readme.txt`、`deepm-crossref.json`：DeepM²CDL 官方仓库及日期证据；全文未取得。
