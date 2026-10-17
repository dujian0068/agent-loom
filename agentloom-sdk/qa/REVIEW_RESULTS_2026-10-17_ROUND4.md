# SDK 第四轮复审结果

审核日期：2026-10-17。范围：SDK，排除 Workflow、默认工具及其 SPI、Platform。

**结论：上轮 4 类缺陷的 44 项原复现用例全部通过；扩展检查仍发现 2 类问题：P1 × 1、P2 × 1。优先修复新增完成回调的 `error=` 分支：它绕过统一结果处理，既可能重复调用，也可能因非法错误数据中断运行。**

审核对象是 **`84b3355` 基础上的未提交修复工作区**，不是该 Git 提交本身。测试开始与结束时，137 个源码、既有测试、SDK 文档及包配置文件的摘要一致。测试对应的修复差异和新实现文件已保存到[源码差异证据](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round4-20261017/reviewed-source.diff)。本轮只新增 QA 测试、报告和证据，没有修改生产代码，没有提交或暂存文件。

交付前检测到 `agentloom-sdk/README.md`、`docs/sdk/implementation.md`、`docs/sdk/index.md` 更新，并新增了集成指南。SDK 源码、测试和本报告引用的能力合同没有变化；这些后续集成文档更新不属于上述测试快照。[交付摘要](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round4-20261017/source-delivery.json)另行记录，保留原始测试前后摘要不覆盖。

## 1. 验证结果

| 检查 | 结果 | 范围 |
| --- | --- | --- |
| 既有完整回归 | **813 通过，0 失败**，251.43 秒 | SDK 635 项、首次 QA 110 项、第二轮 QA 24 项、第三轮 QA 44 项 |
| 第四轮独立边界测试 | **34 通过，22 失败**，4.50 秒 | 共 56 项，覆盖双后端、完成回调两种形式、已知／未知／未开始结果、重试、非法错误字段、循环容器及运行恢复 |
| Ruff lint / format | 通过 | src、tests 与四轮 QA 共 121 个 Python 文件 |
| 测试期间文件一致性 | 通过 | 137 个既有文件未变化；交付前另有 3 份文档更新，源码及测试未变 |

最终两组共 869 项：847 通过、22 失败。22 个失败按根因归并为下面 2 类，不是 22 个独立缺陷。探索过程中的重复执行未重复计入。

## 2. R4-01 P1 错误完成回调绕过统一结果处理

**位置：** [handlers.py:180](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/runtime/handlers.py:180)、[completion.py:21](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/runtime/completion.py:21)。

这是本次新增的公开调用路径。文档允许适配器通过 `context.report_completion(error=error)` 保存已知失败，并要求它使用与正常返回相同的结果校验和租约保护，见[完成回调合同](/Users/bmilk/code/project/agent-loom/docs/sdk/capabilities.md:113)。实际实现直接抛出 error，再通过 `failed_result()` 持久化，没有经过 ToolResult 的统一归一。

### 2.1 已知失败被记为 unknown 开启重试后再次调用

复现时，适配器通过两种公开方式报告相同的 AgentLoomError，均声明 `external_outcome="known"`：

```python
context.report_completion(error=error)
# 对照：
context.report_completion(ToolResult(status="failed", error=error.to_dict()))
```

| 条件 | error 参数路径 | ToolResult 对照路径 |
| --- | --- | --- |
| 普通业务失败 | 调用 1 次，但 execution_fact=unknown | 调用 1 次，execution_fact=completed |
| category=transport、retryable_transport=true，最多调用 2 次 | **调用 2 次，execution_fact=unknown** | 调用 1 次，execution_fact=completed |

内存和文件后端均复现，共 4 个失败。调用次数由适配器内部计数验证；没有调用真实外部业务系统。对具有副作用的自定义工具，此差异可能造成重复操作。

**原因：** `failure()` 将没有额外私有属性的普通 AgentLoomError 映射为 unknown，而该入口已经是“报告完成”。随后调用驱动的“completed 不重试”规则没有生效。调用方不应为了获得正确结果而依赖未声明的 `result_material` 或 `execution_fact` 异常属性。

测试同时覆盖 external_outcome 为 unknown、not_started 的合法情况，两条路径的相应控制用例通过。修复不能把所有错误都强制标为 completed，也不能将适配器执行中直接抛出的异常误当作已返回的结果。

测试入口：[已知失败的执行事实与重试一致性](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round4.py:88)。

### 2.2 非法错误字段进入账本 NaN 使运行中断

通过同一个 error 参数入口分别传入以下错误数据，与等价的 JSON ToolResult 信封做对照：

| 非法值 | error 参数路径实际行为 | 期望及对照 |
| --- | --- | --- |
| code 和 message 为空字符串 | 原样保存为空字符串 | 拒绝为 CAPABILITY_RESULT_INVALID / protocol |
| category="outside-enum" | 未知枚举原样进入错误记录 | 拒绝为 CAPABILITY_RESULT_INVALID / protocol |
| retry_after_at=NaN | **ATTEMPT_COMPLETION_COMMIT_FAILED；Run 保持 running，raw=null，同 Session 新任务报 SESSION_BUSY** | 数据在存储前被协议校验拒绝；工具失败后 Run 可正常继续收束 |

三个场景在双后端产生 6 个失败；六个 ToolResult 对照均通过。NaN 不是存储服务故障，却在规范 JSON 序列化时抛错，再被包装为基础设施故障，导致当前推进退出。

另有两个恢复对照确认：关闭当前 Runtime，再按原 run_id 重建 Runtime 接续，可以结束运行，工具没有再次执行。因此这里是当前运行被错误中断和会话暂时占用，不能描述成后端永久损坏。

测试入口：[错误 DTO 校验](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round4.py:130)、[异常后的接续对照](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round4.py:193)。

