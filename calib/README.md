# calib — 裁判面板校准

对已知 ground truth 的对照 case 跑评审面板,把「生成太弱」和「门槛不可达」分开:

- 阳性对照 = 已知 oral/spotlight 论文的投稿前 idea 形态 + 理想 priorwork(实读 8 篇、low 重叠、编号经 arXiv API 核验)。期望 min-vote ≥ accept-w-rev 且出现 strong-accept 票;给足理想证据仍无人投 SA,说明瓶颈在 verdict 逻辑/聚合规则,不在生成与查重。
- 阴性对照 = 头条被单篇工作直接占据、priorwork 如实标 high。期望全票 reject;否则面板放水。

两条校准跑道分开验证:**冻结校准**(`run_panel.sh`/`run_all.sh`)固定 ideas+priorwork,只验 verdict 逻辑与聚合规则;**端到端检索召回校准**(`run_e2e.sh`)放开检索(须真·联网后端),验查重进程能否召回已知占位。**效力边界**:机械断言只查召回产物的结构(重叠判定、占位命中、近邻/API 密度),纯文本无法证伪一个硬编码链接、甚至明写"未联网"的离线 agent——脚本本身不自证真检索,判读时须默认后端确实联网,其效力来自用真·联网后端跑 research 角色。阳性对照没有端到端跑法——已发表工作会被真检索找到、判成"被自己占据"的假阴性,故端到端只跑 direct-hit 阴性(检索召回侧)。

```bash
./calib/run_panel.sh calib/cases/neg-replai      # 3 位禁搜裁判(默认 claude),min-vote 聚合
PANEL_CMD='./grok-worker.sh' ./calib/run_panel.sh calib/cases/pos-meanflow           # grok 席,自动禁内建检索
PANEL_CMD='codex -c approval_policy=never exec -s workspace-write --skip-git-repo-check --ephemeral' \
  ./calib/run_panel.sh calib/cases/pos-robomme 5                                     # codex 席,禁搜故不开 --search/网络

./calib/run_all.sh                               # 批跑全部带 expect 的 case,按 expect 断言打分,
                                                 # 打印校准正确率;逐 case 结果追加 tmp/calib/summary.tsv
./calib/run_e2e.sh calib/cases/neg-replai        # 端到端检索召回:联网后端跑 roles/research.md,
                                                 # 断言 priorwork 召回已知占位(e2e.expect:overlap/url_contains)
```

每个 case 的判读预期机器化在 `cases/<case>/expect`(冻结面板断言:min_vote/sa_votes/reject_votes/all_votes;`probe` 只跑不打分)与 `cases/<case>/e2e.expect`(端到端断言);DSL 见 `run_all.sh`/`run_e2e.sh` 头注。票数阈值按默认 3 裁判书写,换裁判数须一并复核。

裁判禁用检索:对照多为已发表工作,联网检索会把它们判成"被自己占据"的假阴性。裁判怀疑某 idea 对应已发表论文时,只在 review.md 末尾加「怀疑对应已发表工作:<名>」做泄漏标记,verdict 仍按材料评。

隔离契约:每席裁判只见一次性镜像,bash 只拷回 verdict.tsv/review.md,真仓不作为裁判工作目录;镜像只隔文件树与 CWD,不隔网络/进程,越界写靠后端沙箱挡——PANEL_CMD 必须自带沙箱(claude allowlist / codex workspace-write / grok worker)。写域边界与已知绕过(间接写、继承的用户级 hooks/MCP)细节见 `grok-worker.sh` 头注。

cases:

