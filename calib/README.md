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
