# SDK 第八轮复测结果

审核日期：2026-10-18，Asia/Shanghai。代码版本：`e8fd3b25625cfda1096650af5e17ef1a767c43e4`；开始测试时工作区干净。本轮重新执行测试，不沿用上轮的通过结论。

**结论：原有 1537 项测试全部通过，但新增 61 项检查中有 12 项失败，对应 2 类可复现缺陷：同步文件后端调用期间租约失效，以及 glob 对合法等价目录路径漏报。合计 1586 通过、12 失败、0 跳过。** 安装包另有 8 个进程场景通过，不计入 pytest 数量。不能据原有套件全部通过认定当前版本没有问题。

范围为当前 SDK，排除 Workflow、Platform 和尚未实现的远程基础设施。本轮另外纳入已经落地的文件后端 SPI、11 个内置文件操作及其运行时集成。原先“文件存储 SPI 尚未实现”的结论已经过时；Skill 的读取路由仍是 SkillHandler → SkillLoader，不因增加文件 SPI 就自动变成相同的包注册机制。

本轮仅新增测试、报告和证据；没有修改生产代码、既有测试或旧报告。

## 1. 缺陷清单

| 编号 | 优先级 | 缺陷 | 失败用例 |
| --- | --- | --- | --- |
| R8-01 | P1 | 同步 WorkspaceBackend 的 prepare／write 执行期间没有持续续租，合法长调用使自身失去执行权 | 8 |
| R8-02 | P2 | 本地 glob 使用原始 root 字符串裁剪已规范化路径，合法路径别名返回错误的空集合 | 4 |

这两项都是本轮首次记录的问题；本轮没有执行提交二分，因此不将“首次发现”表述为“确定由最新提交引入”。尤其 R8-02 位于此前已有的本地文件实现中。

### R8-01：同步文件调用会阻断续租

**影响：** 后端仍在约定的工具／运行时间内正常执行时，`handle.wait()` 却收到 `LEASE_LOST`。prepare 场景在失败时仍是 `queued`、没有调用模型；write 场景仍是 `running`、只调用过一次模型。该次推进没有交付成功或正常工具失败结果。

复现条件：

1. 配置 `RuntimeEngine(lease_seconds=0.3)`，工具超时 5 秒、Run 超时 15 秒。
2. 注册完整同步文件后端，让 prepare 或 write 模拟 0.7 秒 I/O。
3. 后端在等待期间每隔约 20ms 调用 `context.check_active()`，写入发布前再次检查，遵守协作式执行权合同。
4. 让 ReAct 或 Plan 调用 write，然后结束。

期望：没有竞争接管、没有停止请求、没有达到业务截止时间时，Runtime 持续续租，任务成功。

实际：约一个租约周期后抛出 `LEASE_LOST`。ReAct／Plan × 内存／文件存储 × prepare／write 共 **8/8 失败**。只把租约改成 3 秒、保留其余条件，**8/8 对照通过**。这里缩短租约用于快速复现；缺陷条件是同步调用超过租约，不能简单理解为工具本身超时。

原因与位置：

- [runner.py:205](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/runtime/runner.py:205) 在异步 `_monitor()` 中续租；同步后端执行时，该协程没有机会运行。
- [runner.py:222](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/runtime/runner.py:222) 在创建 monitor 之前同步调用 prepare，该阶段同样没有续租保护。
- [service.py:133](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/workspace/service.py:133) 在当前事件循环线程直接执行后端方法。

这不是“不受支持的异步提供者”或故意忽略取消的后端：[SPI 合同](/Users/bmilk/code/project/agent-loom/docs/sdk/builtin-tools-spi.md:130) 明确要求普通同步方法，并要求长调用检查执行权；后端也没有得到自行续租的公开端口。已有 [LeaseHeartbeat](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/runtime/leases.py:1) 用于同步撤权等阶段，可以作为修复设计的参考。

建议：在 Runtime 层保证 prepare 和同步文件调用期间的持续续租，并保留真实接管、停止和截止时的 fencing。不能靠忽略 `check_active()` 或重新执行未知写入掩盖问题。修复后除本轮用例外，应继续验证真实失权及停止时不会放行后续写入。

复现测试：[test_sdk_review_round8.py:108](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round8.py:108)。原始证据：[boundaries.txt](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round8-20261018/boundaries.txt)。测试仅替换 entry-point 元数据，后端基于现有独立提供者合同样例；实际安装发现另由完整回归和第 4 节验证。

