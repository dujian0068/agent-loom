# SDK 当前实现与验证

日期：2026-10-17。实现版本：`agentloom-sdk 0.1.0a1`。本页记录本轮实际交付；其他 SDK 章节保留完整目标设计，不能据此推定全部接口已实现。Workflow 按当前开发范围暂不接入，也没有恢复已删除的示例目录。

## 已实现的执行链路

- 独立安装与导入，`ReactAgent`、`PlanAgent`、`AgentNode` 门面、`RuntimeEngine` 和 `RunHandle`；调用方注入模型、能力和工作区，默认文件存储可替换，不导入 Platform。
- 每次委派独立的 ReactAgent、任务与上下文；根级 Loop / deadline 共享，按配置串行或并行执行，批次全部结算后按模型给出的次序反馈。
- 冻结配置、JSON Schema、声明式 Prompt 变量、原始／有效结果分离、有序 Hook、合法拒绝、无效 patch 放行、受限且可过期的读取端口。
- 最近 N 轮实际根角色历史、补充输入的领取／送达／跨轮转交、input_id 去重、受保护输入、预算计量与可接续的分块压缩。
- Chat Completions / Responses 流式适配器；完成证据、首输出／空闲超时、独立 Attempt、完整响应后一次逻辑后置 Hook。Responses 保存用于无状态续传的 output items，包括 encrypted reasoning 与 assistant phase，参照 [OpenAI 官方说明](https://developers.openai.com/api/docs/guides/reasoning)。
- 显式工具注册与角色绑定、MCP 官方客户端的 stdio / Streamable HTTP 连接、调用前目录一致性校验、Skill manifest 与参数渲染、文件能力、大结果预览及受限分片读取。
- 默认 JSON 文件后端、显式选择的内存后端、幂等提交回执、租约 epoch fencing、独立进程恢复；已保存 raw / Hook result / 子任务 / 批次继续原阶段，没有可靠外部结果的调用标记 unknown。文件后端采用 namespace 文件锁、不可变记录和原子清单，不需要数据库。
- 独立停止命令与停止前准入关闭，宿主关闭保留现场；终态与 CleanupJob 原子提交，清理独立领取租约、失败重试，不再次激活 Agent。

源码按 `public`、`configuration`、`runtime`、`events`、`context`、`capabilities`、`environment`、`models`、`storage` 分包。安装、完整构造参数、调用和能力绑定见 [SDK README](../../agentloom-sdk/README.md)。

## 本轮逐项核对及修正

以[行为约定](requirements.md)、[装配职责](assembly.md)、[Agent 执行](runtime.md)、[调用阶段](event-bus.md)、[上下文](context.md)和[能力合同](capabilities.md)为依据；以下是已经改动并验证的偏差，不代表完整验收矩阵全部完成。

| 核对项 | 发现的偏差与本轮结果 |
| --- | --- |
| 默认存储与所有权 | `RuntimeEngine()` 原来默认内存；现默认 `.agentloom/state`，移除 SQLite 实现和导出。Runtime 只关闭自己创建的存储，显式注入对象仍归调用方管理 |
| Agent 与图引擎职责 | 原循环集中在图引擎，PlanAgent 没有暴露 Planner 现场；现由独立 `AgentLoop` 随角色 ReactAgent 激活，图引擎负责绑定和激活；根 `agent.context` 提供所属角色的只读消息、修订和进度 |
| Executor 并发边界 | 原来先创建子任务再等待名额；现先取得名额，再原子提交目标和许可；等待中的委派不会提前创建子任务，子任务完成即释放名额，后置 Hook 仍参与全批结算 |
| 纯准备与实际尝试 | 模型给出不存在的能力或不合 Schema 的参数，原来可能进入 Attempt；现先本地校验并持久化反馈，不创建 Operation、Attempt，也不调用 Hook 或适配器 |
| Loop 票据 | 根模型最终失败且没有接受决策时释放票据；Executor 模型失败反馈按一次 Loop 计量，before deny 同样占反馈额度，但不创建执行许可或 Attempt |
| Hook 的有效修改 | 工具／委派输出修改重新检查输出 Schema；模型工具名或参数修改重新检查当前角色的能力与参数。非法 patch 保留原有效结果并记录诊断，合法 deny 仍生效 |
| 首输出与补充送达 | 完整响应帧先验证协议和调用结构，再确认首有效输出；损坏的终帧不会虚假标记补充已经送达 |
| 根控制与压缩 | Executor 的压缩失败或上下文硬窗口错误终止根执行；终态提交时重查总截止，避免任务先完成、提交延迟后仍报成功；超时配置下界统一为设计的 1 秒 |
| 恢复绑定 | 工作区根目录进入恢复清单；换目录后接续非终态执行会在创建新工作区前被拒绝 |
| 能力边界 | Skill 列表返回角色可见别名；MCP 重复分页游标明确报错；目录缩小时旧游标返回 `DIRECTORY_CHANGED`，不误判为普通偏移非法 |
| 文件读取开销 | 子路径读取只复制所需字段，清单校验直接校验持久化字节，避免每次状态查询重复复制整个运行记录或重编码清单；文件仍是持久化依据 |

## 审查回归修复

审查提供的 7 个场景分别在内存和文件后端执行，修复前 **14 failed**，修复后 **14 passed**。这些用例已纳入 `test_review_regressions.py`；另外补充了并发、恢复、控制截止和连续转交的边界验证。以下六项属于已实现链路的缺陷，与文末尚未实施的目标设计分开记录；Workflow 不在此次修复范围内。

| 审查项 | 修复与断言 |
| --- | --- |
| 工具 Hook 改参后历史仍旧 | Operation 保存来源 call_id；Hook 同一提交更新有效请求和对应 assistant 调用槽，保留 raw。并行修改不同槽位互不覆盖；Hook result／apply 后中断再接续不重跑 Hook；Responses 续传同样使用新参数 |
| Hook 修正来源后仍用旧摘要 | 每次业务模型前置 Hook 应用后，按当前来源 hash 重新物化消息和 record_ids。来源已变的摘要不再用于本次请求，后续 Hook 也读到新的请求映射 |
| 尝试超时不重试 | 首有效输出、流中空闲和工具尝试超时按 RetryPolicy 创建新 Attempt，复用原 Operation、冻结请求和 Loop 票据；前后 Hook 各运行一次，保留前次不完整材料。根截止及未确定的崩溃结果不因此重试 |
| 额度耗尽仍先压缩 | 新业务调用在前置处理前检查额度，压缩准备及每个块发送前再次检查。额度被其他调用预占时等待结算；实际耗尽后不调用摘要模型，也不禁止最后一轮已接受决策的工具执行 |
| 超窗 Hook 修改未放行 | 在提交候选前完整计量不可压缩记录、工具 Schema 和模型参数。固定材料超窗则拒绝本项全部修改、保存诊断，后续 Hook／模型继续看到原值；可压缩结果变长仍走正常压缩 |
| 转交补充排在新问题后 | 输入按 session_seq 保存和投影，根任务统一在 prepare 中插入保护输入。结束转交、初始化和历史排序不再依赖 inputs[0] 是初始问题；跨两轮转交保持 ID、顺序、送达状态及去重 |

## 基于 ef31404 的复审修复

本轮覆盖复审提出的 3 项 P2 行为偏差和 2 项 P3 编码规范问题；不扩展 Workflow 或文末尚未实现的目标能力。按 [阿里巴巴开发规范](https://alibaba.github.io/Alibaba-Java-Coding-Guidelines/)中适用于 Python 的通用要求补充接口契约，并按职责组织常量。

| 复审项 | 修复与验证边界 |
| --- | --- |
| 撤权异常中止维护 | 停止收束、终态清理按 Run 捕获异常；两类扫描各自隔离发现阶段故障。撤权失败保留 stopping，不生成虚假终态；维护诊断与下次重试时间写入文件／内存后端，重启后仍生效，成功恢复保留最近错误。领取租约时原子复查重试条件，避免使用旧扫描结果提前重试；存储不可用时记录脱敏诊断并继续扫描 |
| 非法委派 Hook patch 被接受 | 原始调用、Hook 修改和子任务启动共用 validate_action，完整校验参数 Schema、无 Schema 时非空文本、变量和目标权限；非法 patch 连同 context_patch 整体丢弃，后续 Hook、Executor 与有效调用记录继续使用原值。显式 JSON Schema 允许的 null 仍合法；非法补丁不撤销合法 deny |
| HTTP 429 忽略 Retry-After | Chat / Responses 共用 HTTP 层解析秒数和 HTTP 日期，将服务端绝对时间纳入结构化错误；按 max(配置间隔, 服务端剩余等待) 保存 next_attempt_at。恢复不重算完整等待、不提前发起 Attempt；保留同一 Operation 和一次前后 Hook，停止／总截止照常阻止新发送 |
| 公开 API 契约不足 | start、ainvoke、astream 和 RunHandle 的观察／控制方法补充参数、返回、异常与实例绑定约定，区分本地等待超时、Run 截止和停止，说明流提前结束不会取消业务执行 |
| 重复大小字面量 | environment.limits 管理完整文件和读取分片大小，工具 Schema 与实际 I/O 共用；models.limits 管理模型响应大小，HTTP 与 ModelHandler 共用；Hook 的 result.read 限制引用 context.results 的同一预览常量 |

上述复审阶段新增 `test_followup_review.py` 的 54 项测试覆盖两种后端、双 HTTP 协议、重启接续与非法输入边界；当时只单独运行 QA 中委派及限流问题对应的 14 项。其余 QA 问题的后续修复见下一节，历史报告继续保留原版本的复现事实。

## 系统测试异常修复

本次以 `ec1c93d` 为基线，处理 QA 复评确认的 11 类问题，以及 MCP 的 `Retry-After` 遗漏；原 AUD-01 和模型路径 AUD-09 的修复继续回归。Workflow 和文末目标设计缺口仍不纳入这次修复。

| QA 项 | 实现与验证边界 |
| --- | --- |
| AUD-02／03：Schema 引用与字面数据 | `configuration.schemas` 使用离线引用注册表，按 Schema 位置和资源作用域解析本地引用，支持 anchor、递归引用、嵌入资源及转义路径。字面数据与属性名 `$ref` 不误判；无效引用在装配完成前拒绝，运行期 Schema 故障归一为 INVALID_SCHEMA，不进入模型格式纠正循环。JSON 冻结与规范化独立为 `configuration.json_values` |
| AUD-04：异常 usage | `models.material` 在完整响应接收边界验证 usage，非法结构归 protocol / MODEL_PROTOCOL_ERROR 并形成持久失败；缺失统计保持未知。Chat／Responses 使用同一统计归一入口，持久汇总另有类型保护，终态释放票据和 Session |
| AUD-05／06：公开参数 | `public.validation` 统一校验明确身份和分页参数，命令在写入前拒绝 None、空白或错误类型，不以任意 str 转换制造幂等键；事件读取先校验再比较游标或访问存储 |
| AUD-07：Skill 构造绕过 | `capabilities.skill_profile` 共用 metadata 校验，直接准备包与目录导入均拒绝未支持字段和错误布尔类型；使用现有角色授权，不将 Skill 文本变成新增权限 |
| AUD-08：错误工具输出证据 | 输出 Schema 失败保留已经返回的完整 JSON，包括 null、Unicode 和外部凭证；失败状态与 completed 执行事实同时保存，后置 Hook 可读原材料，重开不重新执行工具 |
| AUD-09：MCP 等待遗漏 | MCP 的 HTTP 错误和异常组保留 Retry-After，由已有统一调用驱动保存 next_attempt_at；秒数／日期／配置下界、Runtime 重开、停止与根截止均有回归 |
| AUD-10：超窗错误分类 | `models.provider_errors` 按明确供应商 code 映射 MODEL_CONTEXT_LENGTH_EXCEEDED；HTTP 错误体有读取上限，不凭远端 message 猜测，也不为该错误重试模型 |
| AUD-11：中断材料 | 文本、reasoning 和不完整调用按 Attempt 保存，带提供方索引／ID；重试不混合，取消在有效租约下保留材料。非文本材料同样计入大小上限，超限保留限额内片段并标截断；未完成调用永不执行 |
| AUD-12：清理续租 | `runtime.cleanup_lease` 对固定 Run／Job／worker／epoch 独立续期，验证到期与接管；耗时文件操作在事务外执行，旧 owner 不可续活或提交。关闭宿主时等待本机清理线程收束，清理进度不改变业务终态 |
| AUD-13：无 owner 截止 | `runtime.deadlines` 使用存储时间提交关闭准入和首个控制原因，再由已有停止服务领取有效执行权、撤权和收束；queued、失去 owner、进程退出均不要求重建 Agent。截止控制与慢清理分开扫描，后到的停止／截止不覆盖首个已保存原因 |

QA 的 Schema 用例调整为验证完整装配边界，并通过单独故障注入验证运行期兜底；usage 的错误映射写入能力合同，测试额外断言唯一终态、Operation 收束、票据释放及同 Session 可继续。原始失败日志和评估记录不改写。

## 验证环境与证据

- 初始实现基于 `5e32dd2b8027ccb3bea0c651495747dd6741121a`；上轮实现与六项修复已提交为 `ef31404d71f4abbf3d9c9db80ddf25b06dcf6183`，本轮复审修复以此为基线。
- 环境：macOS 26.7 / arm64、Python 3.12.14；项目 `.venv`，默认本地 JSON 文件目录；只有显式内存场景使用内存后端。
- 独立进程测试以 `os._exit(73)` 在真实提交回执后退出，再启动第二进程；持久化后端不是 mock。测试会断言任务和操作数量、调用次数、Hook 次数、Loop 用量、终态和清理状态。
- 模型协议使用 `httpx.MockTransport` 与分片 SSE 字节流；MCP stdio 使用真实子进程及官方客户端。没有真实付费模型调用、远程 MCP 服务或跨机器测试。

| 测试文件 | 已覆盖的主要行为 |
| --- | --- |
| `test_execution_contract.py` | ReAct、Plan 隔离、串并行、全批结算、重入、100 次边界、格式纠正、重试、停止、等待取消、超时、补充、构造校验、并行 token 计量 |
| `test_hook_context_contract.py` | Hook 顺序、raw 保留、读端口过期、deny 与非法 patch、压缩预算及保护输入、Skill 读取和 manifest |
| `test_persistence_contract.py` | raw 保存、Hook result、Hook apply、子任务完成、批次消费、终态等 6 个进程退出切点；双宿主互斥和关闭重入 |
| `test_file_store_contract.py` | 文件记录同步／清单发布前后退出、独立进程并发提交、锁等待与 namespace 隔离、损坏拒绝、回执确认丢失、垃圾回收 |
| `test_control_and_history.py` | 历史不递归膨胀、类型变量、JSON null、无 Agent 的停止收束、恢复不重置截止、清理重试、提交回执丢失、删除墓碑和依赖 |
| `test_model_protocols.py` | 分片参数、完整结束证据、后置 Hook 次数、HTTP 分类、Responses reasoning / phase 与有效文本续传、非有限 JSON 参数 |
| `test_mcp_protocol.py` | 实际 stdio 初始化、工具目录、调用元数据、业务错误不自动重试、Schema 漂移拒绝调用 |
| `test_environment_contract.py` | etag 冲突、精确 patch、移动／删除、越根及符号链接、epoch 撤销、持久卷、搜索顺序与完整文本 |
| `test_result_and_scope_contract.py` | 64 KiB 有效预览、分片读取、Executor 私有引用拒绝、目录游标、UTF-8 边界、过期 owner 的自动接管 |
| `test_design_alignment.py` | 默认文件重开与资源所有权、Planner 只读现场、并发准入、无 Attempt 的本地拒绝、失败计票、Hook 校验、首输出送达、恢复绑定、压缩终止、最终截止与目录／MCP 游标 |
| `test_review_regressions.py` | 审查的 14 次双后端复现，加并行改参、Hook 保存／应用后接续、超窗修改原子拒绝、可压缩修改、idle 重试、根截止、预占额度等待、连续补充转交，共 34 项 |
| `test_followup_review.py` | 撤权和维护异常隔离、持久诊断及重启退避，委派非法参数整体放行与合法 Schema／deny，Chat／Responses 的 Retry-After、配置间隔、非法值、重启时间和根控制，共 54 项 |
| `test_schema_contract.py` | Schema 结构位置、本地与递归引用、anchor／资源作用域、运行期失败归一及离线边界 |
| `test_public_validation.py` | 命令身份拒绝无副作用、UUID／显式键一致性、非法游标先于存储读取拒绝 |
| `test_model_material.py` | usage 协议边界、Chat／Responses 中断材料隔离、停止和大小限制、错误工具输出持久保留、精确超窗错误分类 |
| `test_maintenance_contract.py` | 双宿主清理互斥与续租、旧 epoch／到期拒绝、慢清理与截止隔离、撤权重试、首个控制原因和真实进程退出后的无 Agent 收束 |
| `test_mcp_retry_contract.py` | HTTP 异常组、秒数／日期／配置间隔、持久等待重开、停止与截止 |
| `test_skill_profile_contract.py` | 两种 Skill 构造路径共享字段限制及不可变 metadata |
| `qa/test_sdk_audit.py` | 原 110 项系统边界用例，包含本次修订的装配和持久终态断言 |

运行命令：

```sh
.venv/bin/python -m pytest -q agentloom-sdk/tests agentloom-sdk/qa/test_sdk_audit.py
.venv/bin/ruff check agentloom-sdk/src agentloom-sdk/tests agentloom-sdk/qa
.venv/bin/ruff format --check agentloom-sdk/src agentloom-sdk/tests agentloom-sdk/qa
.venv/bin/python -m pip wheel ./agentloom-sdk --no-build-isolation --no-deps --no-index -w /tmp/agentloom-wheels
npm run docs:build
npm run docs:check
```

上轮（`ef31404`）验证结果：

| 检查 | 实际结果 |
| --- | --- |
| 完整 SDK 测试 | **148 passed in 132.94s**，包含审查回归与扩展的 34 项、默认文件后端的第 100 次完成／第 101 次拒绝、6 个任务崩溃切点及文件提交的 4 个退出切点 |
| Ruff lint / format | 通过，62 个 Python 文件格式一致 |
| 安装包构建 | 离线构建 `agentloom_sdk-0.1.0a1-py3-none-any.whl` 成功，检查包内容不含 SQLite 实现或测试文件，包含 `py.typed` |
| wheel 独立导入 | 仓库外两个独立 Python 进程直接从 wheel 导入；第一个使用 `RuntimeEngine()` 的默认文件存储完成执行，第二个重开同 run_id 读取原结果，模型未重调，未加载 SQLite 或 Platform |
| 文档构建 | 通过；打包器提示部分前端 chunk 大于 500 kB，不影响构建成功 |
| 文档链接 | 56 个 HTML 页面、1240 条内部链接、430 个锚点、239 个资源引用检查通过 |

本轮复审修复验证结果（2026-10-17）：

| 检查 | 实际结果 |
| --- | --- |
| 完整 SDK 测试 | **202 passed in 155.77s**；上轮 148 项及新增 54 项全部通过 |
| 定向复测 | **68 passed in 23.86s**；新增 54 项加已有 QA 中本轮相关的 14 项，另 96 项 QA 未在本命令执行 |
| Ruff lint / format | 通过，66 个 Python 文件格式一致 |
| 安装包与进程隔离 | 离线 wheel 构建成功；仓库外两个独立进程从该 wheel 导入，默认文件后端执行与重开成功，未再次调用模型，未加载 SQLite／Platform |
| 文档 | 构建成功；56 个页面、1241 条内部链接、431 个锚点、239 个资源引用检查通过；仍有前端 chunk 大于 500 kB 的非阻断提示 |

系统测试修复验证结果（`ec1c93d` 后的工作区，2026-10-17）：

| 检查 | 实际结果 |
| --- | --- |
| 完整 SDK 与 QA 回归 | **447 passed in 186.44s**：原正常套件 202 项、新增专项回归 135 项、调整断言后的 QA 110 项 |
| Ruff lint / format | 通过，85 个 Python 文件格式一致，包含 QA 和历史探针 |
| 安装包 | 离线 wheel 构建、独立目录安装成功；八个新增职责模块均打入包，包含 py.typed，不包含测试／QA／SQLite 实现 |
| 文件持久化重开 | 仓库外两个独立进程从已安装 wheel 导入；默认 FileRuntimeStore 首次执行模型 1 次，重开同 run_id 调用模型 0 次 |

逐项修复和原始验证日志见[QA 修复报告](../../agentloom-sdk/qa/FIX_REPORT.md)。测试覆盖的 HTTP 服务使用 MockTransport，MCP 限流在生产适配器的 session 边界注入 HTTP 异常；没有将其表述为真实远程 MCP 或供应商联调。

## 与完整目标设计的差异

当前交付采用明确的单机存储／环境实现范围：

1. 默认 JSON 文件后端保存原子执行快照和幂等命令回执，按 namespace 串行提交，清单包含该 namespace 当前所有记录引用；尚非目标中的增量日志／索引布局，大运行记录的写入和查询有规模开销。已完成进程退出恢复验证，未验证断电或 NFS／跨机协调，也不提供任意历史版本迁移。PostgreSQL 只是可选参考适配器，不是 SDK 的依赖要求。
2. LocalEnvironment 只 fence 经 SDK 文件服务的访问，文件锁不约束任意宿主代码。没有 OCI 执行服务、多机卷撤销证明、NFS mutation journal；脚本适配器需要调用方另行提供配套的隔离环境和网关，默认拒绝本地宿主执行。[默认工具与 SPI](builtin-tools-spi.md)是 2026-10-17 新增设计，其中独立 WorkspaceBackend Protocol、后端能力装配校验以及共享文件／对象存储实现仍待实施。公共路径仍依赖本地实现，脚本适配也仍读取环境内部属性；当前分包不能代替这些接口合同，SPI01–SPI10 不包含在上述已通过测试的结论中。
3. 大结果完整值保存在执行快照，已有预览、有效 hash、按角色受限的 `result.read`；尚无独立 BlobStore、ArtifactRef 导入、跨工作区来源撤销和显式外部结果授权。非空 `AgentInput.artifacts` 明确拒绝。
4. 本地文件 grep 当前为字面搜索，未提供 RE2；目录 move 暂不支持。未知外部副作用不被重试成假成功；工作文件变更与运行状态提交是两个动作，即使使用同一文件系统，也不宣称二者原子。
5. EventBus 已有单处理者路由、局部注册和调用阶段持久化，但尚非目标中的完整可靠总线：缺少独立 outbox 订阅确认协议和全部持久等待／准入记录，本机推进仍使用协程、信号量和轮询。Context／Environment 的全部业务事件路由、生命周期修改 Hook，以及完整机器 Schema、查询／内容授权 DTO 仍待补齐；公开查询目前返回 JSON 投影。
6. 恢复清单固定定义、策略、Schema、模型规格、Skill manifest、Hook 绑定与工作区根目录，但没有对注入的 Python handler 实现、客户端配置／服务地址做完整版本指纹。接续方仍须重建相同实现和资源；记录删除要求先移除后续历史依赖。
7. Workflow / StateGraph / LangGraph 按本轮范围暂不接入；容器驱动、跨机存储、真实付费模型、真实远程 MCP 均未作为本轮验收通过项。

这些边界不以静默降级方式隐藏；其余目标继续以 [SDK 验收矩阵](verification.md) 为准。
