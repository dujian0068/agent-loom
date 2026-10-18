# SDK 第六轮复审结果

审核日期：2026-10-18，Asia/Shanghai。审核基线：`2b47ea96111326b3e13d5a0f808583bcf50cc8b4`，**加上工作区尚未提交的模式 Prompt 实现及配套修改**。范围：SDK；排除 Workflow、默认工具及其 SPI、Platform。

**结论：第五轮两类缺陷的 72 项原复现及对照全部通过；ReAct、Planner、Executor 系统 Prompt 已补齐。扩展测试仍发现 2 类 P2 缺陷：非法最终 JSON 候选跳过格式纠错，以及异常附带结果结构不完整时中断 Attempt 提交。**

本轮只新增第六轮 QA 测试、报告和证据，保留旧报告及用户修改。测试前后 156 个既有源码、测试、SDK 文档和配置文件摘要一致，Git HEAD 未变化。新增测试单独记录摘要，见[版本与测试摘要](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round6-20261018/summary.json)。

## 1. 验证结果

| 检查 | 结果 | 说明 |
| --- | --- | --- |
| SDK 与前五轮 QA 回归 | **1155 通过，0 失败**，305.37 秒 | SDK 849 项，前五轮 QA 306 项；独立白盒探针单列 |
| 第五轮原复现及对照 | **72/72 通过** | 已包含在 1155 项中，不重复累计 |
| 模式 Prompt 与恢复专项 | **62/62 通过** | 模式组装 34 项、恢复 28 项；已包含在 1155 项中 |
| 第六轮独立边界测试 | **72 通过，42 失败**，7.45 秒 | 共 114 项，42 个失败归为下述两类缺陷 |
| 现有独立白盒探针 | **22 通过，4 失败**，36.97 秒 | 4 个失败均确认是测试构造／断言问题，详见第 5 节 |
| 白盒探针临时修正版 | **4/4 通过**，1.24 秒 | 仅改临时副本；未修改仓库原测试，不重复累计 SDK 用例 |
| Ruff lint / format | 主体通过 | src、tests、六轮 QA 中的 138 个 Python 文件通过；独立白盒文件另有 5 个 lint 问题 |
| 审核期间文件一致性 | 通过 | 156 个既有文件摘要未变化；`git diff --check` 通过 |

主要回归与新增边界测试共 **1269 项：1227 通过、42 失败**。白盒原文件 26 项和临时修正版的执行结果单列，不能据此声称当前整个 tests 目录无失败。

环境：Python 3.12.14、macOS 26.7 arm64、pytest 8.4.2。Python 递归限制保持默认 1000。

## 2. R6-01 P2 非法最终 JSON 候选跳过格式纠错

**位置：** [候选解析和异常分类](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/runtime/agent_loop.py:145)，特别是同一 try 块中的 `json_copy(json.loads(...))` 与 `except ConfigurationError`。

**触发条件：** Agent 设置了合法的 `output_schema`，模型的最终文本能够进入 JSON 解析，但解析结果不满足 SDK 的规范 JSON 约束。测试使用合法的空 Schema `{}`，第一次返回非法候选，第二次预设返回合法的 `42`；Loop 额度充足。

| 第一次返回的文本 | 实际错误 | 预期 |
| --- | --- | --- |
| `NaN` | `INVALID_JSON`，非有限数值 | 追加格式反馈，再请求模型修正 |
| `1e309` | `INVALID_JSON`，浮点溢出为 Infinity | 同上 |
| `9007199254740992` | `INVALID_CANONICAL_JSON`，超出安全整数范围 | 同上 |
| JSON 转义文本 `"\ud800"` | `INVALID_CANONICAL_JSON`，孤立代理项 | 同上 |
| 129 层嵌套数组 | `INVALID_JSON`，超过 128 层边界 | 同上 |

