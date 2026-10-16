# SDK 行为约定与默认值

状态：已确认行为与工程设计分列，待实现。每份 AgentDefinition 独立配置；一次 Run 使用构造时固定的配置。平台如何编辑、发布和选择配置见[平台配置](../platform/configuration.md)。

## 1. 产品能力与运行模式

SDK 提供 ReactAgent、PlanAgent 和 Workflow。ReAct 与 Plan 共用 ReactNode 的模型—动作—反馈循环；Plan 增加 Planner 委派和结果汇合。Workflow 是用户显式编排完整 Agent、工具及业务步骤的外层图，两种引擎共用调用、上下文与恢复基础设施。

ReAct 没有业务 Executor。Plan 的普通工具由 Planner 直接调用；Executor 是独立上下文的角色任务，不是每次工具调用的必经包装。通用 Executor 默认关闭，可配置专业 Executor。Executor 继承主模型，不继承 Planner Prompt；各角色可以使用不同 Skill、Tool、MCP，主子共享被授权的本次工作文件。

LLM 计划内容不直接变成任务依赖图。LLM 通过完整响应明确调用工具或委派任务，Runtime 执行结构化动作并汇总结果，下一轮模型继续决策。

## 2. 单 Agent 配置

默认值在 [平台配置](../platform/configuration.md)中显示为用户可编辑字段，SDK 的默认策略为唯一执行依据。

| 字段 | 默认值 | 规则 |
| --- | --- | --- |
| execution.loop_max | 100 | 1..100000；根 Run 共享，含 Planner 和所有 Executor |
| execution.run_timeout_seconds | 3600 | 1..86400；恢复、排队、重试不重新计时 |
| execution.parallel_enabled | false | 工具及委派同批串行；开启后才允许并行 |
| execution.executor_max_parallel | 2 | 1..128；限制一个 Planner 批次的活跃 Executor，非模型全局并发 |
| execution.model_first_output_timeout_seconds | 60 | 1..3600；首有效输出后撤销首输出计时 |
| execution.model_stream_idle_timeout_seconds | null | null 不加流中空闲计时；显式值 1..3600 |
| execution.tool_timeout_seconds | 10 | 1..3600；Tool／MCP／脚本共用 |
| execution.retry.enabled | false | Runtime 自动重试，与 LLM 重新调用分开 |
| execution.retry.max_attempts | 2 | 1..20，包含第一次执行 |
| execution.retry.delay_seconds | 2 | 0..300 |
| context.recent_runs | 2 | 0..100；当前 Run 之外最近 N 次已结束提问 |
| context.trigger_ratio | 0.8 | 0 < r < 1；预算公式见上下文设计 |
| context.output_reserve_tokens | null | 按窗口及已知输出规格推导，可显式设置 |
| context.safety_margin_tokens | null | 默认 max(256, ceil(窗口 × 1%)) |
| context.summary_target_tokens | null | 按剩余输入预算推导，可设置更小上限 |
| context.compaction_prompt_addendum | 空字符串 | 补充默认压缩 Prompt，不替换保护规则 |
| environment.persistent_sandbox | false | 非持久工作文件只存在于当前 Run |

已确认的默认行为包括 Loop 100、Run 3600 秒、并行关闭、Executor 并发 2、首输出 60 秒、工具 10 秒、Runtime 重试关闭及已约定尝试上限／间隔、历史 2 轮和压缩阈值 80%。表中的数值校验上界、idle=null、输出预留／安全边距／摘要目标推导及默认非持久环境属于工程默认方案，不是新增的用户确认记录；其中各上界只作输入校验，不是吞吐保证。

Executor 不单独覆盖模型、Loop、上下文和运行策略；角色独立配置 Prompt、变量、输入输出 Schema 与能力目录。Hook 按时机及 matcher 选择适用角色。压缩模型继承当前模型，无独立选择入口。

## 3. Loop、并行与重试

- 第 100 次合法 Loop 可正常完成；需要第 101 次时返回 LOOP_LIMIT_EXCEEDED，不额外调用模型总结。
- 同一逻辑模型调用的自动重试共用一张 LoopTicket；被接受的完整决策算一次，正式进入后续上下文的失败反馈也算一次，内部未反馈的连接尝试不重复扣额。
- 压缩不占业务 Loop，但受总截止、停止和连接重试策略控制。
- 批次成员全部 settled 后，按原调用顺序一次进入父角色上下文；一项失败不取消其他并行成员。用户停止、总超时等根控制除外。
- 自动重试仅由结构化错误及执行阶段决定，不通过猜错误文本，也不以工具声明“可重试／幂等”作为必需门槛。不能识别或重试耗尽时交模型作业务决策；模型本身最终不可用则结束对应执行。
- LLM 再次调用即新 Operation；Runtime 重试同 Operation 新增 Attempt。不能凭参数相同判断它们是同一业务动作。副作用防重由工具提供方配合，不承诺外部恰好一次。

具体票据、并发边界和最后一次调用算法见[Runtime](runtime.md)。

## 4. 上下文与用户输入

Session 的连续提问使用不同 Run；各 Run 的角色上下文独立。当前问题及补充原样保护。历史最近 N 次保留根角色实际过程及结束状态，失败／停止也计入；更早记录保留所有用户输入、最终结果和未完成状态，不向前补找成功轮次。

Loop 中达到阈值即压缩，不等模型报超窗。压缩使用独立摘要请求和进度，默认 system Prompt 加用户自定义补充；不做额外语义验收。调用失败或压缩后仍超硬窗口时提示错误并结束，不丢弃原问题来凑预算。

正式补充只交给下一次入口角色 Loop，Plan 只交给 Planner，不广播 Executor。已接收但尚未消费的补充转交下次主动提问，不静默删除或自动开始下一 Run。

## 5. Hook、能力与输出

Hook 按时机执行，可在白名单内修改输入输出或拒绝执行。合法修改提交为权威有效上下文，后续消费者、压缩和恢复可见；私有上下文仍按 scope 隔离。Hook 自身失败、超时或非法 patch 不影响主流程继续；合法 deny 生效。必要持久化失败不能当 Hook fail-open 忽略。

Skill 使用 SKILL.md、目录元数据、按需阅读和受限脚本；MCP／Tool 使用标准输入输出及同一调用管道。具体兼容清单见[能力设计](capabilities.md)，不把“参考 Claude Code”理解为已实现全部功能。

模型连接内部流式接收；完整响应后才发布一次调用结果，不逐 token 发 EventBus 事件。单次模型完成不代表 Agent 完成；Run 最终收束后才交付最终答案。结构化输出不合规交模型修正，仍共享总 Loop，无额外修正额度。

## 6. 文件、停止与恢复

持久沙箱保留工作文件；非持久工作文件在整个 Run 收束后删除。会话记录、调用事实及检查点独立于工作文件。Planner 和 Executor 共用授权工作目录，Session 之间由基础文件工具隔离，不依赖 LLM 自觉选路径。

用户动作只有补充与结束。停止关闭新业务入口，已发动作按可撤销能力或安全边界收束，全部 writer 失效后清理；不能在第一个 Executor 停止时删除别人仍在写的目录。

故障接管不是用户暂停恢复：沿用原 Run、任务和调用身份，继续未完成阶段，已完成结果不重做。恢复方式见[检查点](checkpoints.md)；平台只重建执行对象并交同一 run_id。