- `pos-robomme` — RoboMME(ICML 2026 oral,arXiv 2603.04639),benchmark 型。2026-07-12 文献核实:MIKASA-Robo(2502.10550,早 11 个月)已占分类学+隔离任务族,诚实重叠 medium;真实 oral 立足于空缺维度(统一 VLA 骨干变体矩阵+长时程演示),priorwork 已按此重写——本 case 同时测"近邻同构但核心维度空缺"的差异定价。
- `neg-meanflow-mp1` — MeanFlow→动作生成迁移故事,被 MP1(2507.10543,2025-07,带代码)在 ICLR 2026 投稿前 72 天直接占据,且"一步化全靠蒸馏"前提早被 FlowPolicy(2412.04987,AAAI 2025 oral)/AdaFlow(2402.04292)证伪。原为 pos-meanflow(Mean Flow Policy,ICLR 2026 oral 2602.13810):核实发现真实 oral 靠 IVC 增量+RL 赛道错位+全文未引 MP1/FlowPolicy 存活,不构成"诚实全知查重下仍值 SA"的阳性,改作 direct-hit 阴性(2025 新占位,补 neg-replai 的 2022 老占位形态)。method 型阳性席位空缺,选定标准同 `pos-axiom-*`(cutoff 后 oral/spotlight 且头条经诚实查重仍零占据)。
- `neg-replai` — 音轨接触标注,被 RepLAI(2209.13583)直接占据
- `neg-axiom-cosplay` — 删公理话术阴性:五字段结构合规、修辞完整,但裂缝证据核验全「不符」(真 URL、假主张)、头条已被 Diffusion Policy/robomimic 的基线评测直接覆盖(overlap=high)、否证实验杀不死赌注(单模态任务测不出多模态坍缩)。期望 3/3 not-SA 且多数 reject;出现任何 SA 票 = 删承重假设通道被话术攻破
- `pos-axiom-*` — 删公理阳性选定标准(`pos-axiom-torque` 已按此落位):须是裁判 cutoff(2026-01)之后的 oral/spotlight、形态为移除/反转一条此前默认必需的组件或假设、其 intro 自带 forcing constraint 与裂缝叙事;按投稿前形态重建 ideas.md(含五字段)+ 理想 priorwork(含「裂缝证据核验」节,≥2 条相符)。判读表:3/3 SA = 通道可用;2/3 且拒票理由全为材料论证质量(非结构性条款)= 条款成立,下一决策点在材料丰度/聚合规则;≤1/3 = 条款措辞回炉。
  oral 金标来源(免人机验证,机器可读):`iclr.cc/virtual/<年>/events/oral`、`icml.cc/virtual/<年>/events/oral`、`cvpr.thecvf.com/virtual/<年>/events/oral`、`roboticsconference.org/<年>/program/awards/`;OpenReview API 有人机验证,仅浏览器可用。2026-07-06 扫描:ICLR/ICML/CVPR 2026 oral 池内无具身域删公理形态;CoRL/NeurIPS 2026 决议在 9 月。RSS 2026 奖项 07-16 揭晓(官方站 07-19 仍回 2025 内容,名单经官方 Instagram + arXiv 编号交叉验证)。
- `pos-axiom-torque` — 删公理**具身域正式阳性**:RSS 2026 Outstanding Systems Paper「NeuralActuator」(arXiv 2607.11734,2026-07,post-cutoff)的投稿前形态——删掉 actuator 动力学建模默认必需的扭矩真值监督(位姿轨迹经可微仿真反传出扭矩代理,随附低成本臂免传感器力感知),forcing constraint 为低成本平台的传感成本。裂缝与近邻编号经 arXiv API 核验;力感知轴最近占位(解析 DOB,2507.06174)如实入 priorwork,SA 净增量以其为 baseline。
- `pos-axiom-adam` — 删公理**形态探针**(非正式阳性):ICML 2026 oral「Do We Need Adam?」(arXiv 2602.07729,2026-02,post-cutoff)的投稿前形态——删掉 RLVR 阶段默认必需的 AdamW,forcing constraint 为优化器显存,裂缝与近邻编号全部经 arXiv API 核验。**越域注记**:LLM 域越出 policy 的具身范围,拒票理由若含"超出具身/与研究上下文不符"一类,不计入通道判读;探针只判读三点——裁判是否逐条核四条件、核验「相符」是否被引用、"未验证不计 MAJOR"是否被执行。具身域正式阳性已落 `pos-axiom-torque`,判读表沿用 `pos-axiom-*`。
