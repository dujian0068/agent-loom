# 第十轮增量审核：节点协议、调度与检查点

**本次发现 4 个需要修复的 P2 缺陷，另有 2 项设计能力仍只完成了一部分。** 审核对象是 `6179c8e → a5ec1bc` 的新增实现，不是再次检查第九轮旧缺陷是否关闭。

复用用户提供的 **1764 passed** 全量结果，没有重跑全量。新增 46 项定向检查，结果为 **34 failed、12 passed、0 errors、0 skipped**；34 个失败实例归属于上述 4 个缺陷，包含内存／文件后端及边界参数，不能理解为 34 个独立缺陷。

本次只增加报告、QA 测试及证据文件，没有修改生产代码、既有测试或 `docs`。所有模型均为本地 ScriptedModel；未使用 LLM Key、真实 MiniMax 或远端模型。

## 1. 基线与审核范围

| 项目 | 核对结果 |
| --- | --- |
| 审核日期 | 2026-10-19，Asia/Shanghai |
| 基准提交 | `6179c8e7678e43f7ecef7275abcffcf1be3740b0` |
| 被审提交 | `a5ec1bcd2b885bad5bfcce745f925be9771d08ed` |
| 源码与设计文档增量 | 38 个文件，2463 行新增、316 行删除 |
| 用户全量结果 | `.agentloom/test-results/20261018-153852-709052Z`，1764 passed，pytest 720.45 秒 |
| 审核开始的有效性检查 | `./scripts/test_all.py --check` 退出码 0；198 个原始清单文件与用户结果完全匹配 |
| 审核结束的完整性 | 上述 198 个文件仍全部匹配；设计文档未变；测试清单仅新增 round10 文件 |
| 本轮定向检查 | 46 项，pytest 12.83 秒，退出码 1 |
| 新增测试静态检查 | Ruff 通过 |

主要审查 `nodes` 的激活／提交握手、continuation 校验、等待恢复和关闭路径；`AgentGraphEngine` 的批次准入、Executor 启动与断点恢复；`checkpoints` 的不可变版本、快照回退及增量重放。并核对调用、控制、存储、总线路由和 Context／Environment Handler 的相关改动。

基线证据：[原始测试摘要](/Users/bmilk/code/project/agent-loom/.agentloom/test-results/20261018-153852-709052Z/summary.json)、[开始时匹配检查](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round10-20261019/baseline-check.txt)、[审核前摘要](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round10-20261019/audit-before.json)、[审核后摘要](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round10-20261019/audit-after.json)。

## 2. 新发现的缺陷

| 编号 | 优先级 | 缺陷 | 失败实例 |
| --- | --- | --- | ---: |
| R10-01 | P2 | Wait 恢复时再次包装 PayloadRef，改变载荷与 hash | 16 |
| R10-02 | P2 | 非法 continuation 可以提交成功，并留下无法接管的状态 | 12 |
| R10-03 | P2 | 生成器关闭异常遮蔽已提交的 Complete，Run 停留在 running | 2 |
| R10-04 | P2 | 已知版本快照引用缺字段／为 null 时，没有回退到有效旧快照 | 4 |

### R10-01：Wait 恢复时重复包装数据引用

**位置：** [nodes/driver.py:64](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/nodes/driver.py:64)。

Wait 提交时保存的是 `PayloadRef.to_dict()`，含 `kind/owner_id/content_hash/value`。下次激活却把整个引用字典作为新 PayloadRef 的 value，导致接收方看到的值多了一层包装，content_hash 也变了。

例如提交的恢复数据是 `{"operation_id":"saved","generation":1}`，恢复后的 `input.resume_input_ref.value` 却是 `{"kind":"inline", ..., "value":{"operation_id":"saved","generation":1}}`。接收方无法按原数据结构读取 operation_id，也不能与原引用进行 hash 一致性校验。

**文档要求：** [runtime.md:126](/Users/bmilk/code/project/agent-loom/docs/sdk/runtime.md:126) 的状态机要求 Wait 提交即满足和等待后唤醒两条路径保留同一恢复引用。

**复现：** 普通对象、null、空对象、空数组、false、0、空字符串，在两种存储中均失败，共 14 项。另外直接观察内置 ReAct 的 model wait，未替换其执行逻辑，也确认恢复引用不等于保存引用，共 2 项失败。

**影响范围：** 内置 ReAct 确实收到错误结构，但当前 AgentLoop 通过持久化 task/operation 查询继续运行，没有读取这份恢复载荷，因此正常回答仍可成功。依赖 NodeInput 恢复信息的节点会读错数据；不能把当前正常回答通过当成此合同已实现。

**建议：** 增加引用反序列化入口，校验 kind、owner 和已保存的 hash，恢复原 value。将“没有恢复引用”与“引用的 value 为 null／空值”分开处理。

用例：[等待边界](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round10.py:63)、[内置 ReAct 观察](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round10.py:108)。

