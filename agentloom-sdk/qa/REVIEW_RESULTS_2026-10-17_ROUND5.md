# SDK 第五轮复审结果

审核日期：2026-10-17。审核版本：`bfe582a`（`fix(sdk): harden completion and recovery boundaries`）。范围：SDK，排除 Workflow、默认工具及其 SPI、Platform。

**结论：第四轮两类缺陷的 56 项原复现及对照用例全部通过；扩展测试仍发现 2 类问题：P1 × 1、P2 × 1。优先处理 JSON 嵌套深度与持久化读写不一致的问题：部分结果已保存为 completed，但后续推进和重启接续均失败，会话继续被占用。**

本次独立运行既有完整套件，并新增边界及恢复测试。测试前后 141 个源码、既有测试、SDK 文档及包配置文件的摘要一致，Git HEAD 未变化；新增测试另行记录摘要。本轮只新增 QA 测试、报告及证据，没有修改生产代码，没有暂存或提交文件。

交付前工作区另出现 `agentloom-sdk/tests/test_review_whitebox_probe.py`。它不在本轮测试快照中，未计入下述 948 项回归或 Ruff 结果；既有源码和测试未变化。后续工作区增量另记于[交付摘要](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round5-20261017/source-delivery.json)。

## 1. 验证结果

| 检查 | 结果 | 范围 |
| --- | --- | --- |
| 既有完整回归 | **948 通过，0 失败**，265.06 秒 | SDK 714 项、首轮 QA 110 项、第二轮 24 项、第三轮 44 项、第四轮 56 项 |
| 第五轮独立边界测试 | **44 通过，28 失败**，6.36 秒 | 72 项；内存／文件后端、4 种结果路径、6 个嵌套深度、重启接续、错误字段及回调对照 |
| Ruff lint / format | 通过 | src、tests 与五轮 QA，共 124 个 Python 文件 |
| 审核期间文件一致性 | 通过 | 141 个既有文件摘要未变化 |

最终两组共 **1020 项：992 通过、28 失败**。28 个失败按根因归为以下两类，不是 28 个独立缺陷；探索过程和早期测试版本的执行次数未重复计入。

环境：Python 3.12.14、macOS 26.7 arm64、pytest 8.4.2；Python 默认递归限制为 1000，测试没有修改该限制。所有新用例使用本地确定性适配器。

## 2. R5-01 P1 嵌套结果通过校验后仍可中断持久化和恢复

**位置：** [JSON 校验边界](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/configuration/json_values.py:64)、[结果归一入口](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/capabilities/invocation.py:124)、[Attempt 深复制](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/runtime/calls.py:490)、[状态读取](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/storage/commit.py:52)。

本次修复能识别循环引用，并捕获 JSON 校验、复制、冻结函数内部的 RecursionError；但其接受范围取决于当前调用栈，没有保证接受的数据还能完成后续状态复制、读取和恢复。

### 2.1 已保存的 completed 结果使后续推进失败

构造没有循环引用的嵌套列表作为正常业务返回值：

```python
value = None
for _ in range(465):
    value = [value]
return value
```

该值是合法 JSON，紧凑表示仅 934 字节。在当前 pytest 环境下，FunctionToolAdapter、旧 invoke 兼容桥、原生 invoke_once JSON 信封、report_completion JSON 信封四条路径均能接受并保存结果，但后续阶段读取运行状态时触发 `copy.deepcopy` 的 RecursionError。

双后端、四条路径均复现，共 8 个失败：

```text
Run.status = running
Operation.raw.status = succeeded
Operation.raw.execution_fact = completed
RunHandle.wait() -> InfrastructureError(EXECUTION_DRIVER_FAILED)
工具调用次数 = 1
```

再以相同 run_id 重建 RuntimeEngine 接续，函数工具和原生工具在双后端均再次失败，原 Session 新问题返回 `SESSION_BUSY`，共 4 个恢复失败用例。工具没有再次执行，但已保存结果未能让运行继续完成。