### R8-02：glob 对等价目录路径漏报

**影响：** 同一个实际存在的文件，read、list 正常，glob 却返回 `{"paths": [], "truncated": false}`。Agent 会收到“没有匹配文件”的错误事实。

最小复现：先创建 `sub/a.txt`，再分别调用 `glob("*.txt", root=...)`。

| root | read／list | glob 实际结果 |
| --- | --- | --- |
| `sub` | 正常 | 找到 a.txt |
| `sub//` | 正常 | 找到 a.txt |
| `/work/sub` | 正常 | 找到 a.txt |
| `./sub` | 正常 | **空集合** |
| `sub/.` | 正常 | **空集合** |
| `/work/./sub` | 正常 | **空集合** |
| `/work/sub/.` | 正常 | **空集合** |

原因：[mutations.py:209](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/environment/mutations.py:209) 用原始 `root.rstrip("/") + "/"` 去裁剪 list 已通过 PurePosixPath 规范化的条目。例如条目是 `sub/a.txt`，原始前缀是 `./sub/`，裁剪失败后用 `sub/a.txt` 匹配单层 `*.txt`，得到错误的不匹配。

建议：按已经授权的规范逻辑路径计算相对路径，保持 `/work` 别名、`.` 和重复分隔符的行为一致；不能用放宽路径授权解决搜索问题。

复现测试：[test_sdk_review_round8.py:251](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round8.py:251)。结果 **4 失败、3 通过**，证据：[glob.txt](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round8-20261018/glob.txt)。

## 2. 本次实际执行结果

| 检查 | 结果 | 说明 |
| --- | --- | --- |
| SDK 与前七轮 QA 完整回归 | **1537 通过**，343.47 秒 | SDK 1075 项、既有 QA 462 项；没有排除失败用例 |
| 本轮第一批边界／租约测试 | **46 通过、8 失败**，9.62 秒 | 54 项；包含 R8-01 及其正常对照 |
| 本轮 glob 路径补查 | **3 通过、4 失败**，0.11 秒 | 7 项；只选择新增 glob 用例，另 54 项已在上一批执行 |
| pytest 合计 | **1586 通过、12 失败、0 跳过** | 1598 个不同用例，三批结果相加，没有重复计数 |
| 离线安装包核对 | **108 个文件一致** | SDK 源码、资源与 wheel 安装目录逐字节相同 |
| 安装包独立进程检查 | **8/8 通过** | 见第 4 节，不加入 pytest 数量 |
| Ruff lint／format | **通过** | 当前 src、tests、QA 和 MiniMax 示例；最终检查报告 187 个文件格式通过 |
| MiniMax 示例入口 | **--help 通过** | 没有执行远端请求 |
| 既有源码与测试一致性 | **231 个文件摘要保持一致** | 测试前后 HEAD 相同；`git diff --check` 通过 |

完整回归是在收集本轮新增测试之前启动；新测试分两批追加执行。交付后的统一命令会包含本轮 12 个预期暴露缺陷的失败用例，它们没有被改为 xfail 或 skip。

静态检查采用仓库正常的配置发现方式。证据目录中的 `ruff-lint.txt`／`ruff-format.txt` 是最初强制指定 SDK 配置的诊断，不作为验收结果；有效结果见 `ruff-auto-*` 及最终检查记录。未通过修改源码格式消除诊断。

## 3. 新增检查覆盖的边界

除上述缺陷外，以下本轮新增的 38 项检查全部通过：

- 6 项：空文件、单字符、中文、emoji、混合文本及 64 KiB 读取上限；EOF 返回空字符串和 `next_offset=null`。
- 6 项：UTF-8 字符内部偏移及超出末尾的偏移被拒绝。
- 24 项：read／list／glob／grep 的 limit 分别为 None、false、0、负数、浮点数 1.0、极大整数；都在提供者调用前拒绝，并保留 `not_started` 执行事实。
- 1 项：20 MiB 文件恰好达到大小上限可写；追加多字节字符超限时拒绝，原版本保持不变。
- 1 项：空目录、无匹配搜索、分页期间目录变化、过期 etag 删除；目录变化和版本冲突均被明确识别。

加上 8 个长租约对照和 3 个 glob 路径对照，本轮新测试共 49 项通过。既有套件中的模式 Prompt、最终 JSON 空值／极限值纠错、Hook、双模型协议、恢复绑定类型、公开事件深度、MCP、本地进程退出恢复等继续通过；通过范围仅限相应用例。

