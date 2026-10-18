# SDK 第七轮复测结果

审核日期：2026-10-18，Asia/Shanghai。审核对象：`2b47ea96111326b3e13d5a0f808583bcf50cc8b4` 加上当前工作区未提交修改，包括 LangChain 模式 Prompt 适配、最终输出校验重构、异常附加结果校验及测试修正。范围仍为 SDK，排除 Workflow、默认工具及其 SPI、Platform。

**结论：已按当前代码重新执行测试，共 1359 项通过，0 失败、0 跳过。第六轮发现的两类缺陷在原复现用例中均已修复，本轮新增边界和恢复检查未发现新的可复现功能缺陷。**

前六轮的 6 个 QA 文件与上轮最终快照逐字节一致，包括第六轮 114 项测试；没有通过修改这些断言消除失败。第六轮报告中的“42 个失败”属于旧代码快照，当前全部转为通过。旧报告和原始证据保留，新结果以本文件为准。

## 1. 当前版本的独立测试结果

| 检查 | 结果 | 范围 |
| --- | --- | --- |
| 当前 SDK 与前六轮 QA 完整回归 | **1317 通过**，313.69 秒 | SDK 897 项、前六轮 QA 420 项；包含白盒探针，不排除用例 |
| 第七轮新增边界／恢复测试 | **42 通过**，4.25 秒 | 三种角色、双后端、极端候选、空值纠错及接续 |
| 第六轮原测试 | **114/114 通过** | 包含于 1317 项，不重复计数；上次为 72 通过、42 失败 |
| 原独立白盒探针 | **26/26 通过** | 包含于 SDK 897 项；4 处测试构造／断言已由当前代码版本修正 |
| 模式 Prompt 专项 | **67/67 通过** | 组装 34 项、恢复 28 项、LangChain 适配 5 项；包含于 1317 项 |
| 安装包源码与资源 | **96 个文件逐字节一致** | 离线构建 wheel；角色模块、NOTICE、许可证、来源清单及 py.typed 均存在 |
| 隔离安装目录运行 | **4/4 场景通过** | ReAct／空 Executor 表的 Plan × 内存／文件存储；非法输出修正为 JSON null |
| v1 旧检查点 → v2 安装包 | **2/2 真实进程退出恢复通过** | 根任务创建前、子任务创建前退出；保存的旧 Prompt 保持不变 |
| Ruff lint / format | **通过** | src、tests、七轮 QA 共 149 个 Python 文件，无排除文件 |
| 源码一致性 | **通过** | 测试前后 175 个既有文件摘要及 Git HEAD 一致，`git diff --check` 通过 |

1317 项完整回归在新增第七轮测试前收集执行，随后单独执行新增 42 项，总计 1359 个 pytest 用例。安装包的 6 个运行／迁移场景单列，不加到 pytest 数量中。

环境：Python 3.12.14、macOS 26.7 arm64、pytest 8.4.2，递归限制保持默认 1000。本轮未修改生产代码、既有测试或旧报告；只新增第七轮测试、报告与证据。

## 2. 第六轮问题的当前状态

| 原编号 | 当前实现与复测结论 |
| --- | --- |
| R6-01 非法最终 JSON 候选跳过纠错 | **原 30 个失败全部通过。** [runtime/output.py](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/runtime/output.py:9) 将候选解析／规范化错误转换为可纠正的 RequestError，Schema 配置错误仍单独传播。NaN、溢出、安全整数边界、非法 Unicode 和 129 层数组均能进入格式纠错 |
| R6-02 异常附加结果破坏 Attempt 提交 | **原 12 个失败全部通过。** [runtime/completion.py](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/runtime/completion.py:44) 对 tool_result 复用完整结果合同，并验证 invalid_tool_result 包装。空列表、空对象和字符串不再导致 Run 停留在 running |
| 第六轮正常对照 | **原 72 项继续通过。** 包括合法空值／深度边界、普通格式纠错、两种错误报告路径、合法附加材料及模式指令 |
| 白盒测试误报 | 当前文件已修正压缩退出计数、Operation 总数、缺失 run_id 和停止时序；26 项全部通过，之前的 Ruff 问题也已消除 |

