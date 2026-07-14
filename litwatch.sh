#!/usr/bin/env bash
# 领域近作监视 orchestrator。见 LITWATCH-DRAFT.md。
# 独立常驻进程,不进 hunt.sh 回路。三段:
#   1) 取数(确定性,python + arXiv/S2 API,不经 agy)→ tmp/litwatch/staging.jsonl
#   2) agy 标注(可选、best-effort,走 agy-worker.sh 继承全局冷却闸)→ annotations.jsonl
#   3) 准入(确定性,ingest 只让标注挂到已取 id)→ tmp/litwatch/index.jsonl
# agy 任何一步失败只记日志,index 照样由确定性核产出(agy 挂掉零回归)。
#
# 配置(env):
#   LITWATCH_DIR          输出目录(默认 tmp/litwatch)
#   LITWATCH_MAX          每主题每源最多取几条(默认 25)
#   LITWATCH_WINDOW       近 N 天(默认 60;0 关闭窗口过滤)
#   LITWATCH_SOURCES      空格分隔,arxiv/s2(默认 "arxiv s2")
#   LITWATCH_THEMES_FILE  每行一个 query,覆盖默认主题
#   LITWATCH_FETCH_GAP    每次取数之间 sleep 秒(默认 3,温柔限速;测试设 0)
#   LITWATCH_NO_AGY=1     跳过 agy 标注段(纯确定性)
#   LITWATCH_AGY_CMD      agy 标注命令(默认 ./agy-worker.sh,经它继承冷却闸)
#   LITWATCH_PREBUILT_STAGING  给定则跳过取数、直接用该 staging(测试/离线用)
set -u
repo="$(cd "$(dirname "$0")" && pwd)"; cd "$repo" || { echo "litwatch: 无法进入仓库根 $repo" >&2; exit 1; }
py="$repo/lib/litwatch.py"
dir=${LITWATCH_DIR:-tmp/litwatch}
max=${LITWATCH_MAX:-25}
window=${LITWATCH_WINDOW:-60}
sources=${LITWATCH_SOURCES:-arxiv s2}
gap=${LITWATCH_FETCH_GAP:-3}
mkdir -p "$dir"
log(){ printf '[litwatch %s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

# 默认主题词(research_context.md 兴趣词;可用 LITWATCH_THEMES_FILE 覆盖)。
default_themes(){ cat <<'EOF'
vision language action reinforcement learning post-training
world model latent dynamics model-based reinforcement learning robot
VLM reward shaping reinforcement learning manipulation
inference-time steering frozen policy backbone
long-horizon robot manipulation reinforcement learning
flow matching action policy robot learning
EOF
}
themes_stream(){
  if [ -n "${LITWATCH_THEMES_FILE:-}" ] && [ -f "${LITWATCH_THEMES_FILE}" ]; then
    cat "$LITWATCH_THEMES_FILE"
  else
    default_themes
  fi
}

staging="$dir/staging.jsonl"       # 可信取数产物;ingest 只读这个
agydir="$dir/agy"                  # agy 的读写沙箱,与可信 staging 隔离(信任边界)
ann="$agydir/annotations.jsonl"
index="$dir/index.jsonl"
drop="$dir/drops.jsonl"
mkdir -p "$agydir"

# 1) 取数(或用预置 staging)
if [ -n "${LITWATCH_PREBUILT_STAGING:-}" ]; then
  cp "$LITWATCH_PREBUILT_STAGING" "$staging" || { log "预置 staging 拷贝失败: $LITWATCH_PREBUILT_STAGING"; exit 1; }
  log "使用预置 staging(跳过取数): $LITWATCH_PREBUILT_STAGING"
else
  : > "$staging"
  while IFS= read -r theme; do
    [ -n "$theme" ] || continue
    for src in $sources; do
      if python3 "$py" fetch --source "$src" --query "$theme" --max "$max" \
           --window-days "$window" --theme "$theme" >> "$staging"; then :; else
        log "取数失败(继续): src=$src theme=$theme"
      fi
      [ "$gap" = "0" ] || sleep "$gap"
    done
  done < <(themes_stream)
fi

# 2) agy 标注(可选,best-effort;走 agy-worker.sh 继承全局冷却闸)
# 信任边界:agy 只在 $agydir 里读写——给它一份 staging 只读拷贝,产物落 $agydir;
# ingest 仍读上层可信 $staging,agy 改烂自己沙箱里的拷贝也进不了 index。
: > "$ann"
if [ "${LITWATCH_NO_AGY:-0}" != "1" ] && [ -s "$staging" ]; then
  cp "$staging" "$agydir/staging.jsonl"
  agy_cmd=${LITWATCH_AGY_CMD:-./agy-worker.sh}
  log "agy 标注: $agy_cmd (AGY_OUT_HINT=$agydir)"
  if AGY_OUT_HINT="$agydir" $agy_cmd "读 roles/litwatch.md,按其执行"; then
    log "agy 标注返回 0"
  else
    log "agy 标注失败(继续,index 仍由确定性核产出)"
  fi
fi

# 3) 准入:标注只能挂到已取 id,越界/坏行/重复丢弃并记 drops
python3 "$py" ingest --staging "$staging" --annotations "$ann" \
  --drop-log "$drop" --out "$index" || { log "ingest 失败"; exit 1; }
n=$(grep -c '' "$index" 2>/dev/null); n=${n:-0}
nd=$(grep -c '' "$drop" 2>/dev/null); nd=${nd:-0}
log "index 就绪: $index ($n 条;丢弃标注 $nd 条,见 $drop)"
