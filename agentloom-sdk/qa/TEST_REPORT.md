# SDK 功能与边界测试报告

测试日期：2026 年 10 月 17 日，Asia/Shanghai。被测提交：`ef31404d71f4abbf3d9c9db80ddf25b06dcf6183`，版本 `agentloom-sdk 0.1.0a1`。

**现有 148 项测试全部通过，但新增的 110 项测试中有 56 项失败、54 项通过，归并为 13 类可复现缺陷。** 优先处理悬空 Schema、异常模型用量、无 owner 执行的截止扫描这三类终态问题。其余问题涉及 Hook 放行语义、清理租约、原始证据、HTTP 错误处理和输入校验。目标设计还存在 7 组未完成能力，不能用现有测试通过代替完整验收。

本次只新增本目录的测试、报告和证据，没有修复或改动生产代码。测试断言表达预期正确行为，因此缺陷未修复时显式运行新增测试会返回非零退出码；没有用 `xfail` 隐藏问题。

## 范围与方法

按用户要求，覆盖独立 ReAct、Plan 和 Executor、公共输入与控制、配置和 Schema、Hook、上下文、模型适配、自定义工具、Skill、MCP、存储及恢复。**Workflow、默认工具及默认工具 SPI、Platform 不列入缺陷或未实现清单。** 原有完整测试仍整体执行，其中包含文件工具相关用例，但未据此扩展本次缺陷范围。

主要依据：[行为约定](../../docs/sdk/requirements.md)、[公共接口](../../docs/sdk/public-api.md)、[装配](../../docs/sdk/assembly.md)、[运行](../../docs/sdk/runtime.md)、[调用总线](../../docs/sdk/event-bus.md)、[上下文](../../docs/sdk/context.md)、[能力](../../docs/sdk/capabilities.md)、[检查点](../../docs/sdk/checkpoints.md)、[验收矩阵](../../docs/sdk/verification.md)。结合[当前实现记录](../../docs/sdk/implementation.md)区分已交付链路的缺陷与目标设计缺口。

环境：macOS 26.7 arm64、Python 3.12.14、pytest 8.4.2、pytest-asyncio 1.4.0、httpx 0.28.1、jsonschema 4.26.0、MCP 1.30.0、ruff 0.16.9。新增集成测试同时使用 `MemoryRuntimeStore` 和真实 `FileRuntimeStore`；构造测试不依赖存储。模型使用 `ScriptedModel` 或 `httpx.MockTransport`，HTTP 测试走实际 SDK 适配器。原有 MCP 测试使用真实 stdio 子进程；原有恢复测试包含独立进程退出。

未执行真实付费模型、远程 MCP、跨机器或 NFS、断电恢复、持续压测和 Python 3.11／其他操作系统兼容性测试。数值上限的构造校验通过，不表示已验证 100000 轮吞吐或 128 个 Executor 的满载性能。

## 执行结果与复现

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 原有完整 SDK 测试 | 148 passed，131.73 秒 | [日志](evidence/baseline.txt)、[JUnit](evidence/baseline.junit.xml) |
| 新增功能与边界测试 | 56 failed、54 passed，11.14 秒；13 类缺陷 | [测试代码](test_sdk_audit.py)、[日志](evidence/audit.txt)、[JUnit](evidence/audit.junit.xml) |
| Ruff 检查及格式检查 | 通过，63 个 Python 文件 | 命令见下方 |
| 离线 wheel 构建 | 成功，包含 `py.typed`，不包含测试目录 | `agentloom_sdk-0.1.0a1-py3-none-any.whl` |
| wheel 独立导入与持久重开 | 第一个进程成功执行，第二个进程返回原答案，模型调用次数分别为 1 和 0 | [结果](evidence/wheel-smoke.json) |

[环境、时间、提交与测试文件摘要](evidence/summary.json)保留本次运行信息。失败用例的参数及后端展开会增加失败数量，**56 个失败不等于 56 个独立缺陷**。

在仓库根目录运行：

