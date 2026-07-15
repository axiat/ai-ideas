#!/usr/bin/env bash
# litwatch 测试。确定性核(parse/ingest)与编排全离线;agy 相关用 stub;
# 末尾一条联网 smoke,网络问题只 SKIP 不 FAIL。
set -u
repo="$(cd "$(dirname "$0")" && pwd)"; cd "$repo" || exit 1
py="$repo/lib/litwatch.py"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
pass=0; fail=0; skip=0
ok(){ pass=$((pass+1)); printf 'ok    %s\n' "$1"; }
no(){ fail=$((fail+1)); printf 'FAIL  %s\n' "$1"; }
sk(){ skip=$((skip+1)); printf 'skip  %s\n' "$1"; }

# ---------- fixtures ----------
cat > "$work/arxiv.xml" <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.11111v2</id>
    <title>Frozen VLA Steering via Latent Noise</title>
    <summary>We steer a frozen vision-language-action policy in latent noise space.</summary>
    <published>2024-01-20T00:00:00Z</published>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2402.22222v1</id>
    <title>World Models for Manipulation</title>
    <summary>A latent dynamics world model for long-horizon manipulation.</summary>
    <published>2024-02-15T00:00:00Z</published>
  </entry>
</feed>
XML

cat > "$work/s2.json" <<'JSON'
{"data":[
 {"paperId":"aaa","title":"S2 With ArXiv","abstract":"x","url":"u","publicationDate":"2024-03-01","externalIds":{"ArXiv":"2403.33333"}},
 {"paperId":"bbb","title":"S2 No ArXiv","abstract":"y","publicationDate":"2024-03-02","externalIds":{}}
]}
JSON

cat > "$work/staging.jsonl" <<'JSON'
{"id":"arxiv:2401.11111","source":"arxiv","title":"A","abstract":"aa","url":"https://arxiv.org/abs/2401.11111","date":"2024-01-20"}
{"id":"arxiv:2402.22222","source":"arxiv","title":"B","abstract":"bb","url":"https://arxiv.org/abs/2402.22222","date":"2024-02-15"}
{"id":"s2:bbb","source":"s2","title":"C","abstract":"cc","url":"u","date":"2024-03-02"}
JSON

# 标注:1 合法 · 1 越界(未取到的 id)· 1 坏行 · 1 重复
cat > "$work/ann.jsonl" <<'JSON'
{"id":"arxiv:2401.11111","theme":"vla","note":"close neighbor"}
{"id":"arxiv:9999.99999","theme":"vla","note":"injected fake"}
this is not json
{"id":"arxiv:2401.11111","theme":"vla","note":"dup should drop"}
JSON

# ---------- T1 parse arxiv ----------
python3 "$py" parse --source arxiv --input "$work/arxiv.xml" --theme vla > "$work/p_arxiv.jsonl" 2>/dev/null
if python3 - "$work/p_arxiv.jsonl" <<'PY'
import sys,json
r=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]
assert len(r)==2, len(r)
assert [x["id"] for x in r]==["arxiv:2401.11111","arxiv:2402.22222"], r
assert r[0]["url"]=="https://arxiv.org/abs/2401.11111", r[0]["url"]
assert r[0]["title"].startswith("Frozen VLA"), r[0]["title"]
assert r[0]["date"]=="2024-01-20", r[0]["date"]
assert r[0]["theme"]=="vla"
PY
then ok "T1 parse arxiv: 2 recs, ids/url/date/theme"; else no "T1 parse arxiv"; fi

# ---------- T2 parse s2 (arxiv-map + s2 fallback) ----------
python3 "$py" parse --source s2 --input "$work/s2.json" > "$work/p_s2.jsonl" 2>/dev/null
if python3 - "$work/p_s2.jsonl" <<'PY'
import sys,json
r=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]
assert len(r)==2, len(r)
assert r[0]["id"]=="arxiv:2403.33333" and r[0]["source"]=="arxiv", r[0]
assert r[1]["id"]=="s2:bbb" and r[1]["source"]=="s2", r[1]
PY
then ok "T2 parse s2: arxiv-map + s2 fallback"; else no "T2 parse s2"; fi

