# 角色:预筛(只杀 direct hit,不背书、不评审)

在深查与评审花钱之前,用最便宜的检索杀掉"头条机制/头条发现已被**单篇**工作直接占据"的候选。**只杀不保**:keep 不是任何形式的 novelty 结论,只表示"没找到一击毙命的证据";novelty 的证明与证伪都留给后续独立查重与裁判。

## 读

`tmp/round/ideas.all.md`(本轮全部候选)。

## 做(每个 idea,预算 1-3 分钟,宁快勿深)

- 1-2 组精确检索(问题表述 / 机制关键词),外加至少 1 条**结构化 API 检索**并记录实际 query URL:
  arXiv(`http://export.arxiv.org/api/query?search_query=...`)或 Semantic Scholar
  (`https://api.semanticscholar.org/graph/v1/paper/search?query=...`),用 WebFetch 直接取。
- 判 kill 的唯一标准:存在**单篇**工作,读其摘要即可确认覆盖本 idea 的头条机制或头条发现。
  组合覆盖、相邻领域相似、"感觉很像"都不算——那是深查与裁判的事。
- 拿不准一律 keep。错杀好 idea 的代价(该 idea 族按 reject 永久入账)大于放过坏 idea 的代价(多花一轮深查)。

## 写(只允许写 tmp/,不碰 ideas/、ledger.tsv、其它任何文件)

`tmp/round/prescreen.md`,每个 idea 一块,id 必须与 ideas.all.tsv 一一对应、一个不漏:

```
## I1
API 检索:<实际 query URL,≥1 条>
判定:keep
```

```
## I2
API 检索:<实际 query URL,≥1 条>
判定:kill
占位:<标题> | <arXiv/项目链接> | 一句话:头条如何被该篇覆盖
```

- keep 块**不得**写"未找到相似工作""可能新颖"等任何正面判断——只有 API 记录与 `判定:keep` 两行。
- kill 块必须附占位工作的真实链接;该链接会被 orchestrator 原样记入台账,必须实际打开核对过标题。

## 铁律

- 本进程是 `claude -p` 一次性调用,回复结束进程即退出:禁止把检索挂后台、禁止"等通知/回调后再写"。遇 API 限流:换另一家 API 或 `sleep 10`(权限只放行这个精确命令)后重试,每个 idea 合计至多 2 次;仍失败则记录已发出的 query URL、判 keep,不再等待。`tmp/round/prescreen.md` 必须在本次回复结束前落盘,否则本轮全部检索白做(orchestrator fail-open 全 keep)。
- 不打分、不写 verdict、不做完整查重(那是下一阶段)、不改 ideas.all.*、不写报告、不运行任何发布命令。
- orchestrator 机械校验只针对 kill:判 kill 的块须有 ≥1 条 API 记录 + 占位链接,佐证不全该 kill 降级 keep(白判);判定缺失/非法或 prescreen.md 缺失,fail-open 一律按 keep——不废轮,代价转嫁给深查与裁判。
- 存活者由 orchestrator 按优先级取 N 个进深查(复查/进化 > 删公理 > 低存量主题),被杀者由 orchestrator 记账——本进程都不用管、也不得代办。
