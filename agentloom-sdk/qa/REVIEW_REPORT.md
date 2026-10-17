# SDK 修复版复审报告

日期：2026-10-17。审核 SDK 提交：`220ebd4e584967a86a341510675eeb98b8b4dd06`。范围沿用首次测试：SDK，排除 Workflow、默认工具及其 SPI、Platform。

**原有 447 项回归全部通过；补充 24 项边界测试为 13 失败、11 通过，归纳为 5 类缺陷：1 项 P1、4 项 P2。** 原报告的 13 类问题在原有触发场景下均已通过，但慢撤权、Responses 错误帧和预制 Skill 包仍有遗漏边界。空 usage 对象覆盖统计已用修复前源码对照，确认是本次引入的回归。

本次只新增测试、报告和证据，没有修改 SDK 生产源码或现有测试。审核期间 HEAD 更新为 `3450aed`，新增提交仅涉及文档界面；审核前后的 `agentloom-sdk/src` Git tree 均为 `5235633296e01753a171e1d7b53ba8e8fe9d1cfe`。

**实际执行结果**

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 正常 SDK 测试及原 QA | 447 passed，185.52 秒；其中 `tests/` 337 项、原 QA 110 项 | [日志](evidence/review-220ebd4/regression.txt)、[JUnit](evidence/review-220ebd4/regression.junit.xml) |
| 本次新增边界测试 | 13 failed / 11 passed，13.58 秒；24 项，无跳过 | [用例](test_sdk_rereview.py)、[日志](evidence/review-220ebd4/followup.txt)、[JUnit](evidence/review-220ebd4/followup.junit.xml) |
| 修复前空 usage 对照 | `ec1c93d` 的两后端空对象用例均通过；相同用例在本版均失败 | [日志](evidence/review-220ebd4/before-empty-usage.txt)、[JUnit](evidence/review-220ebd4/before-empty-usage.junit.xml) |
| Ruff lint / format | 通过 | [lint](evidence/review-220ebd4/ruff.txt)、[format](evidence/review-220ebd4/ruff-format.txt) |

版本、环境、依赖、测试文件 SHA-256、具体失败名称和命令见 [summary.json](evidence/review-220ebd4/summary.json)。失败数是参数及存储后端组合数，不是独立缺陷数。新增测试保留正常失败断言，没有标记 xfail，也没有改写原始历史证据。

**R01 · P1 · 无 owner 收束时未续租，慢撤权导致永久停留在 stopping**

定位：[runtime/control.py](../src/agentloom/runtime/control.py) 第 131–144 行；`finalize_run` 第 51–55 行先同步调用 `environment.revoke`，随后才提交终态。

复现步骤：

1. 启动一个总截止为 1 秒的 Run，模型开始后关闭原 Runtime，保留原执行记录。
2. 新 Runtime 使用同一存储，只 attach，设置合法的 `lease_seconds=0.15`、`cleanup_retry_seconds=0.05`。
3. 注入每次耗时 0.35 秒、最终成功返回的 `revoke`；分别测试显式 stop 和自动 deadline。
4. 等待 2.4 秒，检查终态和同 Session 的下一次准入。

实际：两后端、两种控制共 4 项失败。记录停留在 `stopping`、`terminal_seq=null`，Loop 票据仍预留；撤权被重复调用，维护诊断反复记录 `LEASE_LOST`，同 Session 的新问题返回 `SESSION_BUSY`。将同一撤权延迟改为 0.02 秒后，4 项对照全部通过。

原因：控制扫描领取业务租约后，没有在撤权期间续租。只要每次撤权都长于 TTL，每次终态提交都会因租约过期而失败。已新增的 `CleanupLeaseGuard` 只保护终态后的目录清理，不覆盖这段控制收束。短 TTL 是故障注入；默认 TTL 下对应持续超过 30 秒的环境撤权。

预期：成功完成撤权后能持有效执行权提交唯一终态、释放票据及会话；不能因为每次合法撤权耗时超过 TTL 而无限重做。依据：[检查点 §5.3、§8](../../docs/sdk/checkpoints.md)。建议给控制收束补齐续租和失权处理，并避免单个慢撤权阻塞后续控制扫描。

用例：`test_orphan_finalizes_when_revocation_exceeds_lease`。这是 AUD-13 的扩展场景；原无 owner 截止用例和 AUD-12 慢清理用例本次均通过。

**R02 · P2 · 后续空 usage 对象清空已知 token 统计**

定位：[models/openai.py](../src/agentloom/models/openai.py) 第 117–122 行，以及 [models/material.py](../src/agentloom/models/material.py) 的 `normalize_usage`。