# ---------- T3 ingest trust boundary (核心正确性保证) ----------
python3 "$py" ingest --staging "$work/staging.jsonl" --annotations "$work/ann.jsonl" \
  --drop-log "$work/drops.jsonl" --out "$work/index.jsonl" 2>/dev/null
if python3 - "$work/index.jsonl" "$work/drops.jsonl" "$work/staging.jsonl" <<'PY'
import sys,json
idx=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]
drp=[json.loads(l) for l in open(sys.argv[2]) if l.strip()]
stg=[json.loads(l) for l in open(sys.argv[3]) if l.strip()]
idx_ids=[x["id"] for x in idx]; stg_ids=[x["id"] for x in stg]
# index ⊆ staging,无新增(agy 塞不进新论文)
assert set(idx_ids)<=set(stg_ids), idx_ids
assert "arxiv:9999.99999" not in idx_ids, "越界 id 泄漏进 index!"
assert len(idx)==3, len(idx)
# 合法标注挂上,重复取首条
note={x["id"]:x.get("agy_note") for x in idx}
assert note["arxiv:2401.11111"]=="close neighbor", note
assert note["arxiv:2402.22222"] is None
# drop 日志:越界 + 坏行 + 重复 各一
reasons=sorted(d["reason"] for d in drp)
assert reasons==["dup","malformed","out-of-set"], reasons
PY
then ok "T3 ingest 信任边界:越界/坏行/重复丢弃,index⊆staging,注释挂对"; else no "T3 ingest 信任边界"; fi

# ---------- T4 ingest 无标注 = 去重后的 staging ----------
python3 "$py" ingest --staging "$work/staging.jsonl" --out "$work/index2.jsonl" 2>/dev/null
if python3 - "$work/index2.jsonl" <<'PY'
import sys,json
r=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]
assert len(r)==3, len(r)
assert all("agy_note" not in x for x in r)
PY
then ok "T4 ingest 无标注:index=staging,无 agy_note"; else no "T4 ingest 无标注"; fi

# ---------- T5 ingest 去重(keep-first) ----------
cat > "$work/dup.jsonl" <<'JSON'
{"id":"arxiv:1","title":"first","source":"arxiv","url":"u1","abstract":"","date":""}
{"id":"arxiv:1","title":"second","source":"arxiv","url":"u2","abstract":"","date":""}
JSON
python3 "$py" ingest --staging "$work/dup.jsonl" --out "$work/index3.jsonl" 2>/dev/null
if python3 - "$work/index3.jsonl" <<'PY'
import sys,json
r=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]
assert len(r)==1 and r[0]["title"]=="first", r
PY
then ok "T5 ingest 去重:同 id 取首条"; else no "T5 ingest 去重"; fi

# ---------- T6 agy-worker AGY_OUT_HINT 落点 + 默认不变 ----------
stubbin="$work/bin"; mkdir -p "$stubbin"
cat > "$stubbin/agy" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$work/agy_argv"
exit 0
EOF
chmod +x "$stubbin/agy"
AGY_LAUNCH_GAP_SEC=0 AGY_OUT_HINT=tmp/litwatch PATH="$stubbin:$PATH" ./agy-worker.sh "TESTPROMPT" >/dev/null 2>&1
hint_hit=$(grep -c 'tmp/litwatch' "$work/agy_argv" 2>/dev/null); hint_hit=${hint_hit:-0}
AGY_LAUNCH_GAP_SEC=0 PATH="$stubbin:$PATH" ./agy-worker.sh "TESTPROMPT" >/dev/null 2>&1
def_hit=$(grep -c 'tmp/round' "$work/agy_argv" 2>/dev/null); def_hit=${def_hit:-0}
if [ "$hint_hit" -ge 1 ] && [ "$def_hit" -ge 1 ]; then
  ok "T6 agy-worker:OUT_HINT 改落点、默认仍 tmp/round"
