# SDK 第二轮复审结果清单

整理日期：2026-10-17。

审核版本：`220ebd4e584967a86a341510675eeb98b8b4dd06`。范围：SDK，排除 Workflow、默认工具及其 SPI、Platform。

本文件单独汇总已完成的第二轮复审结果，作为修复跟踪入口。原始报告和证据保留；本次整理没有修改生产代码，也没有重新执行测试。

**结论：仍有 5 类待修复缺陷，其中 P1 为 1 类、P2 为 4 类；另有 7 组目标功能尚未完成。** 原有回归全部通过，但补充边界测试仍有失败。

**测试结果**

| 测试范围 | 结果 | 说明 |
| --- | --- | --- |
| SDK 正常测试与原 QA | 447 通过，0 失败 | 正常测试 337 项，原 QA 110 项 |
| 第二轮新增边界测试 | 11 通过，13 失败 | 共 24 项；失败对应下面的 5 类缺陷 |
| 修复前版本对照 | 2 通过 | 同一空 usage 对象场景在 `ec1c93d` 通过，在本版失败，确认 R02 的空对象分支是新回归 |
| Ruff lint / format | 通过 | 静态检查通过不代表功能缺陷已解决 |

13 个失败用例包含不同参数和存储后端组合，不能当作 13 个独立缺陷。

**待修复缺陷**

以下 R01–R05 均为本轮已复现、待修复的问题；编号与[详细复审报告](REVIEW_REPORT.md)一致。

| 编号 | 级别 | 问题 | 触发条件与实际影响 | 失败用例数 |
| --- | --- | --- | --- | --- |
| R01 | P1 | 无 owner 收束期间未续租 | 原 Runtime 关闭后，新 Runtime 只 attach；撤权耗时 0.35 秒、租约 0.15 秒时反复 `LEASE_LOST`。Run 停留在 `stopping`，没有终态，同 Session 后续任务被 `SESSION_BUSY` 阻塞。显式 stop 和 deadline、两种后端均复现 | 4 |
| R02 | P2 | 后续空 usage 覆盖已知统计 | Chat SSE 先给出 token 计数 `(3, 2)`，后续传入 `{}` 或计数均为 null 的对象，最终统计变成 null。两种后端均复现；其中 `{}` 分支确认由本次修改引入 | 4 |
| R03 | P2 | Responses 顶层 error 漏映射 | 顶层 `code="context_length_exceeded"` 返回 `MODEL_PROVIDER_ERROR / business`，没有按合同返回 `MODEL_CONTEXT_LENGTH_EXCEEDED / validation`。两后端均复现；未观察到错误重试 | 2 |
| R04 | P2 | 预制 Skill 正文校验遗漏 | 相同正文中的动态 shell 标记或专属变量，目录导入会拒绝，直接构造 `SkillPackage` 后却可装配并读取。没有观察到 shell 执行或权限扩大 | 2 |
| R05 | P2 | token 累计越界污染内存状态 | 两次合法单次计数累计为 `2**53` 后，内存后端无法序列化终态，Run 卡在 `running` 并阻塞会话。文件后端能回滚并返回失败终态；这是极限值测试发现 | 1 |

**代码定位与修复验收要求**

| 编号 | 代码位置（审核版本行号） | 修复后必须满足 |
| --- | --- | --- |
| R01 | [runtime/control.py](../src/agentloom/runtime/control.py)，第 131–144 行；`finalize_run` 第 51–55 行 | 慢撤权期间保持有效执行权；成功撤权后提交唯一终态、释放票据和会话；失权时仍应正确拒绝旧 owner 提交 |
| R02 | [models/openai.py](../src/agentloom/models/openai.py)，第 117–122 行；[models/material.py](../src/agentloom/models/material.py) 的 `normalize_usage` | 后续缺失／空统计不清除已知计数；合法的 0 仍被保留；null、空对象和 null 计数字段均覆盖 |
| R03 | [models/provider_errors.py](../src/agentloom/models/provider_errors.py)，第 8–10 行；[models/openai.py](../src/agentloom/models/openai.py)，第 212–220 行 | 顶层 error 与 `response.failed.response.error` 两种结构均正确映射；不按远端 message 文本猜错误码 |
| R04 | [capabilities/skills.py](../src/agentloom/capabilities/skills.py)，第 43–53、65–68、131–151 行 | 两种 Skill 准备路径都在使用边界拒绝不支持的正文语义，返回 `SKILL_PROFILE_UNSUPPORTED` |
| R05 | [runtime/calls.py](../src/agentloom/runtime/calls.py)，第 480–483 行；[configuration/json_values.py](../src/agentloom/configuration/json_values.py)，第 15–18 行 | 累计值保持可持久化；异常统计可按明确合同失败，但不能遗留活动 Run；内存与文件后端保持一致的约束 |

