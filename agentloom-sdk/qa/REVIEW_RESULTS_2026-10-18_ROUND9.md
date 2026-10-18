# 第九轮审核：复用完整回归，补查文档合同与未覆盖边界

本轮确认 **3 类 P2 功能缺陷**，新增定向测试 **49 项：32 passed、17 failed、0 errors、0 skipped**。17 项失败对应下列 3 个根因，并非 17 个不同缺陷。另外记录 1 项符合现有调用顺序、但容易引起误用的设计风险，以及 9 组仍未完整交付的目标能力。

复用用户提供的 **1665 passed** 完整回归，未重跑全量。原有测试通过与新增边界失败可以同时成立；完整回归结果不能证明尚未编写的测试场景也正确。

**收尾期间发生并发源码修改。** 本轮定向测试于 2026-10-18 22:36:56（Asia/Shanghai）结束，22:38:20 核对既有 195 个文件仍与测试基线一致。22:42:59 收尾快照发现事件总线、运行处理器、提交模块、类型定义有新改动，并新增 nodes／checkpoints 代码。本报告的测试结论和功能缺口以已经验证的 22:38 快照为准，原 1665 项结果不覆盖这些随后出现的改动。收尾时三个缺陷所在的 results.py、mutations.py 仍未变化；新增节点／提交实现尚未验收，不能把这里的旧覆盖结论直接移用。变动清单见 [concurrent-changes.json](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round9-20261018/concurrent-changes.json) 和 [差异快照](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round9-20261018/concurrent-changes.patch)。

## 1. 基线、范围与证据

| 项目 | 本轮口径 |
| --- | --- |
| 审核版本 | `6179c8e7678e43f7ecef7275abcffcf1be3740b0`，开始时工作区干净 |
| 复用结果 | [用户本次完整回归 summary.json](/Users/bmilk/code/project/agent-loom/.agentloom/test-results/20261018-141539-685079Z/summary.json)：1665 passed，0 failed/errors/skipped；pytest 378.88 秒 |
| 适用性校验 | 添加本轮文件前执行 `./scripts/test_all.py --check`，退出码 0，确认源码、测试、配置和运行环境匹配。测试时 HEAD 与当前 HEAD 不同，但文件内容校验通过 |
| 完整性 | 定向测试后 22:38 的完整性校验中，既有 SDK 源码、测试、QA 和 SDK 文档共 195 个文件摘要全部一致。本审核仅新增 QA 文件；随后出现的并发修改单独记录 |
| 新执行范围 | 仅 [test_sdk_review_round9.py](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round9.py)，最终运行 4.71 秒；另用本机 `diff` / `patch --dry-run` 验证补丁样本合法性 |
| 环境 | Python 3.12.14，macOS 26.7 arm64；MemoryRuntimeStore / FileRuntimeStore；默认本地文件后端 |
| LLM | 使用确定性的 ScriptedModel，不使用 API Key，不请求真实 LLM，不消耗模型服务额度 |
| 文档范围 | 对照 `docs/sdk` 的公开接口、装配、能力、上下文、调用／事件、恢复和验收合同；以 implementation.md 和 builtin-tools-spi.md 区分当前交付与目标设计 |
| 排除范围 | Workflow / StateGraph / 平台功能沿用既有范围，不计为本轮新缺陷；已实现的文件工具与 SPI 纳入审核 |

证据入口：[本轮摘要](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round9-20261018/summary.json)、[命令与执行时间](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round9-20261018/command.json)、[完整定向日志](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round9-20261018/pytest.log)、[JUnit](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round9-20261018/junit.xml)、[审核前摘要](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round9-20261018/source-before.json)、[审核后摘要](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round9-20261018/source-after.json)。

## 2. 已确认缺陷

| 编号 | 级别 | 缺陷 | 定向结果 |
| --- | --- | --- | --- |
| R9-01 | P2 | result_read 超过结果末尾时报错，违背“空结果结束分页”的合同 | 8 failed / 8 passed |
| R9-02 | P2 | patch 把正文中的 Unicode 分隔字符当成补丁行边界，拒绝合法补丁 | 4 failed / 2 passed |
| R9-03 | P2 | glob 只规范化 root 和条目路径，未规范化 pattern，合法模式漏报文件 | 5 failed / 2 passed |