### R10-02：非法 continuation 在提交前未被拒绝

**位置：** [nodes/driver.py:143](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/nodes/driver.py:143)、[nodes/commands.py:79](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/nodes/commands.py:79)。

Driver 只验证引用类型、owner 和 revision，随后直接发布 continuation.value。没有检查 Progress 能否搭配该 phase、协议版本、角色绑定和循环序号等语义。

以下 5 类值在两种后端均收到成功 CommitReceipt：`Progress(phase="completed")`、未知 phase、`protocol_version=2`、其他角色的 role_key、`role_loop_index=-1`。测试节点随后交付合法 Complete 时，整个 Run 甚至返回 succeeded。

**更严重的恢复结果：** 在非法 `Progress(phase="completed")` 已落盘后模拟进程退出，保存状态是 `task.status=running`、`continuation.phase=completed`。重新启动同一 Run 时，恢复校验返回 `PERSISTENCE_INVARIANT_BROKEN`，字段为 `node.terminal`，无法继续。这说明提交入口允许写入恢复入口明确拒绝的状态。

**文档要求：** [runtime.md:169](/Users/bmilk/code/project/agent-loom/docs/sdk/runtime.md:169) 明确要求提交前拒绝越权状态写及 completed phase 搭配 Progress。恢复端的检查已存在于 [recovery.py:257](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/runtime/recovery.py:257)。

**影响范围：** 这是 Node 协议的防错缺口；本轮通过测试节点注入不合法交付验证，没有观察到内置 AgentLoop 自行产生这些值。但节点实现错误本应得到明确任务错误，不应先污染不可变检查点。

**建议：** 在提交任何 effects/continuation 前，统一校验当前协议、角色／轮次绑定、阶段与 outcome 的组合以及引用完整性；非法交付转为 `NODE_OUTCOME_INVALID`，不发布候选 continuation。提交和恢复应共用兼容的语义校验规则。

用例：[非法 Progress 矩阵](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round10.py:150)、[提交后接管](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round10.py:190)。

### R10-03：关闭生成器的异常遮蔽已经提交的成功结果

**位置：** [nodes/driver.py:109](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/nodes/driver.py:109)。

节点 yield Complete 后，Driver 已提交 TaskResult 并准备返回 ActivationReceipt；但 `finally` 中的 `await stream.aclose()` 没有独立异常处理。如果节点自身的 finally 在清理资源时抛出普通异常，该异常会替换已经准备返回的回执，并逃出前面的错误处理分支。

**实际复现：** 节点 Complete 的值为 `durable-output`，随后 finally 抛出 ValueError。内存和文件后端均出现：

```text
handle.wait() -> EXECUTION_DRIVER_FAILED
Run.status   -> running
Task.status  -> succeeded
Task.result  -> durable-output
TaskResult   -> 已保存 1 条
```

这不是输出未提交，而是清理错误让正常收尾被打断。调用方收不到已完成结果，需要再次接管才能尝试推进 Run 收尾。

**文档要求：** [runtime.md:119](/Users/bmilk/code/project/agent-loom/docs/sdk/runtime.md:119) 规定 Driver 返回最后已提交的 receipt；终态 TaskResult 唯一。文档示例也不能覆盖“业务提交成功后，关闭生成器失败”的异常分支。

**建议：** 将生成器清理异常与业务提交异常分别处理。已有终结提交时保留其回执／结果，并记录清理诊断；不要因此重写 TaskResult 或把已完成节点变为待重执行。尚未提交终结结果的异常应按另一个分支处理。

用例：[Complete 后清理失败](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round10.py:245)。

### R10-04：快照格式损坏没有进入旧快照回退

**位置：** [checkpoints/journal.py:243](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/checkpoints/journal.py:243)、[checkpoints/journal.py:253](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/checkpoints/journal.py:253)。

单个候选快照只捕获 `InfrastructureError(CHECKPOINT_CORRUPT)` 并沿 previous_checkpoint_id 回退。其已知版本的 engine_state_ref 缺少 sha256 时抛出 KeyError；engine_state_ref 为 null 时抛出 TypeError。这两种异常绕过候选快照的回退循环，在外层直接转换为 CHECKPOINT_CORRUPT。

**复现前提：** 通过真实运行生成多份快照和完整 ScopeCommit；仅改变最新快照的引用字段，保留版本号、前驱指针、有效旧快照及完整增量。

| 最新快照损坏方式 | 期望 | 内存／文件实际结果 |
| --- | --- | --- |
| 引用的 blob 不存在 | 回退旧快照并重放 | 均通过，作为对照 |
| 引用缺少 sha256 | 回退旧快照并重放 | 均 CHECKPOINT_CORRUPT |
| engine_state_ref=null | 回退旧快照并重放 | 均 CHECKPOINT_CORRUPT |