三种角色、两种存储全部复现，共 **30 个失败**：目标角色只调用模型一次，格式反馈记录为 0，任务直接变为 failed，预设的修正答案没有机会返回。ReAct 和 Planner 根任务失败时 Run 也失败；Executor 场景是子任务失败，外层 Planner 仍可继续处理委派失败，不能将其混称为所有 Run 都失败。

原因是候选数据的 `json_copy()` 同样抛出 `ConfigurationError`，却被当成“不可由模型修正的坏 Schema”重新抛出。Schema 本身在这些用例中完全有效。

依据[标准输出与修正合同](/Users/bmilk/code/project/agent-loom/docs/sdk/capabilities.md:72)，无效候选应保留原答案，追加格式反馈，并在同一根额度和 deadline 下创建新的业务模型 Operation。实际错误不应仅因 JSON 失败发生在解析阶段还是规范化阶段而改变处理路径。

**正常对照：** 不可解析的 `not-json` 和不满足 number Schema 的 `[]` 在三种角色、双后端共 12 项中均能修正为 `42`。另有 32 项有效边界对照通过：null、false、0、空字符串、空数组、空对象、最大安全整数 `9007199254740991`、128 层数组；ReAct 和空 Executor 表的 Plan 均正常接受，且空业务 Prompt 不会丢失 SDK 系统指令。

**修复验收要求：** 分开处理候选数据解析／规范化错误和 Schema 配置错误。前者进入格式纠错，后者仍及时失败；修正消耗正常业务 Loop，不转成同一 Operation 的传输重试，也不获得额外额度。

测试入口：[非法候选矩阵](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round6.py:110)、[语法与 Schema 对照](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round6.py:139)、[合法空值与极限值](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round6.py:170)。

## 3. R6-02 P2 异常附带结果结构不完整时中断持久化

**位置：** [failed_result 的附加字段处理](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/runtime/completion.py:44)、[Attempt 提交时读取信封字段](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/runtime/calls.py:490)。

**触发条件：** 自定义适配器直接抛出一个合法的 AgentLoomError，但其 `tool_result` 或 `invalid_tool_result` 属性是空列表、空对象或字符串。这里检查的是 SDK 已读取的内部附加属性的健壮性；这些属性并不是建议业务方使用的公开结果接口，普通合法错误不受此项影响。

最小示例：

```python
async def invoke_once(self, request, context):
    error = AgentLoomError("FAILED")
    error.tool_result = {}  # 或 invalid_tool_result = []
    raise error
```

这些值都是可序列化 JSON，因此 `failed_result()` 接受并复制它们；后续持久化代码却直接读取 `envelope["status"]` 或 `invalid_envelope["value"]`，没有验证对应结构。由此泄漏 KeyError／TypeError，最终表现为：

```text
RunHandle.wait() -> InfrastructureError(EXECUTION_DRIVER_FAILED)
Run.status = running
Operation.raw = null
工具调用次数 = 1
```

两类字段、三种非法形状、双后端共 **12 个失败**。失败发生在原始工具错误提交前，模型收不到正常的工具失败反馈，Run 无法正常收束。这是适配器数据导致的异常，不是真实的存储服务中断。

**正常对照：** 相同错误通过 `context.report_completion(error=error)` 报告后，附加属性不会破坏提交，模型可继续结束 Run；附加属性为 None、合法 ToolResult 信封或合法 `{ "value": null }` 包装时也正常。该组共 40 项：12 失败、28 通过。

第五轮已修复错误 DTO 字段本身，以及循环／超深附加材料；本项是另一个未覆盖的结构边界。依据[适配器错误归一合同](/Users/bmilk/code/project/agent-loom/docs/sdk/capabilities.md:121)，非法适配器材料应形成安全的失败记录；[已有附加材料保护测试](/Users/bmilk/code/project/agent-loom/agentloom-sdk/tests/test_operation_error_boundaries.py:122)也明确要求这类材料不能破坏 Attempt 存储。

