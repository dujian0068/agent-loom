# QA 异常有效性评估

评估日期：2026-10-17。原报告版本：`ef31404`；当前版本：`ec1c93d`。范围沿用原报告，排除 Workflow、默认工具及其 SPI、Platform。本次未修改 SDK 源码、原测试或原报告。

**原报告的 13 类异常在原版本均可复现，未发现需要撤销的缺陷结论。当前原 QA 的 110 项用例为 42 失败、68 通过，对应 11 类仍未解决的问题；AUD-01 及 AUD-09 的原模型用例已通过。** 另行验证发现 MCP 仍遗漏 `Retry-After`，不能把模型用例通过扩大为所有 HTTP 调用均已修复。

## 复跑证据

在临时目录从 `git archive ef31404 agentloom-sdk` 提取旧源码，复制同一份 QA 测试，并使用同一 Python 环境运行。没有切换工作区分支。测试文件 SHA-256 与原证据一致：`5558b16f18d985693ff70259c9995caf70f5735044d4d6ff25b2934b03ff50c8`。

| 版本 | 结果 | 用时 | 证据 |
| --- | --- | --- | --- |
| 原报告记录 `ef31404` | 56 failed / 54 passed | 11.135 秒 | [原 JUnit](evidence/audit.junit.xml) |
| 本次重跑 `ef31404` | 56 failed / 54 passed | 11.138 秒 | [日志](evidence/assessment-ec1c93d/ef31404-audit.txt)、[JUnit](evidence/assessment-ec1c93d/ef31404-audit.junit.xml) |
| 当前 `ec1c93d` | 42 failed / 68 passed | 13.390 秒 | [日志](evidence/assessment-ec1c93d/ec1c93d-audit.txt)、[JUnit](evidence/assessment-ec1c93d/ec1c93d-audit.junit.xml) |

旧版本全部 110 项的通过／失败身份与原记录完全一致；当前仅有 14 项从失败变成通过，没有新增失败。分别为委派 Hook 的 12 个参数／后端组合，以及模型 429 的两个后端组合。计数为用例数，不能当作独立缺陷数。[版本、摘要及逐项变化](evidence/assessment-ec1c93d/summary.json)已保存。本次没有重跑正常 `tests/` 全套、wheel 或性能测试，上表仅是 QA 审查套件。

## 逐项判断