```sh
.venv/bin/python -m pytest -q agentloom-sdk/tests
.venv/bin/python -m pytest -q agentloom-sdk/qa/test_sdk_audit.py --tb=short
.venv/bin/ruff check agentloom-sdk/src agentloom-sdk/tests agentloom-sdk/qa
.venv/bin/ruff format --check agentloom-sdk/src agentloom-sdk/tests agentloom-sdk/qa
.venv/bin/python -m pip wheel ./agentloom-sdk --no-build-isolation --no-deps --no-index --no-cache-dir -w /tmp/agentloom-sdk-audit-wheels
```

新增测试位于 `qa/`，不会加入上方显式执行 `agentloom-sdk/tests` 的基线命令。只复现一类问题时，在新增测试命令后加 `-k` 和下表中的用例名称。

## 已复现缺陷

P1 表示应优先修复的执行阻塞问题；P2 表示影响特定场景正确性、诊断或接口合同的问题。运行类缺陷均在两种存储后端复现。

| 编号 | 级别 | 缺陷与实际影响 | 失败用例数 |
| --- | --- | --- | --- |
| AUD-02 | P1 | 悬空本地 Schema 引用通过构造，输出校验崩溃，Run 留在 running | 4 |
| AUD-04 | P1 | 非对象 usage 在持久提交中引发 AttributeError，Run 和 Operation 均未收束 | 6 |
| AUD-13 | P1 | 无 owner 的运行超过总截止后仍 running，继续占用 Session | 2 |
| AUD-01 | P2 | 非法 Executor Hook patch 被应用，原本合法的委派失败 | 12 |
| AUD-12 | P2 | 清理不续租，慢操作反复执行却不能提交成功 | 2 |
| AUD-08 | P2 | 工具输出不符合 Schema 时丢弃实际返回值，包括外部执行凭证 | 2 |
| AUD-09 | P2 | HTTP 429 的 Retry-After 被忽略，立即再次请求 | 2 |
| AUD-10 | P2 | 明确的 context_length_exceeded 被归为 HTTP_400／transport | 2 |
| AUD-07 | P2 | 直接构造 SkillPackage 可绕过不支持字段的拒绝校验 | 3 |
| AUD-03 | P2 | 合法 Schema 中的数据或属性名 `$ref` 被当作远程引用拒绝 | 3 |
| AUD-05 | P2 | 必填 input_id／command_id 的 None 和空白被接受并写入 | 8 |
| AUD-06 | P2 | 查询事件的非法游标泄漏 Python TypeError | 8 |
| AUD-11 | P2 | 中断模型流只留文本，reasoning 与不完整工具调用证据丢失 | 2 |

### AUD-02 悬空 Schema 引用使执行停留在 running

复现：使用 `output_schema={"$ref":"#/$defs/missing"}`，模型返回合法 JSON `{}`。构造未拒绝；`wait()` 抛 `EXECUTION_DRIVER_FAILED: _WrappedReferencingError`。记录 `run_id=dangling-output` 仍为 `running`，`terminal_seq=null`，业务模型 Operation 已经 `succeeded/completed`。本次文件后端 Operation ID 为 `3d7b9f29-af2a-5d9b-9db4-c8c288f94efb`，见日志。

预期：构造阶段发现不可解析的本地引用并返回 `ConfigurationError`；运行校验也应把 Schema 异常转换为稳定 SDK 错误，不能遗留无驱动的活动记录。根因位于 [configuration/models.py](../src/agentloom/configuration/models.py) 第 117、125 行：仅检查元 Schema，执行引用解析时未归一异常；[agent_loop.py](../src/agentloom/runtime/agent_loop.py) 第 154 行未捕获该异常。依据为装配文档第 2.2 节和运行文档的输出校验合同。

用例：`test_dangling_local_schema_reference_rejected_at_construction`、`test_dangling_output_schema_does_not_strand_running_execution`。

### AUD-04 异常模型用量使提交中断