复现：Chat SSE 先返回 `prompt_tokens=3, completion_tokens=2`，后续结束帧携带 `usage={}`，再正常发送 `[DONE]`。将后续 usage 替换为两个计数均为 null 的对象也可复现。

实际：Run 成功，但 `input_tokens`／`output_tokens` 变成 null，`token_counts_complete=false`。两种空对象形态在两后端共 4 项失败；`usage=null` 的 2 项对照通过。

原因：空对象归一后成为非空的 `{input_tokens: None, output_tokens: None}`，随后整体覆盖之前已解析的统计。本次把原来的 truthy 判断改为检查键存在，因此空对象分支出现退化。同一份空对象测试对 `ec1c93d` 源码执行为 2 passed，本版为 2 failed；这里仅将 `{}` 分支认定为新引入回归。

预期：最终仍保留 `(3, 2)` 和完整统计标记。当前 [能力 §1.1](../../docs/sdk/capabilities.md) 已明确后续空 usage 不能清除已知统计。建议按计数字段保留已知值，区分缺失／null 与合法的 0。

用例：`test_empty_usage_after_known_usage_preserves_counts`。

**R03 · P2 · Responses 顶层 error 事件漏掉超窗错误映射**

定位：[models/provider_errors.py](../src/agentloom/models/provider_errors.py) 第 8–10 行；调用点为 [models/openai.py](../src/agentloom/models/openai.py) 第 212–220 行。