独立脚本也复现了这一链路，并保存了从后置阶段到存储读取、再到 deepcopy 的异常栈，见[恢复探针日志](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round5-20261017/recovery-probe.txt)。独立脚本在 480 层复现，pytest 用例在 465 层复现；具体临界点随调用栈变化，这两个数值不是稳定的 SDK 支持上限。

### 2.2 更深的原生结果在提交前失败并丢失完成事实

对 480、600 层的原生 JSON 信封和完成回调信封，JSON 归一仍可通过，但 Attempt 提交中的深复制失败。双后端共 8 个失败：

| 路径 | 实际行为 |
| --- | --- |
| invoke_once 正常返回信封 | EXECUTION_DRIVER_FAILED，Run 保持 running，raw=null |
| report_completion 报告信封 | ATTEMPT_COMPLETION_COMMIT_FAILED，Run 保持 running，raw=null |

这不是存储服务中断，而是适配器数据超过后续处理的安全深度。工具已经返回，结果却没有成为可恢复的完成事实。

**正常对照：** 深度 0 的 null、400 层数据正常交付；1010 层数据在结果边界被拒绝为 completed 的协议失败，Run 能继续结束。函数／旧兼容桥在 pytest 的 480、600 层场景中也提前拒绝并正常收束。这些对照说明单纯增加一个“超过递归上限”的测试不足以覆盖中间危险区间。

**修复验收要求：** 为可持久化 JSON 建立明确且一致的深度约束，或使整条读写链路支持同样的深度。不能只依赖当前栈是否刚好溢出。无法安全处理的结果，应在首次写入前转为协议失败并保留 completed 事实；审计中的非法原始信封也须满足同一约束。已接受的结果必须支持后置处理、重启接续和会话释放。

本项共 **20 个失败用例**。这是上轮循环／递归修复的剩余边界，不认定为该提交新引入的回归。

测试入口：[深度矩阵](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round5.py:124)、[重启接续与会话释放](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round5.py:169)。

## 3. R5-02 P2 直接抛出的非法错误对象绕过字段校验

**位置：** [异常捕获路径](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/runtime/calls.py:427)、[failed_result](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/runtime/completion.py:21)、[error_record](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/errors.py:63)。

第四轮的 `context.report_completion(error=...)` 已进入统一结果校验。但适配器直接 `raise AgentLoomError(...)` 时，仍通过 `error_record()` 原样转换字典，没有验证错误字段，然后交给 Attempt 保存。

例如：

```python
async def invoke_once(self, request, context):
    raise AgentLoomError("FAILED", retry_after_at=float("nan"))
```

相同错误分别通过直接抛出和 error 回调报告，结果如下：

| 非法字段 | 直接 raise 的实际行为 | error 回调对照 |
| --- | --- | --- |
| retry_after_at=NaN | **整个 Run 变为 failed，错误为 INVALID_JSON；工具 raw=null** | CAPABILITY_RESULT_INVALID / protocol / completed，模型能继续并结束 Run |
| category="outside-enum" | 未声明的枚举原样保存到 Operation.raw.error | 在提交前拒绝 |
| code=""，导致 message 也为空 | 空错误码和空消息原样保存 | 在提交前拒绝 |
| retryable_transport=1 | 整数原样保存为应当是布尔值的字段 | 在提交前拒绝 |

双后端共 **8 个失败用例**；相同非法字段的回调对照及合法错误对照共 12 项通过。NaN 场景是整个运行被错误终止，与 R5-01 的 running／会话占用现象不同。

依据[OperationError 合同](/Users/bmilk/code/project/agent-loom/docs/sdk/event-bus.md:148)及[适配器异常应转为结构化错误的规则](/Users/bmilk/code/project/agent-loom/docs/sdk/event-bus.md:470)，错误 DTO 不应因上报方式不同而绕过验证。非法业务错误应成为安全的工具失败，让模型按正常后处理流程接收错误，不能污染错误记录或把 JSON 序列化错误升级为整个 Run 的失败。