复现：自定义模型返回 `ModelResponse("answer", usage=True)`，或 `usage=[1]`／字符串。`wait()` 抛 `EXECUTION_DRIVER_FAILED: AttributeError`。`run_id=malformed-usage` 留在 `running`，Operation 为 `pending/execute`，LoopTicket 仍 `reserved`，没有终态事件。文件后端布尔值场景的 Operation ID 为 `7e3c6fa2-9433-5f03-a4e5-6202fc607d0d`。

预期：在接收模型帧时校验 usage；无效协议材料形成可查询的模型失败，不应破坏存储状态推进。根因位于 [runtime/calls.py](../src/agentloom/runtime/calls.py) 第 438 行，在提交事务内直接对未经类型校验的 usage 调用 `.get()`。依据为能力文档第 1.1 节的 ModelResponse 合同及运行终态要求。本例使用异常自定义适配器返回值，不表示已观察到真实供应商这样返回。

用例：`test_malformed_model_usage_does_not_strand_running_execution`。

### AUD-13 无 owner 的过期执行不会自动收束

复现：设置总截止 1 秒，模型开始后关闭原 Runtime；在新 Runtime 中只 `runs.attach("orphan-deadline")`，不重建 Agent。等待 2.2 秒后得到 `WaitTimeout`，记录已经超过 deadline，仍为 `running`。尝试同一 `orphan-session` 的下一问题得到 `SESSION_BUSY`。文件后端 Operation ID 为 `2d915cd2-fe33-5ba1-ab7b-eb8a5af24e83`。

预期：已运行的宿主扫描到无有效 owner 的过期记录时，通过控制和撤权流程形成 `timed_out`，不要求重新执行模型。检查点文档第 5.3 节明确要求无活动租约不能让截止永远失效。当前 [runtime/host.py](../src/agentloom/runtime/host.py) 第 300 行只扫描显式停止和清理；[runtime/control.py](../src/agentloom/runtime/control.py) 第 106 行仅选择 `stopping`，没有 deadline 扫描。已有“重建 Agent 后恢复超时”的测试通过，不能覆盖本例。

用例：`test_unowned_expired_execution_is_finalized_without_agent_reconstruction`。

### AUD-01 非法委派 Hook 修改没有放行原请求

复现：Planner 产生合法委派 `{"executor":"worker","task":"valid task"}`；request Hook 把 task 改成 `None`、空串、纯空白、`0`、`{}` 或 `[]`。子模型调用次数实际为 0，委派返回 `EXECUTOR_INPUT_INVALID`。原本合法任务被错误 patch 替换，后续 Planner 收到失败。

预期：无输入 Schema 时任务仍须为非空文本；非法 patch 整体无效，记录诊断后使用原请求继续。当前 [events/hooks.py](../src/agentloom/events/hooks.py) 第 445 行只调用 `validate_schema`，当 Schema 为 None 时没有任何校验，未复用原始委派的非空文本检查。依据为行为约定第 5 节的非法 patch fail-open 规则。

用例：`test_invalid_delegate_hook_patch_fails_open`。

### AUD-12 慢清理不断过期并重试

复现：注入遵循 LocalEnvironment 接口的慢清理，单次清理耗时 0.4 秒；Runtime 租约设为合法的 0.25 秒。业务答案成功，观察 1.6 秒内清理已调用 4 次，`cleanup_state` 始终为 `running`。业务终态序号和模型调用次数保持不变。

预期：清理期间续租，或在后续接管时可靠确认原清理结果并完成任务。检查点文档第 8.1 节要求 CleanupLease 持续续期。当前 [runtime/cleanup.py](../src/agentloom/runtime/cleanup.py) 第 29 行领取后直接执行耗时清理，第 53 行因到期丢弃结果，没有续租路径。只要每次操作都长于 TTL，就无法完成；记录删除也被 `RECORD_NOT_QUIESCENT` 条件阻挡。0.25 秒是缩短测试时间的故障注入，默认 30 秒下对应每次清理都超过 30 秒的情况。

用例：`test_slow_cleanup_renews_its_lease`。

### AUD-08 工具输出校验失败时丢失原始返回值