复现：Responses SSE 返回 `type="error"`，同层包含 `code="context_length_exceeded"`、`message`、`param`、`sequence_number`。这是官方定义的事件字段布局，错误码位于事件顶层。参见 [OpenAI Responses streaming events 的 error 定义](https://developers.openai.com/api/reference/resources/responses/streaming-events)。

实际：两后端均返回 `MODEL_PROVIDER_ERROR / business`，未得到文档要求的 `MODEL_CONTEXT_LENGTH_EXCEEDED / validation`。相同错误放在 `response.failed.response.error` 中，两项对照通过。开启两次重试后仍只调用一次；此项是错误分类遗漏，没有观察到不当重试。

原因：`provider_error` 只读取 `payload["error"]`；顶层 error 事件被直接传入后找不到此嵌套字段。建议在适配器内区分两种事件结构，再交统一映射，不依据 message 文本推断错误。

依据：[上下文 §4.2 的超窗合同](../../docs/sdk/context.md)，第 223 行。用例：`test_responses_explicit_context_error_mapping_in_both_event_shapes`。这是 AUD-10 的剩余协议分支；原 HTTP 400 错误体场景已通过。

**R04 · P2 · 预制 Skill 包仍绕过正文的受支持语法检查**

定位：[capabilities/skills.py](../src/agentloom/capabilities/skills.py) 第 43–53、65–68、131–151 行。

复现：写入具有合法 name／description 的 `SKILL.md`，正文分别包含动态 shell 标记或 `${CLAUDE_SESSION_ID}`；按实际文件内容计算正确 manifest。相同目录通过 `SkillPackage.from_directory` 构造会拒绝，但使用公开的 `SkillPackage(root, metadata, manifest, key)` 后，可成功装配 Agent，`DirectorySkillLoader.read_entry` 也直接返回原文。

实际：两个正文场景均没有抛出 `SKILL_PROFILE_UNSUPPORTED`。本版共享了 metadata 校验，但正文检查仍只放在 `from_directory`，预制资源路径的构造、装配和读取均未执行同等检查。

预期：不支持的正文执行语义在使用边界明确拒绝，两种包准备方式行为一致。依据：[能力 §3.1–3.2](../../docs/sdk/capabilities.md)。建议把受支持正文语法检查纳入共享验证，在首次读取入口前完成。这里证明的是兼容性校验被绕过，没有观察到 shell 执行或工具权限扩大。

用例：`test_prepared_skill_rejects_unsupported_body_like_directory_import`。原 AUD-07 的 metadata 字段校验已通过，此项是该资源入口的另一条遗漏路径。

**R05 · P2 · token 累计越界污染内存状态，Run 无法形成终态**

定位：[runtime/calls.py](../src/agentloom/runtime/calls.py) 第 480–483 行；序列化范围见 [configuration/json_values.py](../src/agentloom/configuration/json_values.py) 第 15–18 行。

复现：第一轮模型返回一个工具调用，usage.input_tokens 为 `2**53-1`；工具执行后第二轮返回最终文本，usage.input_tokens 为 1。单次值均满足当前规范化整数边界，但累计值为 `2**53`。

实际：内存后端先保存了越界累计值，后续 checkpoint hash／终态序列化失败，`handle.wait()` 抛 `EXECUTION_DRIVER_FAILED: ConfigurationError`。记录保留 `running`、`terminal_seq=null`，同 Session 无法继续。文件后端在首次越界写入时回滚，能返回失败终态；累计值恰为 `2**53-1` 的两个后端对照均成功。因此该组为 1 项失败、3 项通过。

这是按用户要求执行的极限值／异常提供方统计测试，不表示正常模型会产生如此大的 token 数。问题是 SDK 接受的单次材料可以使累计状态越过自身序列化边界，并且两种后端结果不同。

预期：累计值必须保持可持久化；若拒绝统计，应按明确的 SDK 错误合同收束并释放会话，不能留下不可提交的活动状态。建议在修改状态前检查累计范围，并保证内存与文件后端的存储约束一致。终态合同见 [公共接口 §4](../../docs/sdk/public-api.md)。

用例：`test_usage_aggregation_at_canonical_integer_boundary_does_not_strand_run`。这是新增极限值发现，不认定为本次修复引入的回归。

**对上轮修复的判断**

| 上轮编号 | 本次确认 |
| --- | --- |
| AUD-01、02、03、05、06、08、09、11、12 | 原触发场景和现有扩展回归通过，包括 MCP Retry-After、Schema 离线引用、异常工具 raw 保留、流片段留存及清理续租 |
| AUD-04 | 原非法 usage 类型用例通过；新增空统计及累计上界问题见 R02、R05 |
| AUD-07 | 不支持的 metadata 字段在两种构造路径均拒绝；正文语法仍遗漏，见 R04 |
| AUD-10 | HTTP 400 与嵌套 Responses 错误映射通过；顶层 error 事件遗漏，见 R03 |
| AUD-13 | 正常撤权下无 owner 截止可形成终态；慢撤权仍失败，见 R01 |

原 QA 中 Schema 测试允许在完整装配边界拒绝，并另外注入旧无效 Schema 验证运行期终态；非法 usage 的协议错误码也已写入设计，测试同时检查票据、终态和 Session 继续。这两处调整没有取消原来的核心保护条件。

**仍未实现的目标功能**

本次修复没有完成原报告的七组 GAP。下列保持为独立功能缺口，不计入上述 5 类缺陷，也不要求通过引入数据库来解决。

| 编号 | 仍缺少的能力 |
| --- | --- |
| GAP-01 | 独立 ContentStore／BlobStore、ArtifactRef 输入、来源撤销、显式外部结果授权；非空 artifacts 仍明确拒绝 |
| GAP-02 | 完整可靠 EventBus、独立 outbox／订阅 ACK、持久等待和准入记录、原生 Node 扩展 |
| GAP-03 | 全部 Context／Environment 事件路由、生命周期修改 Hook、ScriptHookHandler；部分合同仍待细化 |
| GAP-04 | 完整机器 Schema、查询／内容授权 DTO、结构化 OutputValidation／ValidationIssue |
| GAP-05 | MCP resources／templates／prompts 与 OAuth 凭据生命周期；当前主要是工具发现和调用 |
| GAP-06 | OCI 隔离、分布式环境、多机 writer 撤权、MCP 工作区挂载或短期令牌 |
| GAP-07 | 增量日志／索引布局、历史格式迁移、Python handler 与客户端服务地址的完整恢复指纹 |

详细依据沿用 [首次报告的 GAP 表](TEST_REPORT.md)和[当前实现差异](../../docs/sdk/implementation.md)。Workflow 和默认工具未被纳入新发现或交付要求。

**复现及复测**

在仓库根目录运行：

```sh
.venv/bin/python -m pytest -q agentloom-sdk/tests agentloom-sdk/qa/test_sdk_audit.py --tb=short
.venv/bin/python -m pytest -q agentloom-sdk/qa/test_sdk_rereview.py --tb=short
.venv/bin/ruff check agentloom-sdk/src agentloom-sdk/tests agentloom-sdk/qa
.venv/bin/ruff format --check agentloom-sdk/src agentloom-sdk/tests agentloom-sdk/qa
```

建议先处理 R01 的控制收束活性，再补齐 R02–R05 的数据边界与一致性。验收应要求新增 24 项与已有 447 项一起通过，并保留 fast／slow、null／空对象、嵌套／顶层错误、恰好上界／越界等对照。

本次环境为 macOS arm64、Python 3.12.14。HTTP 使用 MockTransport；慢撤权通过继承 LocalEnvironment 注入延迟；内存与真实文件存储均执行。没有执行真实付费模型、远程 MCP、多机／NFS、断电、其他 Python／OS 或长时间负载测试。本轮没有重复构建 wheel 或文档站点；相关历史结果不当作本次重新验证的证据。