### R9-01：result_read 对超末尾 offset 错误拒绝

**依据。** [capabilities.md:133](/Users/bmilk/code/project/agent-loom/docs/sdk/capabilities.md:133) 明确规定：offset 为非负整数，超末尾返回空且 `next_offset=null`。当前入口 Schema 没有要求 offset 必须小于等于结果总大小。

**复现。** 通过普通函数工具返回 70,000 字节 ASCII 或 72,000 字节中文文本；模型收到真实生成的预览和 result_ref 后，调用：

```python
ToolCall("result_read", {"result_ref": ref, "offset": total_bytes + 1})
```

同时覆盖合法 JSON 整数极值场景 `offset=2**52`。两种内容、两种超末尾位置、两个存储后端均返回 `RESULT_SLICE_INVALID`，共 8 项失败。对照组 `offset=0` 和 `offset=total_bytes` 均通过。

**原因与影响。** [context/results.py:16](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/context/results.py:16) 将 `offset <= len(data)` 当作参数合法性的必要条件。正常分页调用方按文档越过末尾时会收到工具失败，可能产生多余的模型纠错轮次。测试中的最终 Run 仍成功，因为 ScriptedModel 在收到反馈后固定结束；该成功不代表 result_read 成功。

**原覆盖缺口。** [test_result_and_scope_contract.py:25](/Users/bmilk/code/project/agent-loom/agentloom-sdk/tests/test_result_and_scope_contract.py:25) 覆盖大结果预览、有效结果替换、内部合法切片和跨角色访问拒绝，未覆盖 result_read 的超末尾行为。文件 read 的超末尾测试属于另一个接口，不能代替此合同。

**修复建议。** 保留类型、非负数、limit 和 UTF-8 边界校验，为 `offset > total_bytes` 明确生成空切片及空下一页游标；补齐并统一 `offset/end_offset` 的超末尾约定。新增用例只强制文档已经明确的空文本与 `next_offset=null`，不擅自规定文档未明确的 end_offset。

### R9-02：合法 UTF-8 单文件补丁被错误分行

**依据。** [UTF-8 文本合同](/Users/bmilk/code/project/agent-loom/docs/sdk/capabilities.md:305) 和 [patch 合同](/Users/bmilk/code/project/agent-loom/docs/sdk/capabilities.md:317) 支持 UTF-8 文本及精确单文件 unified diff，没有禁止正文包含 U+2028、U+2029、U+0085 或垂直制表符。

**复现。** 文件只有一条由 LF 结束的物理行，内容为 `before 中\u2028文 after\n`，对该行执行精确替换，补丁由以下结构组成：

```diff
--- a/unicode.txt
+++ b/unicode.txt
@@ -1 +1 @@
-before <正文中的分隔字符> after
+updated <同一个分隔字符> after
```

U+2028、U+2029、U+0085、U+000B 四组均返回 `PATCH_FORMAT_INVALID`；普通中文和 TAB 对照组通过。失败后原文件内容未改变，本轮未观察到文件损坏。

**独立校验。** 本机 `/usr/bin/diff -u` 生成的字节与测试构造完全一致；`/usr/bin/patch --dry-run -p1` 接受全部四种失败样本及普通中文对照样本，退出码均为 0。证据：[unicode-patch-oracle.json](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round9-20261018/unicode-patch-oracle.json)。因此不能将失败解释为测试传入了非法补丁。

**原因与影响。** [environment/mutations.py:93](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/environment/mutations.py:93) 和同文件第 100 行对补丁及原文使用 `str.splitlines(keepends=True)`。该方法也会按 Unicode 分隔符拆行，导致一条 diff 内容行被拆成多条，其中后续片段没有 `+/-/空格` 前缀。包含此类字符的合法文本不能使用 patch 修改。

**原覆盖缺口。** [test_environment_contract.py:10](/Users/bmilk/code/project/agent-loom/agentloom-sdk/tests/test_environment_contract.py:10) 的补丁样本主要是普通 LF 行；既有 Unicode 读写测试不等于验证了补丁解析的 Unicode 边界。