复现：自定义工具实际执行一次，返回 `{"receipt":"external-side-effect-123","count":"invalid"}`，声明的输出 Schema 要求 count 为整数。Operation 保存了 `CAPABILITY_OUTPUT_SCHEMA_INVALID`，但 `raw.result=None`，外部凭证丢失，`execution_fact` 为 `unknown`。

预期：调用失败状态保持真实，同时保留工具已经返回的完整原始材料供审计、Hook 和恢复使用。能力文档第 2.2 节要求保存完整 raw，MCP 错误表也明确要求输出 Schema 失败保留 raw。当前 [runtime/handlers.py](../src/agentloom/runtime/handlers.py) 第 183 行先抛校验错误，未把结果交给保存路径；[runtime/calls.py](../src/agentloom/runtime/calls.py) 第 390 行仅对携带 `result_material` 的异常保留内容。

用例：`test_invalid_custom_tool_output_keeps_raw_evidence`。

### AUD-09 重试忽略服务端等待时间

复现：HTTP 第一次返回 429 和 `Retry-After: 1`，第二次成功；SDK 自动重试开启，间隔设为 0。第二次请求在内存后端约 0.0008 秒、文件后端约 0.0044 秒后发出，未等待 1 秒。

预期：按调用总线文档第 9 节等待 `max(配置间隔, 合法 Retry-After 剩余)`，并继续受总截止控制。当前 [models/http.py](../src/agentloom/models/http.py) 第 23 行只传递状态码，[runtime/calls.py](../src/agentloom/runtime/calls.py) 第 432 行只使用配置间隔。可能导致持续限流并提前耗尽重试次数。

用例：`test_http_retry_after_is_respected`。

### AUD-10 明确的上下文超窗错误丢失语义

复现：HTTP 400 的 JSON 错误体含 `code=context_length_exceeded`。实际 `RunResult.error.code=HTTP_400`，category 为 `transport`。

预期：上下文文档第 4.2 节要求 `MODEL_CONTEXT_LENGTH_EXCEEDED` 并结束对应角色。当前 [models/http.py](../src/agentloom/models/http.py) 第 11 行仅依据 HTTP 状态分类，没有解析明确的供应商错误字段。此用例没有发生自动重试；缺陷是稳定错误代码和分类不符合合同，调用方无法可靠识别超窗。

用例：`test_provider_context_length_error_has_stable_error_code`。

### AUD-07 直接 SkillPackage 装配绕过限制校验

复现：直接提供 `SkillPackage(root, metadata, files_manifest, package_key)`，metadata 含 `allowed-tools`、`disallowed-tools` 或 `model`。manifest 中的文件摘要正确，Agent 仍构造成功。

预期：能力文档第 3.2 节和装配文档第 4 节要求对已准备包的 metadata 同样拒绝这些未支持字段。当前校验只在 `from_directory()`；[capabilities/skills.py](../src/agentloom/capabilities/skills.py) 第 42 行的公共构造只检查 package_key 和 SKILL.md 是否存在，注册装配也未补验。不能把接受含权限限制字段的包当作已经执行这些限制。本例不声称获得了角色原本没有的工具权限。

用例：`test_direct_skill_package_enforces_supported_metadata`。

### AUD-03 合法 Schema 数据被误判为远程引用

复现：`{"enum":[{"$ref":"https://example.invalid/resource"}]}`、相同对象作为 `const`，以及 `{"type":"object","properties":{"$ref":{"type":"string"}}}` 均构造失败，报 `REMOTE_SCHEMA_REF`。这些位置分别是字面数据或属性名，没有外部 Schema 请求。

预期：只限制真正处于 Schema 位置的引用关键词，合法业务字段继续支持。当前 [configuration/models.py](../src/agentloom/configuration/models.py) 第 103 行不区分 Schema 结构和字面数据，对任意嵌套字典键递归检测 `$ref`。依据为 JSON Schema 2020-12 支持合同和仅禁止远程引用的规则。

用例：`test_ref_named_property_inside_enum_is_literal_data`，包含三个 Schema 参数。

### AUD-05 空身份参数被转成持久幂等键

