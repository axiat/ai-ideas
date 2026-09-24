# Reviewer harness 独立验证

核验对象为 `/Users/qinningxu/code/ai-ideas` 当前 reviewer/harness 修改及其校准。验证者未实现生产修改，仅读取源码与证据、运行离线测试，并在 `/tmp/judge-revision/` 保存验证材料。本次没有调用真实 provider、运行 live hunt、修改生产历史或追加模型评测。此前 TPAMI 文献任务不在本次范围内。

## Checklist

1. 新运行采用唯一 active review contract，不提供 `--review-output-version` 或公开旧规则执行入口。
2. policy、rubric、review/research/AwR/select/prescreen 及入口说明使用一致的科学判据；保留合理 Reject，并允许有依据的初步研究、有限修订和无 repair 的知识贡献。
3. Assessment 使用严格结构和冻结引文；无效字段不能成为中性票，coverage 冲突不能提升最低票结果。
4. 历史格式保持只读验证和精确回放；新执行在启动 provider 前校验当前协议，执行同一份已验证对象。
5. 最低票和原自动再进入条件保留；相同 votes 与事实下，新资格集合不扩大。
6. 完成独立对抗评审和实际校准，明确原 60 calls 与最终规则下另行 6 calls 的分母、有效性及科学理由。
7. 保留冻结材料和生产历史，归档中的计数、哈希和报告结论与实际文件相符。
8. 按仓库规范完成必要离线回归，并报告验证能力的实际限制。

## Action Trace

| 要求 | 已执行核验与结果 |
|---|---|
| 1 | 读取实际 active contract、公开 shell/CLI/API 和调用链；搜索旧 contract 与版本参数。唯一 active 文件为 `history/review-contract.md`，旧文本位于测试 fixture。最终科学 contract SHA-256 为 `52c94b98fe9f09f407b255832d8fcdfb846e9f06dd29770d2ed4b60b1f33dffe`。 |
| 2 | 独立审查 policy/rubric/roles/trigger/PROGRAM 的实际 diff。此前发现的机械维度否决、对非因果贡献强加因果要求、排除可核查推导、prescreen 只看已知 headline 等问题已经修正。最终文本按完整决定性贡献判定 occupation；两个独立缺陷不能合并；AwR 需要保留价值和有限修订；未运行实验本身不降级。 |
| 3 | 读取 `review_assessment.py`、`stage_contract.py` 和 host projection/aggregation。引文必须来自冻结 candidate/prior-work；covered 需两类来源及 prior-work URL。结构约束与科学支持分别检验；精确引文只证明出处，不能自动证明结论正确。 |
| 4 | 此前发现并验证修复了 public old-plan execution 和先读后验证的对象替换问题。本次又用 mock 确认底层 `portable_stage.run_stage` 缺协议时可到达 provider；已由实现代理修复。最终 public API/CLI 在启动前强制当前 canonical protocol，单次加载的输入对象交给执行体，历史只读验证保持可用。 |
| 5 | 核对原最低票、MAJOR 和机械证据 gate，再核对 category 与 eligibility。此前独立攻击程序检查 41,472 种组合，新资格均为旧资格子集；coverage 冲突保留 Reject，ordinary Reject 的 `review-unresolved` 不授予自动资格。 |
| 6 | 已逐票审阅全部 59 份原有效评审和 6 份 followup 评审，校验原始 projection 并核对 aggregate。原 A 为 14 R、11 AwR、5 SA；原 B 为 15 R、9 AwR、5 SA、1 invalid。最终 C 的 025/028 共 6 票全部 AwR、0 CRITICAL、1 MAJOR。没有重试或替换无效票。 |
| 7 | 独立归档子审查核对原 copied receipts、旧清单重建、followup 复制和所有 66 cell receipts。本次收尾重新验证当前 766 项清单全部匹配，并直接读取生产 DB/WAL/SHM、两个 ledger；五项哈希与校准结束记录和本次验证开始时均相同。 |
| 8 | 读取 `CONTRIBUTING.md` 和 README；相关仓库路径无额外 AGENTS/CLAUDE 规则。直接运行下表离线测试、产品契约、shell 语法和 diff 检查。没有执行会启动真实后端的 calibration 或 hunt 命令。 |

## Diff Summary / Code Scope

读取了 `git diff`、`git diff --cached`、`git log --oneline -3` 和 `git diff HEAD~1..HEAD`，将当前未提交修改与前一个既有 ledger/contract 提交区分。当前生产改动包括单一 review contract、policy/rubric/roles 和入口说明、Assessment parser、review-plan/aggregation 协议绑定、portable projection/执行校验，以及对应 fixtures 和 smoke tests。没有要求或执行 commit/push。

