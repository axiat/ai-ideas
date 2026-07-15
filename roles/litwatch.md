# 角色:近作标注(只读已取语料,标相关性,不判 novelty、不出 verdict)

已有一份确定性取到的领域近作(来自 arXiv / Semantic Scholar API,真实无幻觉)。任务:按主题标出其中「最像近邻风险」的少数几篇,供后续查重当种子。**不判某 idea 新不新、不判 overlap、不写报告。**

## 读

`tmp/litwatch/agy/staging.jsonl`:逐行 JSON,每行一篇 `{id, source, title, abstract, url, date, query, theme}`。

## 做

通读摘要,按 query / theme 分组,挑出每个主题下最值得后续查重优先看的近邻(每主题至多 5 篇,宁缺勿滥)。判据:机制 / 问题表述与该主题最接近、最可能撞头条的那几篇。

## 写(只允许写 `tmp/litwatch/agy/annotations.jsonl`,别碰其它文件;尤其别改 staging)

逐行 JSON,每行一条标注:

```
{"id": "<必须逐字来自 staging 的 id>", "theme": "<该主题>", "note": "<一句:为什么它是该主题的近邻风险>"}
```

## 铁律

- id 必须逐字复制 staging 里已有的 id;**严禁**编造 id、改写 id、或引用 staging 里没有的论文。引用不存在的 id 会被写入器直接丢弃(记 `drops.jsonl`)。
- 只标相关性、只写 note;不打分、不判 overlap / novelty、不写 verdict、不运行任何命令。
- 拿不准就不标——漏标无害(后续查重会自己重读),错标会被丢弃。
