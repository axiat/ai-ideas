# Backends

Backend commands are environment-variable command strings. Each runner appends its prompt as the final argument. Quote the complete command when setting an override.

## Hunt

The default assignment in `hunt.sh` is:

```bash
AGENT_CMD=${AGENT_CMD:-codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write}
```

`FRONT_CMD` and `BACK_CMD` fall back to `AGENT_CMD`. `REV_CMD_1` through `REV_CMD_N` override individual review seats and otherwise fall back to `BACK_CMD`.

```bash
AGENT_CMD='./grok-worker.sh' ./hunt.sh

FRONT_CMD='./agy-worker.sh' \
BACK_CMD='codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write' \
./hunt.sh

REV_CMD_1='codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write' \
REV_CMD_2='./grok-worker.sh' \
REV_CMD_3='./agy-worker.sh' \
./hunt.sh
```

`./agy-worker.sh` and the AwR built-in adapter default to `AGY_MODEL=gemini-3.6-flash-high`. Overrides use the complete model ID printed by `agy models`.

Claude is available only through an explicit command supplied for the current run:

```bash
AGENT_CMD='claude -p --strict-mcp-config' ./hunt.sh
```

No default or fallback selects Claude.

## AwR Sidecar

The default assignment in `awr-side.sh` is:

```bash
SIDE_CMD=${SIDE_CMD:-codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write --skip-git-repo-check --ephemeral}
```

`SIDE_RESEARCH_CMD` and `SIDE_JUDGE_CMD` fall back to `SIDE_CMD`; `SIDE_PRIORWORK_CMD` falls back to `SIDE_JUDGE_CMD`. `SIDE_CMD=agy` selects the mirror-local adapter explicitly.

```bash
SIDE_CMD=agy SIDE_POLL_SEC=0 ./awr-side.sh
SIDE_JUDGE_CMD='./grok-worker.sh' SIDE_POLL_SEC=0 ./awr-side.sh
```

Operational defaults are `SIDE_POLL_SEC=9000`, `SIDE_MAX_BAD=3`, `SIDE_MAX_ROUNDS=3`, `SIDE_GAP_MIN_SEC=60`, `SIDE_GAP_MAX_SEC=600`, `SIDE_GAP_SEC=120` for the built-in agy adapter, and `SIDE_COOLDOWN_SEC=3600` after three consecutive no-artifact calls.

## Literature Monitor

The default assignment in `litwatch.sh` is:

```bash
LITWATCH_CMD=${LITWATCH_CMD:-codex -c approval_policy=never exec -s workspace-write --skip-git-repo-check --ephemeral}
```

`LITWATCH_CMD` controls optional annotation. `LITWATCH_AGY_CMD` is a compatibility override consulted only when `LITWATCH_CMD` was unset; an explicitly set neutral variable always wins. `LITWATCH_NO_AGY=1` skips annotation while deterministic ingest still runs.

```bash
LITWATCH_CMD='./agy-worker.sh' ./litwatch.sh
LITWATCH_NO_AGY=1 ./litwatch.sh
```

OAI harvesting defaults to the last `LITWATCH_OAI_DAYS=4` days and at most `LITWATCH_OAI_MAXPAGES=8` pages. The default source is OAI; arXiv and Semantic Scholar are explicit `LITWATCH_SOURCES` selections.

## Calibration

Frozen panels and the all-case runner share this retrieval-disabled default:

```bash
PANEL_CMD=${PANEL_CMD:-codex -c approval_policy=never exec -s workspace-write --skip-git-repo-check --ephemeral}
```

End-to-end retrieval calibration uses:

```bash
E2E_CMD=${E2E_CMD:-codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write --skip-git-repo-check --ephemeral}
```

```bash
PANEL_CMD='./grok-worker.sh' ./calib/run_panel.sh calib/cases/pos-axiom-torque
E2E_CMD='./grok-worker.sh' ./calib/run_e2e.sh calib/cases/neg-replai
```

Frozen reviewers must not retrieve because published source papers can invalidate reconstructed positive controls. `run_e2e.sh` intentionally enables retrieval and additionally requires neighbor-link and structured API-query density. Neither runner proves that an arbitrary custom backend actually used or avoided the network; conclusions inherit the configured backend's behavior.
