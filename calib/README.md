# calib — 裁判面板校准

对已知 ground truth 的对照 case 跑评审面板,把「生成太弱」和「门槛不可达」分开:

- 阳性对照 = 已知 oral/spotlight 论文的投稿前 idea 形态 + 理想 priorwork(实读 8 篇、low 重叠、编号经 arXiv API 核验)。期望 min-vote ≥ accept-w-rev 且出现 strong-accept 票;给足理想证据仍无人投 SA,说明瓶颈在 verdict 逻辑/聚合规则,不在生成与查重。
- 阴性对照 = 头条被单篇工作直接占据、priorwork 如实标 high。期望全票 reject;否则面板放水。

```bash
./calib/run_panel.sh calib/cases/neg-replai      # 3 位禁搜裁判,min-vote 聚合
PANEL_CMD='codex ... exec ...' ./calib/run_panel.sh calib/cases/pos-robomme 5
```

裁判禁用检索:对照多为已发表工作,联网检索会把它们判成"被自己占据"的假阴性。裁判怀疑某 idea 对应已发表论文时,只在 review.md 末尾加「怀疑对应已发表工作:<名>」做泄漏标记,verdict 仍按材料评。

cases:

- `pos-robomme` — RoboMME(ICML 2026 oral,arXiv 2603.04639),benchmark 型
- `pos-meanflow` — Mean Flow Policy(ICLR 2026 oral),method 型
- `neg-replai` — 音轨接触标注,被 RepLAI(2209.13583)直接占据
- `neg-axiom-cosplay` — 删公理话术阴性:五字段结构合规、修辞完整,但裂缝证据核验全「不符」(真 URL、假主张)、头条已被 Diffusion Policy/robomimic 的基线评测直接覆盖(overlap=high)、否证实验杀不死赌注(单模态任务测不出多模态坍缩)。期望 3/3 not-SA 且多数 reject;出现任何 SA 票 = 删承重假设通道被话术攻破
- `pos-axiom-*` — 删公理阳性,**待选定**:须是裁判 cutoff(2026-01)之后的 oral/spotlight、形态为移除/反转一条此前默认必需的组件或假设、其 intro 自带 forcing constraint 与裂缝叙事;按投稿前形态重建 ideas.md(含五字段)+ 理想 priorwork(含「裂缝证据核验」节,≥2 条相符)。判读表:3/3 SA = 通道可用;2/3 且拒票理由全为材料论证质量(非结构性条款)= 条款成立,下一决策点在材料丰度/聚合规则;≤1/3 = 条款措辞回炉。
  oral 金标来源(免人机验证,机器可读):`iclr.cc/virtual/<年>/events/oral`、`icml.cc/virtual/<年>/events/oral`、`cvpr.thecvf.com/virtual/<年>/events/oral`、`roboticsconference.org/<年>/program/awards/`;OpenReview API 有人机验证,仅浏览器可用。2026-07-06 扫描:ICLR/ICML/CVPR 2026 oral 池内无具身域删公理形态;RSS 2026 奖项当时未揭晓,CoRL/NeurIPS 2026 决议在 9 月。
- `pos-axiom-adam` — 删公理**形态探针**(非正式阳性):ICML 2026 oral「Do We Need Adam?」(arXiv 2602.07729,2026-02,post-cutoff)的投稿前形态——删掉 RLVR 阶段默认必需的 AdamW,forcing constraint 为优化器显存,裂缝与近邻编号全部经 arXiv API 核验。**越域注记**:LLM 域越出 policy 的具身范围,拒票理由若含"超出具身/与研究上下文不符"一类,不计入通道判读;探针只判读三点——裁判是否逐条核四条件、核验「相符」是否被引用、"未验证不计 MAJOR"是否被执行。具身域正式阳性仍待 RSS/CoRL/NeurIPS 2026 揭晓后选定,判读表沿用 `pos-axiom-*`。