| 编号 | 原报告级别 | 当前判断 | 核对结果与影响边界 |
| --- | --- | --- | --- |
| AUD-01 | P2 | 原用例已修复，12 项通过 | 非法委派 patch 现在被丢弃，继续使用原合法任务，并保留诊断。原版本报告正确，当前不能继续列为未修复。 |
| AUD-02 | P1 | 成立，4 项失败 | 悬空本地引用未被拒绝，输出验证抛 `_WrappedReferencingError`；Run 留在 `running`，没有终态。根因是仅检查元 Schema，未验证引用可解析性，运行校验又未归一异常。构造阶段的测试位置需放宽，见下节。 |
| AUD-03 | P2 | 成立，3 项失败 | SDK 递归遍历所有字典，把 `enum`／`const` 内的数据和 `properties` 下的属性名 `$ref` 误当成引用。三个 Schema 在独立 JSON Schema 校验器中均通过。 |
| AUD-04 | P1 | 成立，6 项失败 | 对非对象 usage 直接调用 `.get()`，持久提交中断；Run 为 `running`，Operation 为 `pending/execute`，票据未释放。触发源是异常自定义适配器，不代表真实供应商已出现该响应。具体错误码断言见下节。 |
| AUD-05 | P2 | 成立，8 项失败 | `None` 先转成字符串 `"None"`，纯空白也被接受并持久写入。空字符串已正确拒绝，不能描述成全部空值均失败。非法身份应在修改准入或输入前被拒绝。 |
| AUD-06 | P2 | 成立，8 项失败 | `events.read` 在类型检查前比较游标大小，泄漏原生 `TypeError`；相邻分页接口和 `RunHandle.events` 已使用 SDK 参数错误。影响是公开接口错误合同，不是执行记录损坏。 |
| AUD-07 | P2 | 成立，3 项失败 | `from_directory` 有字段限制，直接构造的有效 Skill 包却绕过该检查。设计要求装配时同样拒绝不支持的 metadata。此复现没有证明权限提升。 |
| AUD-08 | P2 | 成立，2 项失败 | 工具已经返回可序列化结果，输出 Schema 不匹配时直接抛错，未保留结果；外部凭证从 `raw.result` 丢失。应保持失败状态并保存实际返回材料，不能为保留材料把失败改成成功。 |
| AUD-09 | P2 | 原模型用例已修复，2 项通过；MCP 仍有同类遗漏 | 原模型用例已等待服务端要求的时间。但 MCP 错误转换没有传递响应头，补充验证仍立即重试，详见下节。 |
| AUD-10 | P2 | 成立，2 项失败 | 明确的 `context_length_exceeded` 被折叠成 `HTTP_400/transport`，违反约定的 `MODEL_CONTEXT_LENGTH_EXCEEDED`。本用例未发生自动重试，问题是错误语义丢失。 |
| AUD-11 | P2 | 成立，2 项失败 | reasoning 和未完成工具参数已经到达适配器，却未保存到失败 Attempt 的 partial 材料。Run 正确失败、工具未执行；影响限于审计证据保存，不能表述为工具误执行。 |
| AUD-12 | P2 | 成立，2 项失败 | 每次清理 0.4 秒，合法租约 0.25 秒，完成时租约过期，结果无法提交；本次仍观察到 4 次清理、状态 `running`。实现没有清理续期路径。缩短 TTL 是有效故障注入，默认 TTL 下对应更长的慢清理。 |
| AUD-13 | P1 | 成立，2 项失败 | 新 Runtime 已打开，但只附着已有 Run，不重建 Agent；根截止已过，仍 `running` 并阻塞同 Session 新问题。维护扫描只有显式停止和清理，没有无 owner 的 deadline 扫描。 |

上述 P1 排序合理：AUD-13 是正常重开场景的活性问题；AUD-02／04 是异常配置或适配器材料触发的终态问题。P2 中可优先处理 AUD-12、AUD-08 和 MCP 的限流等待，分别影响清理完成、原始执行证据和远端重试节奏。

代码与设计定位：Schema 在 [configuration/models.py](../src/agentloom/configuration/models.py)，usage／重试／partial 保存位于 [runtime/calls.py](../src/agentloom/runtime/calls.py)，公开身份与游标校验位于 [runtime/services.py](../src/agentloom/runtime/services.py)，Skill 校验位于 [capabilities/skills.py](../src/agentloom/capabilities/skills.py)，工具结果与模型帧处理位于 [runtime/handlers.py](../src/agentloom/runtime/handlers.py)。清理与无 owner 截止分别参见 [cleanup.py](../src/agentloom/runtime/cleanup.py)、[host.py](../src/agentloom/runtime/host.py) 及[检查点 §5.3、§8.1](../../docs/sdk/checkpoints.md)。原报告的源码行号属于旧提交，复查时应以当前符号为准。

## 需要收紧的结论和改进的测试

**AUD-02 的异常真实，但构造测试固定在 `AgentDefinition(...)` 抛错，限制了合法修复方式。** [装配 §2.2](../../docs/sdk/assembly.md)要求装配校验失败返回 `ConfigurationError`、不创建运行记录；并未唯一限定引用解析必须发生在 Definition 值对象构造中。若在 `ReactAgent`／`PlanAgent` 构造阶段拒绝，也满足该要求。建议覆盖完整装配边界，并另设运行期异常兜底测试。现有集成用例一旦 Definition 拒绝就直接返回，不能同时证明运行期兜底已完成。

**AUD-04 的提交中断和遗留活动记录必须修复，但 `MODEL_PROTOCOL_ERROR` 是当前用例选定的具体处理。** 文档明确了完整响应协议、稳定 SDK 错误和失败终态，没有单列“非对象 usage”必须使用哪个错误码。建议明确该错误映射后保留精确断言，并增加持久 `terminal_seq`、Operation 终结、票据释放和 Session 可继续的断言。不能只把 `.get()` 异常藏起来，让记录继续卡住；若产品决定将损坏的统计降为未知，须明确诊断和统计语义并同步测试。

