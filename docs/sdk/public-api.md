# SDK 公共接口与依赖注入

本页是 Python 接口设计，尚未实现。公开入口不包含平台用户、应用、发布或资源目录 CRUD。完整行为约束见[行为约定](requirements.md)。

## 1. 构造阶段

拟提供三个入口：ReactAgent、PlanAgent、StateGraph。前两个自动构建内部图；StateGraph.compile 返回 CompiledWorkflow。

```python
runtime = Runtime(
    store=runtime_store,          # 默认可以是内存；恢复时注入持久后端
    content_store=content_store,
    environment=environment,
)

agent = PlanAgent(
    definition=agent_definition,
    model=model_client,
    capabilities=capability_registry,
    hooks=hook_bindings,
    runtime=runtime,
)

result = await agent.ainvoke(
    input=user_input,
    session_id=session_id,
    run_id=run_id,
    input_id=input_id,
)
```

示例是接口草案。构造时将参数校验并固定为可执行配置，所有 ID 为 SDK 技术身份；不在构造函数里启动模型请求。Runtime 是依赖装配入口，每个 Run 仍创建独立 RunRuntime、scope、总线和本机等待器。

调用者传入已经解析的模型连接、资源路径／加载器、原始能力适配器、Hook 和可访问工作空间；SDK 不携带一个平台 ID 再回平台查询。凭据可由调用方提供的 resolver 按需取得，但该扩展不能成为 SDK 对平台的内置依赖，也不把明文写入检查点。

## 2. 执行门面

| 入口 | 输入 | 输出和语义 |
| --- | --- | --- |
| agent.ainvoke | input、session_id?、run_id?、input_id? | 等待 RunResult；同 run_id 继续原执行或读取终态 |
| agent.astream | 同上，另含可选事件游标 | RunEvent 异步迭代，事件含 run_id 和单调序号；不是逐 token 流 |
| workflow.ainvoke / astream | 工作流业务状态及上述执行关联 | 使用 Workflow 输出契约；共享 Run 控制与恢复协议 |
| runtime.runs.get | run_id | 不可变 RunSnapshot 或明确不存在 |
| runtime.runs.stop | run_id、command_id、reason | StopReceipt；关闭该稳定 ID 的准入，重复请求幂等，终态不重新启动 |
| runtime.inputs.append | run_id、input_id、content | InputReceipt；由 SDK 处理补充、消费和转交 |
| runtime.sessions.history / inputs | session_id、游标 | 授权范围内的历史或正式输入及投递状态，分页读取 |
| runtime.tasks.list / operations.get | run_id、task游标／operation_id | 已提交只读进度和结果；不能修改执行状态 |
| runtime.events.read | run_id、after_seq、limit | 已提交事件分页，供跨进程订阅或平台 SSE |
| runtime.records.delete | 已收束 run_ids／session_ids、command_id | 删除回执；拒绝活动执行，平台决定何时调用 |

一次性示例可以让 SDK 生成运行 ID。需要故障恢复的调用方应在调用前保存稳定 run_id、input_id，或可靠保存 RunStarted 回执。未传 session_id 的 Agent 调用使用 SDK 分配的一次性会话，不自动复用之前的历史。新的 HTTP request_id 不得替换原 run_id。

平台对以上对象的读取都走公共查询门面。SDK 仍可提供更细的只读 task／operation 视图，但不开放直接修改 continuation、额度或检查点的接口。

### 重入语义

1. Run 不存在：以 input_id 登记初始输入并初始化执行；同一 Session 的活动 Run 约束由执行存储检查。
2. Run 未完成且已有有效 owner：不重复启动；当前调用附着结果或返回明确占用状态。
3. owner 已失效：SDK 取得新 epoch，完成环境撤权验证，加载现场，继续原执行。
4. Run 已终结：返回保存结果／终态错误，不再调用模型。
5. 同一 Run／input_id 重投但初始输入不同：返回 INPUT_CONFLICT，不能将新文字偷偷追加为补充。

调用方保证重入时提供该 Run 原配置。SDK 不选择平台版本、不维护配置版本指纹；只验证恢复载荷格式、引用和执行协议是否可处理。

## 3. 补充输入与结束竞态

InputReceipt 至少包含 input_id、run_id、acceptance、delivery_state、session_seq、commit_ref。