## 4. 安装包与真实进程恢复

在临时目录复制当前 SDK 与独立测试提供者源码，使用 `--no-index --no-build-isolation --no-deps` 构建 wheel 并安装到独立目录。新进程确认实际导入路径来自该目录，local 与 test-files 的 entry point 各只有一个。108 个 SDK 源码／资源文件与当前工作区逐字节相同。

ReAct 和带 Executor 的 Plan 各执行以下四个阶段，共 8 个检查：

1. 通过模型工具调用链完成全部 11 个文件操作。
2. 在 write 结果已提交后执行真实 `os._exit(75)`，确认只记录一次初始 write。
3. 用改变后的提供者 options 接续，收到 `RECOVERY_BINDING_MISMATCH`，提供者调用日志不增加。
4. 用原 options 恢复成功，初始 write 不重放；加上 edit／patch 内部条件写，共三个不同 Operation 的写入。

运行脚本：[verify_installation.py](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round8-20261018/verify_installation.py)。结果与完整构建／进程输出：[installation.json](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round8-20261018/installation.json)。这是本地合同提供者，不是生产对象存储或 NFS 驱动。

## 5. 尚未实现的设计功能

下列是目标设计与当前交付范围的差异，不与本轮两项运行缺陷混算；主要依据[当前实现状态](/Users/bmilk/code/project/agent-loom/docs/sdk/implementation.md:9)、[目标差异](/Users/bmilk/code/project/agent-loom/docs/sdk/implementation.md:448)及实际导出／调用路径核对。

| 编号 | 尚未完成的内容 |
| --- | --- |
| GAP-01 | 独立 ContentStore／BlobStore、ArtifactRef 输入、跨工作区来源撤销及外部内容授权；非空 artifacts 当前明确拒绝 |
| GAP-02 | 完整可靠 EventBus、独立 outbox／订阅 ACK、全部持久等待／准入记录及完整 Context／Environment 事件路由 |
| GAP-03 | ScriptHookHandler 和生命周期修改 Hook；当前是调用阶段 request／response Hook |
| GAP-04 | 完整查询／回执／删除／内容授权 DTO 与机器 Schema；sessions.inputs 仍没有分页 |
| GAP-05 | MCP resources／templates／prompts、OAuth 凭据生命周期等完整能力；现有 McpToolAdapter 以 tools 的发现和调用为主 |
| GAP-06 | 完整 EnvironmentProvider／SandboxGateway、OCI、多机 writer 撤权、多后端挂载及真实 NFS／对象存储驱动；文件 WorkspaceBackend 和包注册发现本身已实现 |
| GAP-07 | 本地 grep 的 RE2 正则、目录 move；当前只支持字面搜索和文件移动 |
| GAP-08 | 增量日志／索引存储、跨机协调、任意历史版本迁移；其他注入 handler 实现及客户端地址的完整恢复指纹 |
| GAP-09 | 统一 ModelAdapter.discover；模型上下文窗口等规格仍需调用方提供 |

Workflow / StateGraph / LangGraph、Platform 仍不在本轮验收范围，未据此新增缺陷。完整 Skill 后端接入也不能用本次文件 SPI 通过结果替代验证。

## 6. 复测命令与证据

完整执行当前所有用例：

```sh
.venv/bin/python -m pytest -q -ra agentloom-sdk/tests agentloom-sdk/qa
```

仅复现本轮新增检查：

```sh
.venv/bin/python -m pytest -q -ra agentloom-sdk/qa/test_sdk_review_round8.py
```

该命令在本次代码版本下应有 **49 通过、12 失败**。先修复 R8-01，再修复 R8-02；通过后应重新执行完整回归。

- [本轮汇总与执行命令](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round8-20261018/summary.json)
- [1537 项完整回归日志](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round8-20261018/regression.txt)
- [测试前源码摘要](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round8-20261018/source-before.json)／[测试后源码摘要](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round8-20261018/source-after.json)
- [保留的第七轮报告](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/REVIEW_RESULTS_2026-10-18_ROUND7.md)

本轮没有使用 LLM key，没有调用真实 MiniMax 或其他付费模型。ScriptedModel、HTTP 协议替身、本地 MCP 和本地进程测试验证代码合同；真实模型的规划质量、提示词遵循、线上鉴权／限流、远端 MCP、真实存储介质、断电、多机接管及规模性能仍需单独验收。MiniMax 示例入口能启动不等于已通过真实 LLM 测试。