**修复验收要求：** 在直接抛出异常的记录边界验证错误 DTO 和规范 JSON，生成安全错误；保持真实基础设施异常和控制信号的传播。字段校验与执行事实判断应分别处理：本项不要求把执行中抛出的异常记为 completed，正常直接抛错的 unknown 事实对照仍须保留。

测试入口：[错误字段和两种上报方式](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round5.py:213)。

## 4. 上轮修复确认

| 上轮项目 | 本轮结果 | 剩余扩展边界 |
| --- | --- | --- |
| R4-01 完成回调的错误归一、重试与非法 DTO | 原 38 项全部通过，含恢复对照 | 回调路径修复有效；直接 raise 的非法错误见 R5-02 |
| R4-02 循环容器丢失完成事实 | 原 18 项全部通过，含 NaN 对照 | 循环检测有效；无循环的中间嵌套深度见 R5-01 |
| 首轮、第二轮、第三轮 QA | 178/178 通过 | 原覆盖场景未复现失败 |
| 本次新增的 SDK 测试 | 随完整套件通过 | 包含错误字段、共享子对象、超深值、执行中 RecursionError、失权、回调过期及进程恢复等对照 |

本次没有发现既有测试回归。新发现涉及原测试没有覆盖的入口或深度区间，不能将其描述成第四轮原用例仍未修复。

## 5. 仍未完成的设计功能

本次源码变化集中于完成结果和 JSON 边界，以下目标差异仍未补齐。它们单独跟踪，不计入上述两类运行缺陷；依据[当前实现边界](/Users/bmilk/code/project/agent-loom/docs/sdk/implementation.md)及相应模块与第四轮快照的比较。

| 编号 | 尚未完成 |
| --- | --- |
| GAP-01 | 独立 ContentStore／BlobStore、ArtifactRef 输入、来源撤销、显式外部结果授权 |
| GAP-02 | 完整可靠 EventBus、outbox／订阅 ACK、完整持久等待与准入记录、通用原生 Node 扩展端口 |
| GAP-03 | 全部 Context／Environment 事件路由、生命周期修改 Hook、ScriptHookHandler |
| GAP-04 | 完整机器 Schema、全部查询／内容授权 DTO、OutputValidation／ValidationIssue；已实现的部分 DTO 不等于完整目标合同 |
| GAP-05 | MCP resources／templates／prompts、OAuth 凭据生命周期和完整连接分区合同 |
| GAP-06 | OCI 隔离、分布式环境、多机 writer 撤权、MCP 受限工作区挂载／短期授权令牌 |
| GAP-07 | 增量日志／索引布局、历史格式迁移、handler 实现与客户端服务地址的完整恢复指纹 |

Workflow、默认工具及其 SPI、Platform 不纳入本轮验收；真实付费模型、远程 MCP、跨机协调、断电与规模性能未执行验证。

## 6. 复测命令和证据

从仓库根目录运行：

```sh
.venv/bin/python -m pytest -q agentloom-sdk/qa/test_sdk_review_round5.py
.venv/bin/python -m pytest -q agentloom-sdk/tests agentloom-sdk/qa/test_sdk_audit.py agentloom-sdk/qa/test_sdk_rereview.py agentloom-sdk/qa/test_sdk_review_round3.py agentloom-sdk/qa/test_sdk_review_round4.py
```

新增 QA 需显式运行。没有将失败断言标记为 xfail 或跳过；日志包含参数化用例名、run_id、operation_id 及关键状态。

- [第五轮测试文件](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round5.py)
- [948 项完整回归日志](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round5-20261017/regression.txt)
- [72 项新增边界测试日志](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round5-20261017/boundaries.txt)
- [新增测试 JUnit 结果](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round5-20261017/boundaries.junit.xml)
- [版本、环境、命令、用例和源码摘要](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round5-20261017/summary.json)
- [独立恢复探针源码](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round5-20261017/recovery-probe-source.txt)
- [第四轮报告，保留作修复对照](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/REVIEW_RESULTS_2026-10-17_ROUND4.md)