- input_id 重复且内容相同，返回原回执；不同内容报冲突。
- SDK 接收补充时固定正式记录和顺序，Plan 只交给 Planner，ReAct 只交给入口角色。
- 已接收但未进入模型的输入在 Run 结束时标记 carried，下一次新 Run 接入；不自动开始下一轮。
- SDK 发现 Run 已结束且此前未接收该 input_id，返回 RUN_ENDED，不伪造一次成功补充。平台可将该请求作为下一次提问，见[平台接入](../platform/sdk-integration.md)。
- 输入领取、最终请求冻结和终态转交由 SDK 一致提交，不能由平台轮询状态后自行补写消息表。

## 4. 可插拔端口

| 端口 | 最小职责 | 恢复约束 |
| --- | --- | --- |
| ModelAdapter | 流式请求、聚合完整决策、结构化异常 | 不拼接不同 Attempt 的半截结果 |
| CapabilityAdapter | 工具调用、结果归一、外部请求关联 | 已保存原始结果不重发能力调用 |
| SkillLoader | 列目录、读 SKILL.md、读包内资源 | 从调用方提供的稳定只读资源包加载 |
| HookAdapter | 运行有序处理器，返回决定和 patch | 保存决定／修改与游标，恢复不重做已应用修改 |
| RuntimeStore | 原子命令、CAS、执行权、快照／增量、事件 | 不允许只实现各记录独立 save |
| ContentStore | 大内容存取、长度／摘要校验 | 提交引用前内容已持久，孤儿可清理 |
| EnvironmentProvider | 工作视图、受限文件／脚本、撤权、清理 | 活动 Run 不丢文件，旧 owner 不继续写入 |

MemoryRuntimeStore 只能保证本进程存续期间的数据。持久后端的事务、租约与跨进程访问能力必须声明并验证；不以存储产品名称替代能力验证。

## 5. AgentNode 与完整 Agent 调用

高代码 Workflow 装配时传入已经构造好的可执行 Agent 或 PreparedAgentRef。AgentNode 使用 SDK 的内部调用端口保存父 Operation 与子 Invocation 引用，并共享根 Run 控制。

不能在普通节点里无关联地调用一个新根 agent.ainvoke，然后声称已经得到嵌套恢复。平台在执行前解析工作流所引用的发布配置，SDK 外层运行期间不会回查平台应用目录。

## 6. 结果和事件

RunResult 至少包含 run_id、status、output?、error?、usage、started_at、ended_at。错误可以伴随未完成材料，但不得当作正常最终答案。

RunEvent 至少包含 event_id、run_id、seq、type、payload、committed_at。进度、批次和终态是低频事件；模型连接内部流式接收，完整模型响应不必然等于最终回答。最终回答随 Run 终态交付。

关闭平台 SSE 或 SDK 的观察流只解除订阅，不表示停止 Run。主动停止走 stop；宿主进程关闭则释放执行依赖并保留未完成现场，下一进程可继续。具体接口的任务生命周期须在 SDK 测试中验证。

## 7. SDK 不负责的事情

用户认证、空间授权、草稿、发布、模型 Key 管理页面、Git 导入 UI、机器选择、应用删除策略和 HTTP 路由均在 Platform。SDK 仍必须执行已注入的能力范围、文件路径和运行边界检查，不能因为不认识用户就让 LLM 任意指定 scope。

## 8. 提交前停止的竞态

调用方可能已经分配 run_id，但 SDK 尚未创建 Run。runs.stop 必须在该 Runtime namespace 下保存不可绕过的 RunAdmissionControl，再返回 admission_closed=true；这不需要伪造配置、消息或执行结果。

Run 初始化与 stop 争用同一 admission key：stop 先提交则后到的 ainvoke 返回 RUN_ADMISSION_CLOSED，不执行；初始化先提交则 stop 关联原 Run，按正常 stopping 流程收束。控制回执只来自受信 SDK 控制门面，LLM／Hook 没有该权限。记录是技术取消事实，不是平台发布屏障。

这使平台关闭分派后，即使旧 Worker 已通过检查、稍后才调用 SDK，也不能重新启动被取消的 ID。删除正文后保留最小无内容关闭标记，直到调用方确认相应分派已彻底撤销；不会因为普通 records.delete 而恢复该 ID 的准入。
