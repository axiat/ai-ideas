## I1
检索词:问题表述(audio contact moment labeling manipulation pretraining);方法机制(audible interaction representation learning egocentric);相邻领域(audio-visual self-supervised learning)
API 检索:http://export.arxiv.org/api/query?id_list=2209.13583,2110.07058&max_results=5
最近工作:
- Learning State-Aware Visual Representations from Audible Interactions (RepLAI) | https://arxiv.org/abs/2209.13583 | 在 Ego4D/EPIC 上用音频瞬态定位交互时刻,以其为锚做视频表征预训练,下游含操作相关任务 | 头条机制完整重合:音轨=交互/接触时刻的免费标注器 → 注入视频表征
- Ego4D: Around the World in 3,000 Hours of Egocentric Video | https://arxiv.org/abs/2110.07058 | 数据源,含同步音轨 | 提供相同数据底座,不构成方法占位
- The Sound of Pixels | https://arxiv.org/abs/1804.03160 | 音画自监督对齐的经典工作 | 概念前驱:音频作为视觉学习的自由监督
最强反例:RepLAI(2209.13583)—— 音频瞬态定位交互时刻 + 以此预训练视频表征,在同数据源上已完整实现;本 idea 头条与其无 clear-accept 级差异,残余仅"下游换成操作 BC"。
重叠判定:high —— 头条机制已被 RepLAI 直接覆盖。
实读篇数:3
编号自查:是(全部经 arXiv API 实际命中并打开核对标题)