else
  no "T6 agy-worker OUT_HINT (hint=$hint_hit def=$def_hit)"
fi

# ---------- T7 litwatch.sh e2e(预置 staging + stub agy)----------
cat > "$work/stub_agy.sh" <<'EOF'
#!/usr/bin/env bash
# 忽略 prompt,按 AGY_OUT_HINT 写 annotations(含一条越界)
cat > "${AGY_OUT_HINT}/annotations.jsonl" <<'JSON'
{"id":"arxiv:2401.11111","theme":"vla","note":"stub neighbor"}
{"id":"arxiv:9999.99999","theme":"vla","note":"stub fake"}
JSON
exit 0
EOF
chmod +x "$work/stub_agy.sh"
LITWATCH_DIR="$work/lw" LITWATCH_PREBUILT_STAGING="$work/staging.jsonl" \
  LITWATCH_AGY_CMD="$work/stub_agy.sh" LITWATCH_FETCH_GAP=0 ./litwatch.sh >/dev/null 2>&1
if python3 - "$work/lw/index.jsonl" "$work/lw/drops.jsonl" <<'PY'
import sys,json
idx=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]
drp=[json.loads(l) for l in open(sys.argv[2]) if l.strip()]
ids=[x["id"] for x in idx]
assert len(idx)==3, len(idx)
assert "arxiv:9999.99999" not in ids, "stub 越界 id 进了 index"
note={x["id"]:x.get("agy_note") for x in idx}
assert note["arxiv:2401.11111"]=="stub neighbor", note
assert any(d["reason"]=="out-of-set" for d in drp), drp
PY
then ok "T7 litwatch.sh e2e:index 建成、越界丢弃、注释挂上"; else no "T7 litwatch.sh e2e"; fi

# ---------- T8 litwatch.sh 无 agy 仍产 index(agy 挂掉零回归)----------
LITWATCH_DIR="$work/lw2" LITWATCH_PREBUILT_STAGING="$work/staging.jsonl" \
  LITWATCH_NO_AGY=1 LITWATCH_FETCH_GAP=0 ./litwatch.sh >/dev/null 2>&1
if python3 - "$work/lw2/index.jsonl" <<'PY'
import sys,json
r=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]
assert len(r)==3 and all("agy_note" not in x for x in r), r
PY
then ok "T8 litwatch.sh NO_AGY:确定性核照样产 index"; else no "T8 litwatch.sh NO_AGY"; fi

# ---------- T9 ingest 抗类型垃圾标注(zero-regression 前提)----------
cat > "$work/stg2.jsonl" <<'JSON'
{"id":"arxiv:1","source":"arxiv","title":"one","abstract":"","url":"u1","date":""}
{"id":"arxiv:2","source":"arxiv","title":"two","abstract":"","url":"u2","date":""}
JSON
cat > "$work/ann_bad.jsonl" <<'JSON'
{"id":["arxiv:1"],"note":"list id"}
{"id":{"x":1},"note":"dict id"}
{"id":"arxiv:1","note":5}
{"id":"arxiv:1","note":["a","b"]}
[1,2,3]
{"id":"arxiv:2","note":"good"}
JSON
if python3 "$py" ingest --staging "$work/stg2.jsonl" --annotations "$work/ann_bad.jsonl" \
     --drop-log "$work/drops2.jsonl" --out "$work/index_bad.jsonl" 2>/dev/null \
   && python3 - "$work/index_bad.jsonl" <<'PY'
import sys,json
r=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]
assert len(r)==2, len(r)
note={x["id"]:x.get("agy_note") for x in r}
assert note["arxiv:2"]=="good", note
assert note["arxiv:1"] is None, note   # 非串 note 被忽略,不塞垃圾
PY
then ok "T9 ingest 抗类型垃圾:不崩、index=staging、垃圾标注丢弃"; else no "T9 ingest 抗类型垃圾"; fi