因此，旧 QA 用例仍然适用；变化的是当前实现已满足这些断言。第六轮的两类问题可以按本轮已覆盖场景关闭，不应继续作为当前版本的未修复缺陷。

## 3. 本轮新增的 42 项检查

### 3.1 极端候选纠错为合法空值

共 24 项：ReAct、Planner、Executor × 内存／文件后端 × 4 组输入。

| 第一次模型回复 | 第二次合法回复 | 验证内容 |
| --- | --- | --- |
| 1100 层数组 | `null` | 解析递归错误可纠正，合法 null 可交付 |
| 5000 位整数 | `0` | 超长／超范围整数不会绕过边界或中断运行，0 不被当成空值丢弃 |
| `-Infinity` | `false` | 非有限数值被纠正，布尔 false 保持原类型 |
| `{} {}` | `""` | 多余 JSON 文本被纠正，Schema 允许的空字符串可交付 |

所有场景均保留原候选，追加且只追加一条格式反馈，创建两次业务模型 Operation，每个 Operation 只有一个 Attempt。开启传输重试后也没有把格式纠错误作传输重试；根角色在仅有两次 Loop 额度时完成。最终持久化状态通过恢复一致性校验。

测试入口：[极端候选与空值纠错](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round7.py:108)。

### 3.2 纠错记录已保存后的中断恢复

共 6 项，覆盖三种角色和双后端。在格式反馈已提交后，注入提交观察回调异常，关闭当前 RuntimeEngine，再重新打开并接续同一 Run。同时模拟安装模板版本变化。

结果均为：不重发已完成的首次模型请求、不重复追加格式反馈，第二次请求完成纠错；原 Run 继续使用已保存的 Prompt，后续新 Run 使用新模板。Executor 子任务的纠错也能通过父级委派恢复。

这些用例使用提交观察回调异常模拟推进中断；实际进程退出的验证另列于第 4 节。

测试入口：[已保存纠错的接续](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round7.py:151)。

### 3.3 没有输出 Schema 时保留普通文本

共 12 项，覆盖三种角色、双后端及两种文本：`NaN` 和 1100 层 JSON 形状的字符串。没有 output_schema 时，它们仍是普通最终文本，只调用一次模型，不被新增 JSON 纠错逻辑误解析或拒绝。

测试入口：[非结构化输出对照](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round7.py:215)。

## 4. 新 Prompt 与安装包恢复验证

当前模板版本为 `agentloom-langchain-modes-v2`。角色内容分布于 [react.py](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/prompts/roles/react.py)、[planner.py](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/prompts/roles/planner.py)、[executor.py](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/prompts/roles/executor.py)；[protocol.py](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/prompts/protocol.py) 约定原生工具调用与结束规则，[assembly.py](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/prompts/assembly.py:16) 按角色及 Schema 组装。

现有专项测试确认：文本中的 Action／Observation 等标签不会被当成真实工具调用；Executor 失败后，Planner 能收到真实错误，并在脚本化模型指示下继续委派。空 Executor 表使用本地执行指令；角色隔离、Hook 保护、预算、压缩和保存后的 Prompt 合同继续通过回归。

安装验证在临时目录复制源码并离线构建 wheel，避免改写仓库的包元数据。导入路径显式指向 wheel 解包目录，并核对实际 `agentloom.__file__`，确保测试的是打包结果。96 个源码／资源文件与当前工作区的 SHA-256 全部一致。

旧版接续使用第六轮保存的补丁和基线恢复旧源码，并核对其中 **86 个 Python 文件**与第六轮摘要一致。由旧源码实际生成 `agentloom-modes-v1` 检查点，分别在根任务创建前、Executor 创建前执行 `os._exit(75)`，再由当前 v2 安装包接管：