**AUD-03 不是 SDK 禁止远程 Schema 带来的合理限制。** `enum`／`const` 中这些对象是普通值，`properties` 中的 `$ref` 是实例属性名；它们不请求外部 Schema。已对照 [JSON Schema 2020-12 的 enum／const](https://json-schema.org/draft/2020-12/json-schema-validation#section-6.1.2)及 [properties](https://json-schema.org/draft/2020-12/json-schema-core#section-10.3.2.1)，并执行三个正向对照。[对照结果](evidence/assessment-ec1c93d/probes.json)确认标准校验器接受而 SDK 拒绝。

**AUD-09 原测试只覆盖 Chat 模型路径，不能证明 MCP 已修复。** 当前 [MCP `_transport_error`](../src/agentloom/capabilities/mcp.py) 第 132 行仍调用 `http_error(status)`，没有传递响应头，而[能力 §4.2](../../docs/sdk/capabilities.md)同样要求 MCP 解析合法 `Retry-After`。补充探针只替换外部 MCP session，保留生产 `McpToolAdapter.invoke`、错误转换、统一重试和持久化路径：第一次调用抛携带 `Retry-After: 1` 的 `httpx.HTTPStatusError`，第二次成功。

| 后端 | 两次调用间隔 | 第一 Attempt 保存的事实 |
| --- | --- | --- |
| MemoryRuntimeStore | 约 1.18 毫秒 | `HTTP_429`，没有 `retry_after_at`，`next_attempt_at` 约等于失败提交时间 |
| FileRuntimeStore | 约 5.52 毫秒 | 同上 |

共享 HTTP 解析函数在显式传入相同响应头时能正确保留截止，故此处是 MCP 接线遗漏。该结果来自本地故障注入，未连接真实远程 MCP；它不包含在原 110 项统计内。[探针代码](evidence/assessment-ec1c93d/probes.py)与[持久 Attempt 证据](evidence/assessment-ec1c93d/probes.json)已保存。

其他覆盖边界：AUD-05 没有穷举 UUID／字符串身份的全部格式；AUD-11 目前只证明选定 Chat 中断流缺少材料，修复后还应检查 Attempt 归属与跨重试隔离；AUD-12／13 的故障注入足以证明当前问题，但未完成多进程竞争、强制退出和旧 epoch 提交拒绝的全部验收。原 AUD-09 的 0.9 秒阈值用于排除立即重试，不能替代 HTTP 日期、持久恢复、停止／截止等完整等待合同测试。

## 对七组 GAP 的评价

原报告把 GAP 与 13 类缺陷分开是正确的，不能直接把它们当成额外七个已定稿合同的失败测试。Content／Artifact、可靠 outbox、完整事件路由、结构化查询和诊断、MCP 扩展、隔离环境、迁移与恢复指纹，确实还有目标设计差距；多项已在[实现记录](../../docs/sdk/implementation.md)披露。

需要保留三项边界：

- 生命周期修改 Hook 在[调用总线 §1.2](../../docs/sdk/event-bus.md)明确仍需细化；分页 DTO／机器 Schema 在[公共接口 §7](../../docs/sdk/public-api.md)也明确尚未全部定稿。应先补全合同再验收，不能为现有测试变绿临时猜接口。
- MCP OAuth 的配置期授权与执行期凭据刷新／拒绝是不同职责。可以确认当前 SDK 未完成相关集成，但不应据此把平台登录或授权管理界面纳入本次 SDK 修复范围。
- 文件后端仍是既定默认方案。增量日志、索引、迁移和持久可靠性可以在文件后端实现；这些 GAP 和本次失败都不构成引入 PostgreSQL／SQLite 的理由。Workflow 继续不纳入本次范围。

建议保留原报告作为 `ef31404` 的历史事实，使用本评估标注当前状态。后续先修复三个 P1，再处理其余已确认行为缺陷；同时维护单独的设计缺口清单。原 QA 全绿也只能证明这批已覆盖场景，不能据此宣称整个 SDK 需求已验收。