# ---------- T10 litwatch.sh:agy 写类型垃圾也零回归 ----------
cat > "$work/stub_agy_garbage.sh" <<'EOF'
#!/usr/bin/env bash
cat > "${AGY_OUT_HINT}/annotations.jsonl" <<'JSON'
{"id":["arxiv:2401.11111"],"note":"garbage list id"}
{"id":"arxiv:2401.11111","note":42}
JSON
exit 0
EOF
chmod +x "$work/stub_agy_garbage.sh"
LITWATCH_DIR="$work/lw3" LITWATCH_PREBUILT_STAGING="$work/staging.jsonl" \
  LITWATCH_AGY_CMD="$work/stub_agy_garbage.sh" LITWATCH_FETCH_GAP=0 ./litwatch.sh >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 0 ] && python3 - "$work/lw3/index.jsonl" <<'PY'
import sys,json
r=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]
assert len(r)==3, len(r)                        # = 去重后 staging
assert all("agy_note" not in x for x in r), r   # 垃圾标注全被丢
PY
then ok "T10 litwatch.sh:agy 写类型垃圾仍产 index(零回归)"; else no "T10 litwatch.sh 类型垃圾零回归 (rc=$rc)"; fi

# ---------- T11 联网 smoke(问题只 SKIP)----------
if smoke=$(python3 "$py" fetch --source arxiv --query "reinforcement learning" --max 3 2>/dev/null) \
   && printf '%s' "$smoke" | python3 -c 'import sys,json
ls=[l for l in sys.stdin if l.strip()]
assert ls, "no recs"
assert json.loads(ls[0])["id"].startswith("arxiv:"), ls[0]' 2>/dev/null; then
  ok "T11 smoke:live arXiv fetch 解析出 arxiv: 记录"
else
  sk "T11 smoke:网络不可用或限流,跳过"
fi

# ---------- T12 信任边界:agy 篡改沙箱 staging,假论文进不了 index ----------
cat > "$work/stub_agy_poison.sh" <<'EOF'
#!/usr/bin/env bash
# 混乱/被注入的 agy:往自己沙箱写一份伪造 staging(含未取到的假论文)+ 越界标注
cat > "${AGY_OUT_HINT}/staging.jsonl" <<'JSON'
{"id":"arxiv:0000.00000","source":"arxiv","title":"FABRICATED","abstract":"z","url":"x","date":"2099-01-01"}
JSON
cat > "${AGY_OUT_HINT}/annotations.jsonl" <<'JSON'
{"id":"arxiv:0000.00000","theme":"vla","note":"fake"}
JSON
exit 0
EOF
chmod +x "$work/stub_agy_poison.sh"
LITWATCH_DIR="$work/lw4" LITWATCH_PREBUILT_STAGING="$work/staging.jsonl" \
  LITWATCH_AGY_CMD="$work/stub_agy_poison.sh" LITWATCH_FETCH_GAP=0 ./litwatch.sh >/dev/null 2>&1
if python3 - "$work/lw4/index.jsonl" <<'PY'
import sys,json
r=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]
ids=[x["id"] for x in r]
assert "arxiv:0000.00000" not in ids, "agy 篡改沙箱 staging 把假论文塞进了 index!"
assert len(r)==3, len(r)   # index 仍 = 可信取数 staging(未被 agy 沙箱污染)
PY
then ok "T12 信任边界:agy 篡改沙箱 staging,假论文进不了 index"; else no "T12 信任边界 staging 篡改"; fi

# ---------- T13 _http_get 429/503 退避重试(不联网,monkeypatch)----------
if python3 - <<'PY'
import sys, io
sys.path.insert(0, "lib")
import litwatch, urllib.request, urllib.error
litwatch.time.sleep = lambda s: None      # 别真睡
calls = {"n": 0}
def once_429(req, timeout=0):
    calls["n"] += 1
    if calls["n"] == 1:
        raise urllib.error.HTTPError(req.full_url, 429, "rate", {"Retry-After": "0"}, None)
    return io.BytesIO(b"OK")