| 检查点 | 结果 |
| --- | --- |
| 根任务创建前退出 | 接续成功，Planner／Executor 均采用保存的 v1 指令 |
| Executor 创建前退出 | 接续成功，已完成的 Planner 决策没有重发，尚未创建的 Executor 使用 v1 指令 |

两个场景实际模型角色序列均为 Planner → Executor → Planner，Loop 消耗均为 3；新进程退出码为 0，stderr 为空。没有手工改写检查点中的 Prompt。

证据：[安装与迁移结果](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round7-20261018/installed-results.json)、[wheel 文件摘要](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round7-20261018/wheel-manifest.json)、[旧源码重建核对](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round7-20261018/legacy-reconstruction.json)。

## 5. 尚未完成的设计功能

完整回归通过不代表全部目标设计已经实现。根据[当前实现状态](/Users/bmilk/code/project/agent-loom/docs/sdk/implementation.md:9)及[完整目标差异](/Users/bmilk/code/project/agent-loom/docs/sdk/implementation.md:412)，以下仍独立跟踪，不记作本轮新增运行缺陷：

| 编号 | 尚未完成 |
| --- | --- |
| GAP-01 | 独立 ContentStore／BlobStore、ArtifactRef 输入、来源撤销与显式外部结果授权 |
| GAP-02 | 完整可靠 EventBus、独立 outbox／订阅 ACK、全部持久等待／准入记录及通用原生 Node 扩展端口 |
| GAP-03 | 全部 Context／Environment 事件路由、生命周期修改 Hook、ScriptHookHandler |
| GAP-04 | 完整机器 Schema、查询／分页／回执／内容授权 DTO、OutputValidation／ValidationIssue 目标接口；sessions.inputs 仍无分页 |
| GAP-05 | MCP resources／templates／prompts、OAuth 凭据生命周期和完整连接分区合同 |
| GAP-06 | OCI 隔离、分布式环境、多机 writer 撤权、MCP 受限工作区挂载／短期授权令牌 |
| GAP-07 | 增量日志／索引布局、任意历史格式迁移、handler 实现与客户端服务地址的完整恢复指纹 |
| GAP-08 | 统一 ModelAdapter.discover；模型上下文窗口等规格仍由调用方明确提供 |

GAP-08 是对当前文档已披露能力缺口的补列，不是本轮改动新引入的回归。Workflow、默认工具及其 SPI、Platform 继续排除。

## 6. 验证边界与复测命令

本轮使用本地 ScriptedModel、httpx.MockTransport、已有本地 MCP 进程／会话测试及真实本地进程恢复，没有使用真实 LLM key、远端 MCP 密钥或付费模型。结论覆盖代码合同与测试场景；真实模型的规划质量、提示词遵循率、线上鉴权／网络／限流、跨机协调、断电与规模性能仍未完成验收。

当前交付目录可统一运行全部 1359 项：

```sh
.venv/bin/python -m pytest -q agentloom-sdk/tests agentloom-sdk/qa
```

仅复测上轮问题和本轮新增边界：

```sh
.venv/bin/python -m pytest -q agentloom-sdk/qa/test_sdk_review_round6.py agentloom-sdk/qa/test_sdk_review_round7.py
```

- [第七轮新增测试](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round7.py)
- [1317 项完整回归日志](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round7-20261018/regression.txt)
- [42 项新增测试日志](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round7-20261018/boundaries.txt)
- [版本、命令、用例和环境摘要](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round7-20261018/summary.json)
- [测试前源码摘要](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round7-20261018/source-before.json)
- [测试后源码摘要](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round7-20261018/source-after.json)
- [本轮工作区差异快照](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round7-20261018/reviewed-source.diff)
- [保留的第六轮历史报告](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/REVIEW_RESULTS_2026-10-18_ROUND6.md)