**修复建议。** 按 unified diff 的物理行规则解析补丁和文件内容，保留正文中的 Unicode 字符；同时保留 CRLF、文件末尾无换行、空文件、精确 hunk 与版本冲突等规则，避免通过宽松匹配掩盖问题。

### R9-03：glob 的 pattern 仍存在路径规范化遗漏

**依据。** [文件 glob 合同](/Users/bmilk/code/project/agent-loom/docs/sdk/capabilities.md:312) 规定使用 glob 路径语法；[文件 SPI 路径规则](/Users/bmilk/code/project/agent-loom/docs/sdk/builtin-tools-spi.md:163) 规定点段与重复分隔符表示同一逻辑位置。当前参数校验允许下列模式。

**复现。** 创建 `sub/a.txt` 和 `sub/nested/b.txt`：

| pattern | 预期 paths | 实际 paths |
| --- | --- | --- |
| `sub/*.txt` | `sub/a.txt` | 正确，对照通过 |
| `./sub/*.txt` | `sub/a.txt` | `[]` |
| `sub//*.txt` | `sub/a.txt` | `[]` |
| `sub/./*.txt` | `sub/a.txt` | `[]` |
| `**/*.txt` | 上述两个文件 | 正确，对照通过 |
| `**//*.txt` | 上述两个文件 | `[]` |
| `**/./*.txt` | 上述两个文件 | `[]` |

同一组样本使用 Python `Path.glob` 得到全部预期结果，见 [glob-pathlib-oracle.json](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round9-20261018/glob-pathlib-oracle.json)。

**原因与影响。** [environment/mutations.py:214](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/environment/mutations.py:214) 将规范化后的条目路径与未经规范化的 `pattern.split("/")` 比较，空段和 `.` 被当成必须匹配的真实目录名。接口返回成功和 `truncated=false`，模型可能据此误判文件不存在。

**原覆盖缺口。** [test_workspace_glob.py:25](/Users/bmilk/code/project/agent-loom/agentloom-sdk/tests/test_workspace_glob.py:25) 系列用例变化的是 root，pattern 固定为 `**/*.txt` 等规范形式。上一轮 R8-02 的 root 修复仍然有效；本轮发现的是同一类规范化逻辑在另一个输入参数上的遗漏。

**修复建议。** 在匹配前规范化 pattern 的普通路径段，保留 `* / ? / [] / **` 的 glob 语义；继续拒绝 `..`、宿主绝对路径和受限系统目录。若未来支持末尾 `/` 的“仅目录”语义，需要单独保留该信息，不能简单删除全部分隔符后改变匹配含义。

## 3. 设计风险：非法 astream 游标不保证任务未启动

**R9-R01，不计入上述缺陷或失败数。** `after_seq=-1 / None / True / 0.5` 都会抛 `EVENT_CURSOR_INVALID`，但新 Run、初始输入、Session 已提交，Agent 已绑定。两个存储后端 × ReAct/Plan × 四种非法值的 16 组探查中，每组在异常后仍完成 **2 次 ScriptedModel 调用和 1 次本地测试工具调用**。

实现位置：[agents.py:158](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/public/agents.py:158) 先 `start()`，随后 [handles.py:68](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/public/handles.py:68) 才检查游标。[public-api.md:183](/Users/bmilk/code/project/agent-loom/docs/sdk/public-api.md:183) 明确描述此顺序，且观察失败／取消不应自动停止已接收的任务，因此不能将它直接报告为违反现行设计。

调用方不能仅凭“流接口抛参数错误”断言没有产生模型费用或工具副作用；未预先保存 run_id 时也不易定位这次已接收任务。建议在 `astream` 接纳新任务前先检查游标类型和非负性，并在文档中明确启动成功后观察失败的语义。不要用“观察报错就自动取消 Run”来改变原有停止合同。

本轮对应测试记录错误与实际接纳事实，不把未经文档承诺的“必须零副作用”当作失败断言。有效游标的 4 组 ReAct/Plan、双后端对照全部通过。