**修复验收要求：** 将 error 参数报告也纳入统一完成结果边界，按明确 external_outcome 生成执行事实，在提交前验证错误 DTO、枚举和规范 JSON。非法数据应成为完成后的协议失败；真正的存储异常、停止、截止和失权仍按各自控制合同处理。这两组现象来自同一个绕过归一路径，合并为 R4-01，共 10 个失败用例。

## 3. R4-02 P2 循环容器返回值仍丢失完成事实

**位置：** [json_values.py:32](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/configuration/json_values.py:32)、[invocation.py:14](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/capabilities/invocation.py:14)及该模块的 normalize_result／normalize_completion。

复现输入为函数正常返回的循环列表或循环字典，例如：

```python
value = []
value.append(value)
return value
```

循环对象不属于合法 JSON，必须拒绝，但“函数已正常返回”的执行事实仍然成立。当前 JSON 检查递归遍历时触发 RecursionError，未归一成 ConfigurationError／协议结果错误，最终记录变为：

```text
code = ADAPTER_ERROR
message = RecursionError
category = unknown
execution_fact = unknown
```

FunctionToolAdapter、旧 invoke 兼容适配器、原生 invoke_once JSON 信封三条路径，在双后端、两种循环容器下全部复现，共 12 个失败。相同路径的 NaN 对照共 6 项通过，说明上轮 NaN 修复已经生效，但非法容器边界仍遗漏。

**期望：** 返回 CAPABILITY_RESULT_INVALID / protocol，保留 execution_fact=completed，不序列化循环数据。可以检测循环或将递归限制错误安全归一；不能将函数本身执行期间抛出的 RecursionError 一律视作完成。

本组测试中，每个工具均只调用一次，Run 能结束，未观察到存储污染或重复调用。本项是上轮 R3-03 的扩展边界，不声称它由本次修复新引入。

测试入口：[循环容器返回值及 NaN 对照](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round4.py:219)。

## 4. 上轮缺陷复测状态

| 上轮编号 | 本轮原用例结果 | 扩展检查 |
| --- | --- | --- |
| R3-01：关闭阶段覆盖已取得结果 | 10/10 通过，含原 6 个失败与 4 个正常对照 | MCP／模型原场景已修复；新增自定义工具 error 完成回调见 R4-01 |
| R3-02：usage 事件丢失及上界漏检 | 14/14 通过 | 既有新增套件还覆盖字段合并、0、空统计、安全整数边界和新 Operation 不继承统计 |
| R3-03：值返回适配器非法值的完成事实 | 4/4 通过 | NaN 原场景修复；循环容器见 R4-02 |
| R3-04：超大锁等待参数泄漏 OverflowError | 16/16 通过 | null、布尔值、NaN、无穷、普通负值、超大正负整数与 0 对照通过 |
| 首轮与第二轮 QA | 134/134 通过 | 原覆盖场景未复现失败 |

上表只确认对应场景的修复，不能据此将整个模块所有边界标记为通过。既有套件中的完成前后失权、提交故障、停止／截止／shutdown 和真实进程退出恢复测试也随 813 项回归执行通过。

## 5. 仍未完成的设计功能

本次变动集中于完成回执、usage 和边界校验。下列目标差异仍在，单独跟踪，不计入上述 2 类缺陷；依据[实现边界](/Users/bmilk/code/project/agent-loom/docs/sdk/implementation.md)及前轮已核对的模块实现。

| 编号 | 当前仍未完成 |
| --- | --- |
| GAP-01 | 独立 ContentStore／BlobStore、ArtifactRef 输入、来源撤销、显式外部结果授权；已有快照内预览和 result.read 不等于完整内容服务 |
| GAP-02 | 完整可靠 EventBus、outbox／订阅 ACK、完整持久等待与准入记录、通用原生 Node 扩展端口 |
| GAP-03 | 全部 Context／Environment 事件路由、生命周期修改 Hook、ScriptHookHandler；调用前后 Hook 与读取 DTO 已实现 |
| GAP-04 | 完整机器 Schema、全部查询／内容授权 DTO、OutputValidation／ValidationIssue；Hook／Tool／能力目录类型已部分补齐 |
| GAP-05 | MCP resources／templates／prompts、OAuth 凭据生命周期和完整连接分区合同 |
| GAP-06 | OCI 隔离、分布式环境、多机 writer 撤权、MCP 受限工作区挂载／短期授权令牌 |
| GAP-07 | 增量日志／索引布局、历史格式迁移、handler 实现与客户端服务地址的完整恢复指纹 |

Workflow、默认工具及其 SPI、Platform 仍不纳入本轮验收。第四轮新增用例使用本地确定性适配器；本次没有验证真实付费模型、远程 MCP、跨机协调、断电或规模性能。

## 6. 复测命令和证据

从仓库根目录运行：

```sh
.venv/bin/python -m pytest -q agentloom-sdk/qa/test_sdk_review_round4.py
.venv/bin/python -m pytest -q agentloom-sdk/tests agentloom-sdk/qa/test_sdk_audit.py agentloom-sdk/qa/test_sdk_rereview.py agentloom-sdk/qa/test_sdk_review_round3.py
```

新增 QA 位于 qa 目录，需要显式运行；失败断言未改为 xfail，也未跳过。

- [第四轮测试文件](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round4.py)
- [813 项完整回归日志](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round4-20261017/regression.txt)
- [56 项新增边界测试日志](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round4-20261017/boundaries.txt)
- [新增测试 JUnit 结果](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round4-20261017/boundaries.junit.xml)
- [版本、命令、用例和源码摘要](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round4-20261017/summary.json)
- [第三轮报告，保留作修复对照](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/REVIEW_RESULTS_2026-10-17_84b3355.md)