复现：`runs.stop(..., command_id=None)` 返回 accepted receipt，command_id 为字符串 `"None"`，同时关闭目标运行准入；`inputs.append(..., input_id=None, content="supplement")` 接受同名输入。纯空白字符串同样被接受。空字符串本身已正确拒绝。

预期：必填幂等 ID 的 None 和空白应在任何状态写入前拒绝，不能形成共享的无意义键。当前 [runtime/services.py](../src/agentloom/runtime/services.py) 第 63、99 行先 `str()` 再校验。多个错误请求会复用 `"None"`，引发跨命令／输入冲突。依据为公共接口的必填身份、接收幂等及参数校验合同。

用例：`test_stop_rejects_missing_or_blank_command_id`、`test_append_rejects_missing_or_blank_input_id`。

### AUD-06 非法事件游标抛出原生 TypeError

复现：`runtime.events.read(run_id, after_seq=None)`，或传入字符串、列表、字典，均抛 Python `TypeError`。

预期：一致返回 SDK `RequestError`，与 `handle.events()` 和其他分页接口的非法游标处理相同。当前 [runtime/services.py](../src/agentloom/runtime/services.py) 第 177 行在类型校验之前执行大小比较。输入字段如果来自外层接口，可使可预期的客户端参数错误变成未捕获异常。

用例：`test_event_read_rejects_bad_cursor_with_sdk_error`。

### AUD-11 中断流丢失非文本审计材料

复现：Chat SSE 先返回 reasoning 内容，再返回工具名和不完整参数，然后在完整终帧前结束。运行正确失败，工具也没有被执行，但 Operation 和 partial_results 中均找不到已接收的 reasoning 或工具参数。

预期：能力文档第 1.1 节的 PartialModelMaterial 保存 reasoning_fragments、incomplete_calls 等，且永不当作决策执行。当前 [runtime/handlers.py](../src/agentloom/runtime/handlers.py) 第 124 行只累积 text_delta，[runtime/calls.py](../src/agentloom/runtime/calls.py) 第 392 行也只保存 partial_text。此项影响故障审计与恢复证据，不是工具误执行。

用例：`test_interrupted_model_retains_nontext_partial_material`。

## 已验证的正常路径和边界

| 范围 | 本次实际覆盖 | 结果 |
| --- | --- | --- |
| 数值策略 | Loop 1／100000、截止 1／86400、Executor 并发 1／128、模型与工具超时 1／3600、重试次数 1／20、重试间隔 0／300、历史 0／100；各自下界减一、上界加一、布尔、字符串、NaN、正负无穷 | 新增测试通过；为构造校验 |
| 上下文特殊值 | trigger_ratio 排除 0 和 1，允许接近 0；safety_margin=0；idle_timeout=None | 新增测试通过 |
| 空输入 | 无 Schema 时拒绝 None、空串、空白、0、False、空对象和空数组 | 新增测试通过；未创建 Run、未调用模型 |
| 显式 JSON 输入输出 | Schema 允许时 None、空串、0、False、空对象、空数组都可执行 | 新增测试通过 |
| 序列化与冻结 | 最大安全整数 ±(2^53−1)、越界整数、非有限数、任意 Python 对象、中文、emoji、NUL、调用方修改原始数组、修改查询快照 | 新增测试通过 |
| 身份与并发准入 | 同 run_id 附着、不同内容冲突、同 Session 的第二运行被拒绝、同 Agent 不能改绑、未重复调用模型 | 新增测试通过 |
| Loop 与批次 | 第 100 次完成／第 101 次拒绝、格式纠正计票、串并行、全部结算与有序消费、Executor 独立上下文、并发名额 | 原有测试通过 |
| 停止、重试与历史 | stop 与等待取消分离、关闭保留现场、首输出和 idle 超时、根截止不变成连接重试、跨两轮补充转交、历史不重复膨胀 | 原有测试通过；异常 ID、无 owner 截止另见缺陷 |
| Hook 与压缩 | 顺序、有效修改、raw 保留、受保护输入、合法 deny、读端口过期、超窗 patch 放行、摘要失效、原 Hook 结果恢复复用 | 原有测试通过；委派空任务 patch 另见缺陷 |
| MCP 与能力授权 | stdio 子进程握手、目录／Schema 漂移、isError 不按连接错误重试、分页游标异常、角色工具与结果隔离 | 原有测试通过；远程 MCP 未执行 |
| 持久化 | 文件事务原子性、退出切点、损坏拒绝、双宿主租约、丢回执、已存 raw／Hook／子任务恢复、清理失败重试 | 原有测试通过；慢清理续租另见缺陷 |

