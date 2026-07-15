#!/usr/bin/env python3
"""litwatch —— 领域近作监视的确定性取数/准入核。见 LITWATCH-DRAFT.md。

只处理数据、不含判断。三个子命令:
  parse   本地 API 响应文件 → 规范化 records JSONL(离线,可测)。
  fetch   打 arXiv / Semantic Scholar 端点 → parse(联网)。
  ingest  staging records + agy 标注 → index JSONL。标注只能挂到 staging 里
          已有的 id;越界(引用未取到的 id)/坏行/重复一律丢弃并记 drop 日志。
          record 只来自 API 响应,agy 结构上塞不进新记录。

record 逐行 JSON: {id, source, title, abstract, url, date[, query, theme]}
agy 标注逐行 JSON: {id, theme?, note}   id 必须 ∈ staging
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ARXIV_API = "https://export.arxiv.org/api/query"   # http 会 301→https
S2_API = "https://api.semanticscholar.org/graph/v1/paper/search"
ATOM = "{http://www.w3.org/2005/Atom}"
UA = "litwatch/0.1 (ai-ideas idea-hunt; research use)"


def _norm_arxiv_id(raw):
    # http://arxiv.org/abs/2401.12345v2 -> 2401.12345
    # http://arxiv.org/abs/cs/0309136v1 -> cs/0309136(保留老式类别前缀,url 才可达)
    raw = (raw or "").strip()
    for pre in ("http://arxiv.org/abs/", "https://arxiv.org/abs/"):
        if raw.startswith(pre):
            raw = raw[len(pre):]
            break
    else:
        raw = raw.rsplit("/", 1)[-1]
    return re.sub(r"v\d+$", "", raw)


def _squash(s):
    if not isinstance(s, str):
        s = "" if s is None else str(s)
    return " ".join(s.split())


def _arxiv_search_query(q):
    # theme 行可直接写 arXiv 查询表达式(带 all:/ti:/abs: 字段或 AND/OR 布尔);
    # 纯文本则整体裹进 all:(),否则空格分词会被当松散 OR、配 date 排序召回近作噪声。
    ql = q.lower()
    if any(op in ql for op in ("all:", "ti:", "abs:", "cat:")) or \
       any(b in " " + ql + " " for b in (" and ", " or ", " andnot ")):
        return q
    return "all:" + q


def parse_arxiv(xml_text):
    recs = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        sys.stderr.write("litwatch parse_arxiv: 无法解析 xml: %s\n" % e)
        return recs
    for e in root.findall(ATOM + "entry"):
        rid = _squash(e.findtext(ATOM + "id"))
        if not rid:
            continue
        aid = _norm_arxiv_id(rid)
        title = _squash(e.findtext(ATOM + "title"))
        if not aid or not title:
            continue
        recs.append({
            "id": "arxiv:" + aid,
            "source": "arxiv",
            "title": title,
            "abstract": _squash(e.findtext(ATOM + "summary")),
            "url": "https://arxiv.org/abs/" + aid,
            "date": _squash(e.findtext(ATOM + "published"))[:10],
        })
    return recs


def parse_s2(json_text):
    recs = []
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        sys.stderr.write("litwatch parse_s2: 无法解析 json: %s\n" % e)
        return recs
    if not isinstance(data, dict):
        return recs
    for p in data.get("data") or []:
        ext = p.get("externalIds") or {}
        arx = ext.get("ArXiv")
        if arx:
            rid, url, src = "arxiv:" + arx, "https://arxiv.org/abs/" + arx, "arxiv"
        else:
            pid = p.get("paperId")
            if not pid:
                continue
            rid = "s2:" + pid
            url = p.get("url") or ("https://www.semanticscholar.org/paper/" + pid)
            src = "s2"
        title = _squash(p.get("title"))
        if not title:
            continue
        recs.append({
            "id": rid,
            "source": src,
            "title": title,
            "abstract": _squash(p.get("abstract")),
            "url": url,
            "date": (p.get("publicationDate") or "")[:10],
        })
    return recs


def _http_get(url, headers=None, retries=2, backoff=5):
    # arXiv API 实测会间歇 429/503/超时(尤其未缓存的复杂 query);退避重试并认 Retry-After。
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < retries:
                ra = e.headers.get("Retry-After") if e.headers else None
                try:
                    wait = int(ra)
                except (TypeError, ValueError):
                    wait = backoff * (attempt + 1)
                wait = min(wait, 60)
                sys.stderr.write("litwatch http %d,%ds 后重试(%d/%d)\n" % (e.code, wait, attempt + 1, retries))
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, OSError) as e:
            if attempt < retries:
                wait = backoff * (attempt + 1)
                sys.stderr.write("litwatch http %s,%ds 后重试(%d/%d)\n" % (type(e).__name__, wait, attempt + 1, retries))
                time.sleep(wait)
                continue
            raise


def fetch(source, query, max_results, window_days=0, sort="submittedDate"):
    headers = None
    if source == "arxiv":
        url = ARXIV_API + "?" + urllib.parse.urlencode({
            "search_query": _arxiv_search_query(query),
            "start": 0,
            "max_results": max_results,
            "sortBy": sort,          # submittedDate=近作优先(可能带 OR 噪声);relevance=相关优先
            "sortOrder": "descending",
        })
    elif source == "s2":
        url = S2_API + "?" + urllib.parse.urlencode({
            "query": query,
            "limit": max_results,
            "fields": "title,abstract,url,publicationDate,externalIds",
        })
        key = os.environ.get("LITWATCH_S2_KEY")   # 免 key 的 S2 免费额度基本 429;有 key 才可靠
        if key:
            headers = {"x-api-key": key}
    else:
        raise SystemExit("unknown source: " + source)
    try:
        body = _http_get(url, headers)
    except (urllib.error.URLError, OSError) as e:   # 含 HTTPError(429)/超时/连接错:干净跳过,不 traceback
        sys.stderr.write("litwatch fetch %s 取数失败(%s): %s\n" % (source, type(e).__name__, e))
        return [], url
    recs = parse_arxiv(body) if source == "arxiv" else parse_s2(body)
    if window_days:
        cutoff = (datetime.date.today() - datetime.timedelta(days=window_days)).isoformat()
        recs = [r for r in recs if (r.get("date") or "0000-00-00") >= cutoff]
    return recs, url


def ingest(staging_path, ann_path, drop_log_path=None):
    staging, order = {}, []
    with open(staging_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            rid = r.get("id")
            if not rid or rid in staging:  # keep-first dedup
                continue
            staging[rid] = r
            order.append(rid)

    drops, seen = [], set()
    if ann_path:
        try:
            af = open(ann_path, encoding="utf-8")
        except FileNotFoundError:
            af = None
        if af:
            with af:
                for i, line in enumerate(af, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        a = json.loads(line)
                    except json.JSONDecodeError:
                        drops.append({"reason": "malformed", "line": i, "raw": line[:200]})
                        continue
                    if not isinstance(a, dict):
                        drops.append({"reason": "malformed", "line": i})
                        continue
                    rid = a.get("id")
                    if not isinstance(rid, str) or not rid:
                        drops.append({"reason": "bad-id", "line": i, "id": rid})
                        continue
                    if rid not in staging:
                        drops.append({"reason": "out-of-set", "line": i, "id": rid})
                        continue
                    if rid in seen:
                        drops.append({"reason": "dup", "line": i, "id": rid})
                        continue
                    seen.add(rid)
                    note = a.get("note")
                    if isinstance(note, str) and _squash(note):
                        staging[rid]["agy_note"] = _squash(note)
                    theme = a.get("theme")
                    if isinstance(theme, str) and theme:
                        staging[rid]["theme"] = theme

    if drop_log_path:
        with open(drop_log_path, "w", encoding="utf-8") as g:
            for d in drops:
                g.write(json.dumps(d, ensure_ascii=False) + "\n")
    return [staging[rid] for rid in order], drops


def _emit(recs, out):
    fh = open(out, "w", encoding="utf-8") if out else sys.stdout
    try:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    finally:
        if out:
            fh.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="litwatch")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("parse", help="解析本地 API 响应文件")
    p.add_argument("--source", required=True, choices=["arxiv", "s2"])
    p.add_argument("--input", required=True)
    p.add_argument("--query", default="")
    p.add_argument("--theme", default="")
    p.add_argument("--out", default="")

    f = sub.add_parser("fetch", help="联网取数并解析")
    f.add_argument("--source", required=True, choices=["arxiv", "s2"])
    f.add_argument("--query", required=True)
    f.add_argument("--max", type=int, default=25)
    f.add_argument("--window-days", type=int, default=0)
    f.add_argument("--sort", default="submittedDate",
                   choices=["submittedDate", "relevance", "lastUpdatedDate"])
    f.add_argument("--theme", default="")
    f.add_argument("--out", default="")

    g = sub.add_parser("ingest", help="标注挂到已取 record,产出 index")
    g.add_argument("--staging", required=True)
    g.add_argument("--annotations", default="")
    g.add_argument("--drop-log", default="")
    g.add_argument("--out", default="")

    args = ap.parse_args(argv)

    if args.cmd == "parse":
        text = open(args.input, encoding="utf-8").read()
        recs = parse_arxiv(text) if args.source == "arxiv" else parse_s2(text)
        for r in recs:
            if args.query:
                r["query"] = args.query
            if args.theme:
                r["theme"] = args.theme
        _emit(recs, args.out)
    elif args.cmd == "fetch":
        recs, url = fetch(args.source, args.query, args.max, args.window_days, args.sort)
        for r in recs:
            r["query"] = args.query
            if args.theme:
                r["theme"] = args.theme
        sys.stderr.write("litwatch fetch %s -> %d recs | %s\n" % (args.source, len(recs), url))
        _emit(recs, args.out)
    elif args.cmd == "ingest":
        recs, drops = ingest(args.staging, args.annotations or None, args.drop_log or None)
        sys.stderr.write("litwatch ingest -> %d recs, %d annotations dropped\n" % (len(recs), len(drops)))
        _emit(recs, args.out)


if __name__ == "__main__":
    main()