**文档要求：** [checkpoints.md:355](/Users/bmilk/code/project/agent-loom/docs/sdk/checkpoints.md:355) 要从最近校验通过的快照恢复。这里没有未知协议版本，不应与“未知版本必须拒绝”混为一谈。

**建议：** 在候选快照校验边界统一处理结构错误；当前驱指针有效时尝试旧快照。保留对未知版本、断裂增量和实际不可恢复损坏的拒绝，不跳过 ScopeCommit。本轮没有把文件存储底层任意字节损坏都能恢复作为通过标准。

用例：[快照回退矩阵](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round10.py:307)。

## 3. 本次增量仍未完整实现的设计能力

以下是代码与设计合同的静态差异，不计入上面的 4 个复现缺陷，也不等于正常功能已经失败。

| 编号 | 已实现部分 | 尚未完整实现部分与证据 |
| --- | --- | --- |
| G10-01 | task ready/wait、Operation／批次完成后的持久唤醒 | 内置 AgentLoop 没有产出 budget_available Wait；预算不足仍在 [calls.py:280](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/runtime/calls.py:280) 和 _permit 中每 5ms 轮询。没有 [runtime.md:357](/Users/bmilk/code/project/agent-loom/docs/sdk/runtime.md:357) 所述持久预算申请序及 FIFO 唤醒；Executor 等待也仍由 [agent_engine.py:256](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/runtime/agent_engine.py:256) 轮询。当前已释放 NodeDriver 激活，但尚未完整实现文档的等待协议。 |
| G10-02 | 节点 Activate／Progress／Wait／Complete／Fail 已用 RuntimeCommit | 其他关键变更仍通过字符串 command + 任意 mutation 回调进入 [storage/commit.py:42](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/storage/commit.py:42)，如 [agent_engine.py:254](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/runtime/agent_engine.py:254) 的 StartExecutor。尚未达到 [checkpoints.md:224](/Users/bmilk/code/project/agent-loom/docs/sdk/checkpoints.md:224) 所述统一结构化命令、语义 fingerprint 和 expected revisions 合同。本轮 StartExecutor 原子恢复测试通过，因此这里不据此推断有重复子任务缺陷。 |

此前已声明的独立 BlobStore／Artifact 授权、完整 outbox／订阅 ACK、生命周期 Hook、完整查询 DTO、多后端环境等缺口仍见 [implementation.md:495](/Users/bmilk/code/project/agent-loom/docs/sdk/implementation.md:495)。它们没有因这次增加节点协议而全部补齐；也不能继续把“没有持久节点、批次和增量日志”当成当前事实。Workflow 继续按原范围排除，本轮没有以新增默认工具为验收要求。

性能方面，上一份[修复验证报告](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/REVIEW_RESULTS_2026-10-18_ROUND9_FIX_VERIFICATION.md) 已记录 1665 项旧用例在新基线中的耗时上升。本轮没有做同环境性能剖析，不把日志耗时差直接归因为这次调度／日志实现的性能缺陷。

## 4. 通过的定向检查与覆盖边界

**12 项通过：** 最新快照 blob 丢失后回退，2 个后端共 2 项；真实 Plan 节点正常执行以及在 admit、delegate-start、子任务 validate、consume 提交后中断再接续，2 个后端共 10 项。

Plan 场景包含两个 Executor、并行模式和 `executor_max_parallel=1`。接续后均成功，两个本地工具各执行一次；最终只有 3 个 task／TaskResult，所有批次已消费、所有 admission token 已释放、loops_reserved=0。这些是本轮范围内的正向对照，不代表覆盖所有并发交错或真实进程硬杀场景。

现有 `test_node_protocol.py` 已测引用类型、外层 owner、缺失终态和正常握手，但没有检查恢复引用的往返内容、continuation 内部字段或 aclose 抛错。现有 `test_checkpoint_protocol.py` 已测丢失 blob 回退，没有覆盖已解析快照引用的缺字段／null。因此全量 1764 通过与本轮新缺陷并不矛盾。

## 5. 文件与修复验证

- [新增 QA 测试](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round10.py)
- [本轮汇总 JSON](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round10-20261019/summary.json)
- [最终定向运行日志](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round10-20261019/pytest.log)
- [最终 JUnit](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round10-20261019/junit.xml)
- [命令、时间及测试文件 hash](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round10-20261019/run.json)

修复后可在仓库根目录先运行这一个文件：

```bash
.venv/bin/python -m pytest -c agentloom-sdk/pyproject.toml agentloom-sdk/qa/test_sdk_review_round10.py -q -ra --junitxml=/tmp/agentloom-round10-fix.xml
```

这些测试断言预期正确行为，没有 xfail，也没有修改生产实现来配合断言。新增文件会被统一全量脚本自动收集，修复前会使全量命令报告失败。原始 1764 项证据没有被覆盖；新增测试后 `--check` 会因测试集合增加而不再确认“当前全部测试已运行”，这与原 198 个文件仍全部匹配是两件事。