urllib.request.urlopen = once_429
assert litwatch._http_get("https://x", retries=2) == "OK", "429 后应重试成功"
assert calls["n"] == 2, calls
calls["n"] = 0
def always_503(req, timeout=0):
    calls["n"] += 1
    raise urllib.error.HTTPError(req.full_url, 503, "down", {}, None)
urllib.request.urlopen = always_503
try:
    litwatch._http_get("https://x", retries=2)
    raise SystemExit("重试耗尽应抛异常")
except urllib.error.HTTPError:
    pass
assert calls["n"] == 3, calls             # 1 + 2 retries
PY
then ok "T13 _http_get 429/503 退避重试 + Retry-After"; else no "T13 _http_get 退避重试"; fi

# ---------- OAI-PMH fixture ----------
cat > "$work/oai.xml" <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <ListRecords>
    <record>
      <header><identifier>oai:arXiv.org:2607.10001</identifier><datestamp>2026-07-10</datestamp></header>
      <metadata><arXiv xmlns="http://arxiv.org/OAI/arXiv/">
        <id>2607.10001</id><created>2026-07-08</created>
        <title>A Vision-Language-Action Policy with Reinforcement Learning</title>
        <categories>cs.RO cs.LG</categories>
        <abstract>We post-train a vision-language-action policy using reinforcement learning.</abstract>
      </arXiv></metadata>
    </record>
    <record>
      <header status="deleted"><identifier>oai:arXiv.org:2607.10002</identifier><datestamp>2026-07-10</datestamp></header>
    </record>
    <record>
      <header><identifier>oai:arXiv.org:2607.10003</identifier><datestamp>2026-07-10</datestamp></header>
      <metadata><arXiv xmlns="http://arxiv.org/OAI/arXiv/">
        <id>2607.10003</id><created>2026-07-09</created>
        <title>Sector Rotation by Factor Model</title>
        <categories>q-fin.PM</categories>
        <abstract>An analytical approach to sector rotation using factor models.</abstract>
      </arXiv></metadata>
    </record>
    <resumptionToken>tok123</resumptionToken>
  </ListRecords>
</OAI-PMH>
XML

# ---------- T14 parse_oai(deleted 跳过 + categories/date/token)----------
if python3 - "$work/oai.xml" <<'PY'
import sys; sys.path.insert(0, "lib"); import litwatch
recs, tok = litwatch.parse_oai(open(sys.argv[1], encoding="utf-8").read())
assert tok == "tok123", tok
assert [r["id"] for r in recs] == ["arxiv:2607.10001", "arxiv:2607.10003"], recs
assert recs[0]["categories"] == "cs.RO cs.LG", recs[0]
assert recs[0]["date"] == "2026-07-08", recs[0]
assert recs[0]["url"] == "https://arxiv.org/abs/2607.10001", recs[0]
PY
then ok "T14 parse_oai:deleted 跳过 + categories/date/token"; else no "T14 parse_oai"; fi

# ---------- T15 filter_tag(类别白名单 + 关键词命中 + 打标 + 去重)----------
if python3 - "$work/oai.xml" <<'PY'
import sys; sys.path.insert(0, "lib"); import litwatch
recs, _ = litwatch.parse_oai(open(sys.argv[1], encoding="utf-8").read())
themes = [("vla", ["vision-language-action"])]
kept = litwatch.filter_tag(recs, themes, cats=["cs.RO", "cs.LG"])
assert [r["id"] for r in kept] == ["arxiv:2607.10001"], kept   # q-fin 被类别挡,vla 命中
assert kept[0]["theme"] == "vla", kept[0]
assert len(litwatch.filter_tag(recs + recs, themes, cats=["cs.RO", "cs.LG"])) == 1   # 去重
assert litwatch.filter_tag(recs, [("x", ["nonexistent-kw"])], cats=None) == []       # 无命中全丢
PY
then ok "T15 filter_tag:类别白名单 + 关键词命中 + 打标 + 去重"; else no "T15 filter_tag"; fi

echo "----"
printf 'litwatch tests: %d ok, %d fail, %d skip\n' "$pass" "$fail" "$skip"
[ "$fail" -eq 0 ]