## 目标设计中尚未完成的功能

下列是设计与交付的差距，不重复计算为上述 13 类缺陷。多数在当前实现记录中已主动声明；表中的“未完成”不表示现有等效路径完全不可用。

| 编号 | 未完成范围 | 当前实际状态与依据 |
| --- | --- | --- |
| GAP-01 | ContentStore／BlobStore、ArtifactRef 导入和来源撤销、显式外部结果授权 | 大结果保存在运行快照中，已有角色受限预览；`AgentInput.artifacts` 非空返回 `ARTIFACT_INPUT_UNSUPPORTED`，新增两后端测试确认。见能力文档第 5.2 节、实现记录差异第 3 项 |
| GAP-02 | 完整可靠 EventBus、订阅 ACK／outbox 游标、持久 Wait／批次准入和原生 Node 激活扩展 | 现有 EventBus 只有注册、冻结、单请求路由，业务恢复由 Runner／CallDriver 的执行快照承担；没有文档中的完整独立总线及调度端口。见调用总线第 3、10 节及实现记录差异第 5 项 |
| GAP-03 | Context／Environment 全部事件路由、生命周期修改 Hook、ScriptHookHandler | 当前 Hook 路由仅有 model、capability、executor、Skill、result；仅提供 FunctionHookHandler。生命周期修改 Hook 本身仍属待细化设计，不能要求现有 Operation Hook 替代。见装配第 5 节、调用总线第 1.2 节 |
| GAP-04 | 完整机器 Schema、查询／内容授权 DTO、结构化 OutputValidation／ValidationIssue | 公共数据主要为 dataclass 和 JSON 投影；输出校验存在，但反馈为通用纠正文本，没有完整结构化诊断接口。见公共接口第 7 节、能力第 1.3 节、运行第 4.4 节及实现记录差异第 5 项 |
| GAP-05 | MCP 的资源／模板／Prompt 接入和 OAuth 凭据生命周期 | 当前适配器公开 discover tools 和 invoke，支持 stdio／HTTP 连接；没有上述完整方法与 OAuth 流程。见能力文档第 4 节和 `capabilities/mcp.py`。HTTP 头注入不等同 OAuth 授权及刷新实现 |
| GAP-06 | 分布式执行环境、OCI 隔离、多机 writer 撤权、MCP 工作区挂载或短期访问令牌 | 默认环境是本地 POSIX；stdio MCP 为可信宿主进程，脚本执行需调用方注入隔离环境和网关。见能力第 4.2、5 节及实现记录差异第 2、7 项。此项不包含本次排除的默认工具 SPI |
| GAP-07 | 增量日志／索引存储、历史格式迁移及完整恢复资源指纹 | 现有后端使用整份运行快照和 namespace 清单；恢复清单未完整覆盖 Python handler 实现及客户端服务地址。调用方仍需保证重建同一实现和资源。见检查点设计及实现记录差异第 1、6 项；不把缺少 PostgreSQL 适配器算作必须引入数据库的问题 |

## 修复与复测顺序

先处理 AUD-02、AUD-04、AUD-13，保证异常配置／适配器输出和无人接管时的截止都能形成可查询终态并释放会话。随后处理 AUD-01 和 AUD-12，补齐 Hook 修改验证及清理租约。其余输入校验、HTTP 分类与证据保存可分别修复，并用本目录的对应用例验收。

复测门槛是新增测试按预期通过，同时原有 148 项不退化。GAP 项应单独确定交付范围及验收用例；不能为让测试变绿而删除保护条件、吞掉异常或将已执行的外部调用自动重发。