## 4. 仍未完整实现的目标能力

以下是当前代码与目标设计的差距；多数已在实现说明中披露，不能作为本轮新增回归缺陷。此表基于文档与源码核对，不把“存在适配器接口”当作完整功能已交付，也不把未做的验收写成已通过。

| 编号 | 尚缺能力与实际影响 | 当前实现及依据 |
| --- | --- | --- |
| G9-01 | 独立 ContentStore / BlobStore、ArtifactRef 输入、非 inline 结果、跨 scope 来源授权和撤销。无法按完整目标合同导入文件／二进制材料或授权外部结果 | [host.py:203](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/runtime/host.py:203) 拒绝非空 artifacts；[invocation.py:152](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/capabilities/invocation.py:152) 拒绝 artifact 结果。大结果目前仍完整保存在运行快照；[目标合同](/Users/bmilk/code/project/agent-loom/docs/sdk/capabilities.md:125)、[已披露范围](/Users/bmilk/code/project/agent-loom/docs/sdk/implementation.md:474) |
| G9-02 | 可靠事件 outbox、订阅 ACK／重投、完整持久等待与准入账本，以及 Context／Environment 的全部业务事件路由 | [EventBus](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/events/bus.py:31) 当前主要负责冻结注册与单处理者 request；运行快照中的事件和调用阶段恢复已存在，但不是 [可靠订阅合同](/Users/bmilk/code/project/agent-loom/docs/sdk/event-bus.md:610) 的完整实现。原回归不能代替 ACK 前崩溃、订阅背压等未实现路径的验收 |
| G9-03 | ScriptHookHandler、受限脚本 Hook 执行、生命周期修改 Hook | 现有 [FunctionHookHandler](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/events/hooks.py:109) 和有序调用 request／response Hook 可用；[assembly.md:359](/Users/bmilk/code/project/agent-loom/docs/sdk/assembly.md:359) 的脚本接口未完整交付，不能把函数 Hook 的通过结果当作脚本隔离验证 |
| G9-04 | 全部查询／分页／删除任务／回执 DTO、机器 Schema 和内容授权查询合同 | [services.py](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/runtime/services.py:24) 提供 JSON 投影、分页的部分查询和删除命令；sessions.inputs 仍一次返回列表。[public-api.md:302](/Users/bmilk/code/project/agent-loom/docs/sdk/public-api.md:302) 本身也说明查询 DTO 尚需完成，属于待交付设计，不能据此假定类型接口已冻结 |
| G9-05 | 统一 ModelAdapter.discover，实际模型规格／窗口发现 | [ModelAdapter](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/types.py) 只有单次流接口和 descriptor；[OpenAI 适配器](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/models/openai.py:19) 仍要求调用方显式提供窗口。对应 [模型发现设计](/Users/bmilk/code/project/agent-loom/docs/sdk/capabilities.md:15)；手填窗口不是供应商规格验证 |
| G9-06 | 完整 MCP resources/templates/prompts、OAuth 生命周期，以及执行域／挂载视图／工作区令牌授权 | [McpToolAdapter](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/capabilities/mcp.py:12) 当前核心是 list_tools 与 tools/call、目录核对和每 Attempt 会话；stdio 使用本机 stdio_client。[完整 MCP 目标](/Users/bmilk/code/project/agent-loom/docs/sdk/capabilities.md:215) 的资源入口、授权刷新和 SandboxGateway 执行域未由该适配器实现，可信本机 MCP 的成功不能证明完整隔离合同 |
| G9-07 | 完整 EnvironmentProvider / SandboxGateway、OCI 或远端执行服务、执行状态查询／终止／撤权证明、多后端挂载和真实共享／对象存储驱动 | 文件 WorkspaceBackend 的 11 方法、包发现与配置加载**已经实现**；[ScriptToolAdapter](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/capabilities/tools.py:64) 仍依赖外部配套环境与网关，[workspace/runtime.py:45](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/workspace/runtime.py:45) 拒绝尚不兼容的第三方文件后端与脚本组合。见 [当前边界](/Users/bmilk/code/project/agent-loom/docs/sdk/builtin-tools-spi.md:3)；文件 SPI 不是脚本沙箱完整实现 |
| G9-08 | 文件 grep 的 RE2 正则和完整搜索超时 profile、目录 move、生产级 mutation journal | [local.py:361](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/environment/local.py:361) 当前为字面搜索；文件与 Run 状态提交不构成同一原子事务。见 [文件目标合同](/Users/bmilk/code/project/agent-loom/docs/sdk/capabilities.md:308)、[已披露边界](/Users/bmilk/code/project/agent-loom/docs/sdk/implementation.md:475)。这与本轮确认的 glob／patch 缺陷分别处理 |
| G9-09 | 增量日志／索引、多机存储与任意历史版本迁移；非文件提供者的 Python handler／客户端地址等完整恢复指纹 | 当前 [FileRuntimeStore](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/storage/filesystem.py:1) 是 namespace 锁与原子清单／快照，另有内存后端；[recovery_manifest](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/configuration/assembly.py:10) 固定定义、模型描述、Hook 绑定、Skill manifest 和文件提供者，但未固定所有注入实现及客户端配置。见 [实现边界](/Users/bmilk/code/project/agent-loom/docs/sdk/implementation.md:472)；单机恢复通过不等于 NFS／跨机故障接管通过，SQLite/PostgreSQL 也未交付 |