**修复验收要求：** 在进入提交前验证附加材料的对象类型和必要字段；对不完整材料安全丢弃或转为协议失败。直接执行中抛错仍保持 unknown 事实，已经确认的完成事实单独保留；不要通过捕获所有持久化异常来掩盖真实存储故障。

测试入口：[异常附加字段与回调对照](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round6.py:219)。

## 4. 上轮修复与模式 Prompt 确认

| 项目 | 本轮结论 |
| --- | --- |
| R5-01 嵌套结果损坏持久化／恢复 | 原 52 项深度及恢复用例全部通过；明确的边界深度及内部文档预算替代了依赖调用栈的隐含上限 |
| R5-02 直接抛出的非法错误 DTO | 原 20 项错误字段与上报方式对照全部通过，字段异常已安全归一 |
| 前四轮 QA | 原 234 项全部通过 |
| ReAct／Planner／Executor 模式指令 | 已实现；组装、模型请求体、角色隔离、Hook 保护、预算和恢复专项均通过 |
| 后台维护关闭 | 新增的 4 项关闭／取消测试随完整回归通过 |

两项新发现来自扩展边界测试，不代表第五轮原用例仍未修复，也没有据此认定它们由模式 Prompt 改动新引入。

系统 Prompt 现在位于以下位置：

| 职责 | 代码位置 |
| --- | --- |
| ReAct、Planner、Executor 模板及公共规则 | [prompts/templates.py](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/prompts/templates.py:5) |
| 按模式、角色、输出 Schema、Executor 输入及变量合同组装 | [prompts/assembly.py](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/prompts/assembly.py:16) |
| 业务 Prompt 与 SDK Prompt 写入任务上下文 | [runtime/runner.py](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/runtime/runner.py:21) |
| 重启时核对保存的 Prompt 合同和任务／请求记录 | [prompts/recovery.py](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/prompts/recovery.py:18) |

正常请求的顺序为业务 system 消息、SDK 模式 system 消息，然后是历史／当前输入。SDK 模式指令按 Run 保存版本和正文，固定且受保护；Executor 使用自己的业务定义和模式指令。旧记录维持旧行为，恢复时不自动套用新模板。

本轮验证的是指令是否正确生成、发送、隔离、计入预算、保存及恢复。真实模型是否稳定遵守指令、分解任务是否合理、回答质量是否达标，仍需真实 LLM 集成与效果评测。

## 5. 现有白盒测试的误报与质量问题

`test_review_whitebox_probe.py` 已纳入本轮单独执行，原文件保留不动。4 个失败在仅调整测试构造／断言的临时副本中全部通过：

| 原失败测试 | 原因 | 临时验证方法 |
| --- | --- | --- |
| [压缩触发](/Users/bmilk/code/project/agent-loom/agentloom-sdk/tests/test_review_whitebox_probe.py:602) | 用当前可见 tool 消息数量决定何时结束；压缩移除旧反馈后计数变小，模型夹具持续发起工具，最后等待超时 | 改用业务模型调用次数，第四轮返回最终答案；仍验证压缩发生和业务 Loop 等于 4 |
| [未知工具不创建 Operation](/Users/bmilk/code/project/agent-loom/agentloom-sdk/tests/test_review_whitebox_probe.py:650) | 场景实际有两次模型调用和一次合法工具调用，断言总数为 2 错误 | 总数改为 3，保留未知工具错误及只有一个 capability Operation 的断言 |
| [跨角色 result_read](/Users/bmilk/code/project/agent-loom/agentloom-sdk/tests/test_review_whitebox_probe.py:741) | `last_run_id` 从未写入；模型夹具访问 `[0]` 抛异常，实际没有发起 result_read | start 返回后先保存 run_id，再 wait；验证真实读取被 RESULT_ACCESS_DENIED 拒绝 |
| [停止优先](/Users/bmilk/code/project/agent-loom/agentloom-sdk/tests/test_review_whitebox_probe.py:856) | 延迟放在 yield 完整帧之后，不能阻止成功提交；调用方 sleep 与完成存在竞态 | 用 Event 在模型返回完整帧之前阻塞，确认已进入调用后发 stop；取消与幂等断言通过 |

