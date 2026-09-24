# 运行时对抗复核

分类、最低票聚合、自动资格收敛、无效评审拒绝和旧聚合重放均通过定向检查。仍有一项执行入口与“旧格式仅用于归档读取”的约定不一致。

## 必须修正：公开执行入口仍可运行旧评审计划

位置：`lib/history_runtime.py` 的 `run_review_matrix`、`_run_review_matrix` 和 `verify_round_review_plan`。

`seal_round_review_plan` 总是创建 schema 3，并且 `hunt.sh` 固定消费唯一当前契约，这两项正确。但公开 `run-review-matrix --review-plan ...` 接受 schema 2 旧计划；`run_review_matrix` 没有限制计划必须使用当前 schema。后续 `_run_review_matrix` 对 schema 2 不挂载 `review_protocol.json`，`portable_stage._project_outputs` 因此选用旧输出解析器并实际启动 reviewer。

可复现依据：`tests/review_protocol_runtime_smoke.py` 的 legacy 场景和独立攻击脚本的 `legacy_reject_replay`、`legacy_sa_replay` 均由旧 schema 2 计划重新执行 review stage，再建立和验证旧 aggregation；它们不是只读旧输出。公开入口与测试入口都调用同一 `_run_review_matrix`，公开包装函数没有版本限制。

影响：普通 hunt 新轮始终使用当前契约，未发现它自动降级；但“历史格式处理仅限归档读取”的约束尚未在公开执行入口落实。持有旧 sealed plan 的调用者仍能重新启动旧评审流程。

修正：公开 `run_review_matrix` 应在启动任何 provider 前拒绝非当前 schema 的计划；`verify_round_review_plan`、`verify_review_matrix`、`verify_round_aggregation` 保留旧格式读取和验证。测试夹具可以保留受测试 authority 限定的旧产物构造路径。回归应检查旧 plan 的公开执行被拒绝且 reviewer 调用数为零，同时旧输出及 aggregation 仍可重放。

## 已验证的运行结果

| 情形 | verdict | category | near-SA observation |
|---|---|---|---|
| 一席 covered/Reject，另一席 not-covered/SA | Reject | review-unresolved | 0 |
| 一席 AwR、一席 SA，overlap=high | AwR | ceiling-limited | 0 |
| 一席 AwR、一席 SA，overlap=low | AwR | design-fixable | 1，符合旧资格 |
| 全部 AwR，overlap=low | AwR | design-fixable | 0 |
| 普通 Reject，未证明完全覆盖 | Reject | review-unresolved | 0；入库后无 generation parent |
| 全员 SA 被机械证据门槛降级 | Reject | evidence-incomplete | 仅按旧资格产生 |
| 当前协议缺少 Assessment | 无有效票 | 无新 canonical row | 0 |
| 旧协议普通 Reject/low | Reject | novelty-dead | 保持旧结果 |

覆盖冲突改变归档 category，不提高最低票 verdict。一个 supported covered 与 unknown 同时出现时，unknown 按事实弃权处理；covered 与 not-covered 或任一 disputed 同时出现时，category 为 review-unresolved。

`eligible_for_reentry` 同时要求旧 category 和新 category 都属于旧可重审集合，并保留 supporting SA vote 与 story count 限制。独立穷举覆盖 41,472 个 votes、overlap、机械门槛、story count 和 coverage 组合，未发现新 eligible 超出旧 eligible；普通 Reject 在全部组合下均未获得自动资格。

## 无效输出攻击

下列 15 类输出均被拒绝：缺 Assessment、原始 CR、NUL、重复 JSON key、额外 key、缺 key、重复 cause、错误 cause 类型、伪造 quote、covered 引文无 URL、covered 却判 SA、covered 却判 AwR、CRITICAL 却判 AwR、两个 MAJOR 却判 SA、字段超长。

另有 5 类 protocol 攻击均被拒绝：旧版本值、浮点版本值、额外字段、非规范字节、重复 key。篡改 aggregation 版本并重新计算 hash、修改 frozen protocol，也均不能通过已有端到端校验。

covered 必须同时包含 candidate 和 prior-work 的原文 quote，且 prior-work quote 内必须存在该冻结记录中的 HTTP(S) URL。该检查证明来源一致性，科学蕴含关系仍由独立 reviewer 判断；此实现没有声称字符串匹配能够证明占用事实。