Workflow / StateGraph / LangGraph 按既定范围暂不接入，不另外计为功能遗漏。当前 SDK 已有 ReAct、Planner、Executor 的系统模式提示词，已有 Skill 的三个读取入口和独立 SkillLoader 扩展路径；本轮不把这些已实现能力重复列入缺口。

## 5. 覆盖结论与复跑方式

| 新测试组 | 总数 | 通过 | 失败 | 主要验证点 |
| --- | ---: | ---: | ---: | --- |
| result_read | 16 | 8 | 8 | ASCII／中文；结果内、恰好 EOF、EOF+1、2**52；双存储后端 |
| 非法 astream 游标探查 | 16 | 16 | 0 | 负数、null、布尔、小数；接纳与副作用记录；ReAct/Plan、双后端 |
| 合法 astream 对照 | 4 | 4 | 0 | after_seq=0，接纳到业务终态 |
| UTF-8 patch | 6 | 2 | 4 | 普通中文、TAB，以及四种非 LF 分隔字符 |
| glob pattern | 7 | 2 | 5 | 普通／递归模式、点段、重复斜线 |
| **合计** | **49** | **32** | **17** | **无 skip、xfail 或生产代码替换** |

只复跑本轮定向测试：

```bash
cd /Users/bmilk/code/project/agent-loom
.venv/bin/python -m pytest -q -ra -s --tb=short agentloom-sdk/qa/test_sdk_review_round9.py
```

在本报告验证的代码快照下，上述命令返回非零退出码，因为本轮保留了揭示 3 个缺陷的真实失败断言。并发修改完成后，应基于新快照重新确认受影响场景。修复后可先运行此文件验证，再由调用方按需要运行原统一全量命令。

新增 QA 文件后，`scripts/test_all.py --check` 若提示原结果不再覆盖**现在的完整测试集合**，属于预期：原 1665 项结果没有被覆盖或修改，但它不包含这次新增 49 项。收尾期间新增的源码改动也会让基线检查失效，需要在后续验证中单独处理。应联合使用原完整回归与本报告，不能将两次独立执行写成一次新的全量通过。

过程记录也保留在证据目录：首次测试夹具遗漏默认本地后端要求的 workspace root，36 项尚未进入待测逻辑；修正的仅是新增测试夹具，该次结果不作为 SDK 缺陷。随后 42 项定向执行为 30 passed / 12 failed，再增加 7 项 glob 场景形成最终结果。日志以 `initial-fixture-*`、`before-glob-*` 保存，最终以本报告链接的 `pytest.log/junit.xml/summary.json` 为准。

本轮完成的是源码／文档审核和新边界的离线功能复现，没有进行真实 MiniMax、真实远程 MCP、跨机共享存储、断电恢复或长期负载验收；这些仍需独立场景和实际环境提供证据。