重点源码为 `lib/review_assessment.py`、`lib/history_runtime.py`、`lib/portable_stage.py`、`lib/stage_contract.py`、`lib/history_budget.py` 和 `hunt.sh`。最后的入口修复只增加新调用的协议校验；旧票和冻结运行材料没有改变，也没有据此重新运行模型。

## Evaluation

- **Correctness:** 当前协议严格校验键、枚举、整数类型、重复键、引文和 verdict/coverage/causes 一致性。缺失或畸形协议在公开执行路径启动前失败；历史 replay 保持有效。必要离线测试全部通过。
- **Adequacy:** 已完成规则修订、实现、独立对抗和实际票据复核。占用明确的 probe009 在两臂均保留 Reject。原 B028 的 SA 提升没有被当作改善：其遗漏了 identity-blind double counting 可无交互 signature 的反例。最终通用 falsification 条款区分科学必要预测与项目停止阈值，C028 三票均识别该缺口；C025 三票指出 pending/correlation 总存储未被 event-identifier 论证覆盖，并给出有限修订条件。未发现这些六票因为实验未完成或没有无关 repair 而扣级。
- **Excess:** 改动围绕审稿判据和机器协议完成。历史兼容属于必要只读能力，没有形成公开版本选择；最后 guard 没有引入公开模式参数或测试上下文豁免。没有更改最低票规则或扩张预算资格。
- **Edge Cases:** 覆盖普通 Reject 的中性 category、跨席 coverage 反证、严格 JSON/数值类型、缺失引文、伪造引文、旧 plan 执行、对象替换、冻结源哈希变化、旧 aggregation 字节回放，以及低层公开 CLI 缺协议入口。MAJOR 数量与 cause 类型数量独立。

校准只支持对这些具体理由和规则行为的判断。原 A 已含先前的未运行实验修正；原 B 与最终 C 分开报告。026–028 的合成预期存在输入限制，不能作为必然正确的标签。报告没有宣称普遍误拒率降低、TPAMI 接收概率、完整 AC 综合审稿等价性、live prescreen 效果或实际数据库/sidecar 再提交。模型身份仅记录 CLI 请求 `gpt-6-astra/xhigh`，没有将未认证 registry 表述为服务端身份认证。

归档的 frozen `baseline/rubric.md:1097` 保留一个缺少目标的旧 handbook 相对链接，主报告已经披露；该文件仍与原 receipt 逐字节相同。其余已核查的报告相对链接有效。该既有冻结引用缺失不影响本次机器校验和实际票据核验，不需要修改冻结证据。

## Build & Test Results

本项目使用 Python/Bash，没有另一个编译构建步骤。以下命令由本验证者在最后修改上执行，日志位于 `/tmp/judge-revision/check-work-tests/`。

| 精确命令 | 结果 |
|---|---|
| `python3 tests/review_assessment_v2_smoke.py` | exit 0；6 tests，OK |
| `python3 tests/history_budget_smoke.py` | exit 0；11 tests，OK |
| `python3 tests/review_protocol_runtime_smoke.py` | exit 0；10 tests，OK |
| `python3 tests/portable_stage_runtime_smoke.py` | exit 0；17 tests，OK |
| `python3 tests/history_runtime_smoke.py` | exit 0；102 tests，OK |
| `python3 tests/verify_product_contract.py all` | exit 0；`ok: all` |
| `bash -n hunt.sh` | exit 0 |
| `git diff --check` | exit 0 |

独立 CLI 攻击使用实际 `portable_stage.main`，只 mock provider resolution 和启动函数。missing、empty、v1、noncanonical、numeric-alias protocol 五种输入均 exit 2，报 `invalid_review_protocol`，启动调用数为 0，未生成 output；当前协议唯一到达被 mock 的启动点，实际 provider 执行数仍为 0。结果保存于 `check-work-tests/portable-cli-guard.json`，修复前的反例保存于 `portable-missing-protocol.json`。

实现代理另报告最后的离线 Hunt/AwR E2E、runtime ABI 和 diff-check 三项 exit 0，记录位于 `/tmp/judge-revision/checks/portable-review-guard-checks.json`。这些执行结果由实现代理提供，与上述独立执行结果分别记录。

本次核验时的归档清单有 766 项，全部匹配，SHA-256 为 `42a1eae15620dd2b834278e05ef0e63eecd78f328c3704c919b26504e23909cf`。原 662 项清单由 receipts 重建后的 SHA-256 精确匹配早期摘要；599 项原 A/B 保护记录、657 项原复制记录和 96 项 followup 复制记录全部一致。根代理随后将本报告加入归档时，需要按既有流程更新清单；这不改动已核验的冻结票据。

VERDICT: PASS