## 旧记录重放与唯一当前配置

旧 schema 2 计划仍选用 12 行解析器和旧 aggregation 分类。独立脚本从 `git show HEAD:lib/history_runtime.py` 提取修改前 `_aggregation_material` 与 `_round_aggregation_hash`，在同一旧格式夹具上比较其结果与当前兼容分支。Reject 和 SA 两个场景的 canonical aggregation 字节及 hash 完全一致。

新 `seal_round_review_plan` 没有 review-version 参数，固定创建 schema 3 和当前 protocol；`hunt.sh` 固定使用 `history/review-contract.md`。CLI 没有新增公开版本切换 flag。上述公开执行入口接受旧 plan 是仍需修复的差异。

## 检查产物

- `python3 tests/review_assessment_v2_smoke.py`：6 项通过。
- `python3 tests/review_protocol_runtime_smoke.py`：6 项通过。
- `/tmp/judge-revision/runtime_attack.py`：41,472 个资格组合、20 类格式/协议攻击、7 个端到端场景通过。
- `/tmp/judge-revision/runtime-attack-results.json` 保存逐项结果。

这些检查验证协议和运行结果，未评价模型是否能正确判断科研贡献。生产文件未发生审查侧修改。

## 公开执行入口修复复核

`run_review_matrix` 已在 provider/profile 前置校验后要求严格整数 schema 3；静态 schema 2 计划在创建 stage 和 index 前被拒绝。旧计划的只读 verify 继续可用，CLI 未添加版本切换 flag。`tests/review_protocol_runtime_smoke.py` 的 9 项测试通过。

`trigger.md` 的两处摘要已同步：research 只报告覆盖内容和剩余差异，由 reviewer 判断价值；可行性同时评价最小验证和合理首篇范围。supports 来源本身已经占用假设移除贡献时，也须报告该事实。

仍有一个可复现的读取次序缺口：公开入口检查的 plan 快照与内部执行的 plan 快照不同。`run_review_matrix` 先调用 `_load_canonical_json` 检查 schema 3，随后 `_run_review_matrix` 再调用 `verify_round_review_plan` 读取同一路径。后一次校验合法地支持历史 schema 2，却没有再次执行当前版本限制。

临时攻击以一份合法的当前 schema 3 计划为起点，在第一次读取返回后，将同一路径替换为 schema 2、删除 review_protocol 字段并重新计算 plan hash。随后公开调用继续执行。结果如下：

```json
{"first_schema":[3],"provider_calls":1,"stage_created":true,"current_schema":2}
```

该攻击需要在两次读取之间改写宿主计划文件；它没有证明普通稳定文件会自动降级。它证明当前版本检查尚未绑定到实际执行的那份已验证计划。修正应在 `verify_round_review_plan` 返回后、`_mkdir_single_use` 及任何 provider 调用前，针对该返回值检查生产执行必须使用当前 schema。私有历史夹具可使用仅限内部测试的执行路径；公开 API 和 CLI 无须增加版本参数。回归应在两次读取间替换计划，并断言 provider 调用数仍为零且无 stage/index。

## 执行对象约束验证

旧计划执行问题和读取次序问题均已修复。生产路径在 `verify_round_review_plan` 返回后检查该对象为当前 schema，再把同一对象传给 `_execute_verified_review_matrix`。执行器不再重读 plan。公开 API 和 CLI 没有增加版本选择参数；公共调用即使位于全局测试上下文，也必须使用当前 schema。旧夹具只通过既有 authority 和临时路径检查后的私有测试入口构建，旧验证及重放继续可用。

`python3 tests/review_protocol_runtime_smoke.py` 的 10 项测试通过。其中静态旧 plan 和验证前被替换为旧 schema 的 plan 均在 provider 启动前被拒绝；provider 调用数为零，stage/index 均未创建。

原始“第一次读取后替换文件”的独立攻击再次执行，得到：

```json
{"verified_snapshot_schema":[3],"launched_with_current_protocol":[true],"later_path_schema":2,"legacy_execution":false}
```

该结果说明实际执行绑定到已验证的当前对象，后续路径变化未使其切换为旧格式。主路径固定使用唯一当前契约；trigger 的查重事实职责和首篇可行性范围也已同步。上述运行时审查范围内未发现尚待修复的问题。