建议先处理 R01，再处理 R02–R05。新增用例同时保留了正常对照：快速撤权、`usage=null`、嵌套错误、累计值恰为安全整数上界。

**上轮缺陷的复测状态**

| 上轮编号 | 当前确认结果 | 本轮仍需关注 |
| --- | --- | --- |
| AUD-01、02、03、05、06、08、09、11、12 | 原触发场景及现有扩展回归通过 | 未在这些原场景下复现失败 |
| AUD-04 | 非法 usage 类型已能按协议错误收束 | 空统计保留与累计上界见 R02、R05 |
| AUD-07 | 两种构造路径均拒绝不支持的 metadata 字段 | 正文语法遗漏见 R04 |
| AUD-10 | HTTP 400 与嵌套 Responses 错误映射通过 | 顶层 error 分支见 R03 |
| AUD-13 | 正常撤权下，无 owner 的截止可以收束 | 慢撤权仍失败，见 R01 |

“原场景通过”只表示对应复现和断言已通过，不表示该模块的所有边界均已验收。

**尚未完成的功能**

以下沿用首次报告的 GAP 编号，作为功能交付缺口单独跟踪，不计入 R01–R05。

| 编号 | 未完成范围 |
| --- | --- |
| GAP-01 | 独立 ContentStore／BlobStore、ArtifactRef 输入、来源撤销、显式外部结果授权；非空 artifacts 仍明确拒绝 |
| GAP-02 | 完整可靠 EventBus、独立 outbox／订阅 ACK、持久等待和准入记录、原生 Node 扩展 |
| GAP-03 | 全部 Context／Environment 事件路由、生命周期修改 Hook、ScriptHookHandler；部分合同仍需细化 |
| GAP-04 | 完整机器 Schema、查询／内容授权 DTO、结构化 OutputValidation／ValidationIssue |
| GAP-05 | MCP resources／templates／prompts 和 OAuth 凭据生命周期 |
| GAP-06 | OCI 隔离、分布式环境、多机 writer 撤权、MCP 工作区挂载或短期令牌 |
| GAP-07 | 增量日志／索引布局、历史格式迁移、handler 实现与客户端服务地址的完整恢复指纹 |

这些差异的依据见[当前实现记录](../../docs/sdk/implementation.md)和[首次测试报告](TEST_REPORT.md)。Workflow、默认工具及其 SPI 仍在本次范围之外。

**复现入口与证据**

- [详细复审报告](REVIEW_REPORT.md)：逐项复现步骤、原因、设计依据和影响边界。
- [本轮新增测试](test_sdk_rereview.py)：24 项边界及对照用例，保留正常失败断言。
- [原有回归日志](evidence/review-220ebd4/regression.txt)：447 项通过。
- [新增边界测试日志](evidence/review-220ebd4/followup.txt)：13 失败、11 通过。
- [修复前对照日志](evidence/review-220ebd4/before-empty-usage.txt)：确认空 usage 对象回归。
- [版本及运行摘要](evidence/review-220ebd4/summary.json)：环境、测试文件摘要、命令和失败用例名称。

在仓库根目录复测：

```sh
.venv/bin/python -m pytest -q agentloom-sdk/tests agentloom-sdk/qa/test_sdk_audit.py agentloom-sdk/qa/test_sdk_rereview.py --tb=short
```

修复验收要求：既有 447 项与新增 24 项全部通过，并保留原始失败证据。测试范围为 macOS arm64、Python 3.12.14、本地模拟 HTTP 及内存／文件存储；真实付费模型、远程 MCP、多机／NFS、断电和跨平台测试未在本轮执行。