这 4 项不计入 SDK 的两类缺陷。原白盒文件另有 5 个 Ruff 问题：导入排序、重复导入和未使用名称／变量，见[lint 日志](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round6-20261018/whitebox-ruff.txt)。

证据：[原执行日志](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round6-20261018/whitebox.txt)、[临时修正版差异](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round6-20261018/whitebox-fixture-corrections.diff)、[四项通过日志](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round6-20261018/whitebox-corrected.txt)。

## 6. 仍未完成的设计功能

模式 Prompt 从此前缺失状态变为已实现。以下差异仍需单独跟踪，依据[当前实现范围](/Users/bmilk/code/project/agent-loom/docs/sdk/implementation.md:382)及相应模块核查，不计入上面两个运行缺陷：

| 编号 | 尚未完成 |
| --- | --- |
| GAP-01 | 独立 ContentStore／BlobStore、ArtifactRef 输入、来源撤销与显式外部结果授权 |
| GAP-02 | 完整可靠 EventBus、独立 outbox／订阅 ACK、全部持久等待与准入记录、通用原生 Node 扩展端口 |
| GAP-03 | 全部 Context／Environment 事件路由、生命周期修改 Hook、ScriptHookHandler |
| GAP-04 | 完整机器 Schema、全部查询／内容授权 DTO、OutputValidation／ValidationIssue 的目标接口 |
| GAP-05 | MCP resources／templates／prompts、OAuth 凭据生命周期和完整连接分区合同；当前 McpToolAdapter 主要提供工具发现与调用 |
| GAP-06 | OCI 隔离、分布式环境、多机 writer 撤权、MCP 受限工作区挂载／短期授权令牌 |
| GAP-07 | 增量日志／索引布局、历史格式迁移、handler 实现及客户端服务地址的完整恢复指纹 |

Workflow、默认工具及其 SPI、Platform 继续排除，不将其未实现项计为本轮验收缺陷。

## 7. 测试方式与复测入口

**本次没有使用真实 LLM key 或密钥。** 模型行为由 ScriptedModel 确定性返回；Chat／Responses 协议测试使用 httpx.MockTransport 检查请求并返回本地模拟流；MCP 测试使用本地子进程或模拟会话。占位凭据仅用于本地测试。

因此，回归通过说明代码流程与已覆盖合同符合预期，不等于已经通过真实供应商鉴权、线上网络、限流、远程 MCP 或模型推理效果验证。跨机协调、断电与规模性能也未作为本轮通过项。

从仓库根目录执行：

```sh
.venv/bin/python -m pytest -q agentloom-sdk/qa/test_sdk_review_round6.py
.venv/bin/python -m pytest -q agentloom-sdk/tests --ignore=agentloom-sdk/tests/test_review_whitebox_probe.py agentloom-sdk/qa/test_sdk_audit.py agentloom-sdk/qa/test_sdk_rereview.py agentloom-sdk/qa/test_sdk_review_round3.py agentloom-sdk/qa/test_sdk_review_round4.py agentloom-sdk/qa/test_sdk_review_round5.py
.venv/bin/python -m pytest -q agentloom-sdk/tests/test_review_whitebox_probe.py
```

新增 QA 需显式运行；断言要求预期的正常行为，没有用 xfail／skip 隐藏失败。

- [第六轮测试文件](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round6.py)
- [1155 项回归日志](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round6-20261018/regression.txt)
- [114 项边界测试日志](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round6-20261018/boundaries.txt)
- [新增测试 JUnit 结果](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round6-20261018/boundaries.junit.xml)
- [版本、环境、命令与用例摘要](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round6-20261018/summary.json)
- [本轮审核的工作区源码差异](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round6-20261018/reviewed-source.diff)
- [第五轮报告](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/REVIEW_RESULTS_2026-10-17_ROUND5.md)
