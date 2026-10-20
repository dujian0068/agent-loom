# Platform 配置、资源与发布

更新：2026-10-20。状态：待实现设计。平台将已授权配置装配为 LangChain Agent／LangGraph 图，不再使用 agentloom-sdk 定义、策略或运行接口。类型及集成边界见[能力接入](capabilities.md)。

## 1. 访问与应用

Space 包含应用和共享资源；Session 固定绑定一个应用及发起人，应用共享不开放个人聊天历史。API Token 固定 actor、space 和作用域，不隐含管理员权限。

| 操作 | viewer | editor | admin |
| --- | --- | --- | --- |
| 查看与使用已发布应用、管理自己的会话 | 是 | 是 | 是 |
| 查看已授权资源目录、绑定资源、编辑及发布应用 | 否 | 是 | 是 |
| 注册 Tool、导入 Skill、添加及发现 MCP | 否 | 否 | 是 |
| 查看或停止他人私有会话 | 否 | 否 | 显式管理入口并记录审计 |
| 删除应用、成员和凭据管理 | 否 | 否 | 是 |

创建时在同一表单填写 name、description、mode；名称必填，description 可选，mode 必选且不预选。API mode 为 react 或 plan_executor，页面显示 ReAct／Plan，创建后不能改变。Workflow 是后续独立应用类型，首版不开放。

## 2. 配置 DTO

HTTP 使用 snake_case，拒绝未知字段，明确声明的业务 JSON 除外。客户端不能指定管理 ID、物理文件根、graph_thread_id、执行 owner 或检查点。草稿 revision 用 CAS，ResourceVersion 和 Release 不可变；Secret 单独保存。

```text
AgentDraft
  model: ModelBindingDraft | null
  definition: AgentDefinitionDraft
  executors: Mapping[string, AgentDefinitionDraft]  # ReAct 必须为空
  execution: ExecutionPolicyDraft
  context: ContextPolicyDraft
  environment: EnvironmentBindingDraft
  middleware: MiddlewareBindingDraft[]

AgentDefinitionDraft
  name, description, prompt, variables
  input_schema?, output_schema?
  capabilities: CapabilityBindingDraft[]

EnvironmentBindingDraft
  sandbox_enabled, sandbox_template_ref?
  storage_ref, workspace_access             # read_only | read_write
  retention                                # run | session
```

这些都是平台 DTO，不是上游类名。创建事务分配 app_id、稳定 file_root_id 及 revision=1 草稿。文件目录延迟准备；name 去首尾空白后 1..100，description 0..2000；名称不作身份。mode 和 file_root_id 不出现在可编辑草稿体中。

PUT draft 整体替换可编辑字段，If-Match 指定旧 revision。草稿允许尚未完整，但类型、范围及引用必须合法；缺模型等项在 validate 报告。ReAct 使用 definition；Plan 的 definition 供 Planner 使用，executors 按稳定 key 保存专业 Executor。Executor 共享应用模型与执行策略，Prompt、职责和能力分别配置，通用默认执行器关闭。

Executor key 使用 `[A-Za-z_][A-Za-z0-9_-]{0,63}`，保留 planner、main 和 system 前缀供系统使用；显示名不作 key。重命名须同步更新引用，原始 JSON 重复映射键必须拒绝。角色能力名不能覆盖系统工具名，不能把各角色资源取并集后统一传入。

Prompt 使用 inline 或固定资源版本；变量声明 name/type/required/default/source，source 为 input 或 fixed。仅进行文本占位替换，不执行 Jinja/Python，不递归展开变量值。JSON Schema 使用自包含定义，禁止远程引用。

模型字段保留 connection_ref、credential_slot_id、model_id、think、context_window_tokens、max_output_tokens、metadata_source；provider adapter 只接收该模型支持的参数，不把不支持的 think 静默传给所有供应商。地址及协议由连接配置提供；元信息无法核验时要求显式填写。UI 的 K 为 1024 tokens，API 保存整数。

凭据只由 CredentialsService 解析，快照保存引用；客户端和认证头在 Worker 构造。密钥不进入图 state、messages、checkpoints、公开事件或 Tool 参数。Key 轮换不改变模型与 endpoint，不能在一次进行中的远端请求中更换认证。

### 2.1 执行与上下文策略

平台自有 ExecutionPolicy 包含模型调用额度、工具调用额度、总期限、并行开关、Executor 并发、委派上限、单次调用超时及分类重试；ContextPolicy 包含历史选取、模型窗口和压缩策略。部署方提供版本化默认 profile，发布时解析成完整数值并显示给编辑者，不依赖上游隐式默认值。

具体默认数值、范围及计费单位须在 P0 Schema 中固定；缺失有效上限阻止发布。LangGraph recursion_limit 是图步数保护，不能等同于模型调用次数。根 Run 共享预算，子图不能各自重置；恢复沿用原 deadline 和已记录消耗。副作用未知的工具不适用通用自动重试。

### 2.2 后续 Workflow 配置

Workflow 将采用独立的 graph_definition、node_bindings、输入输出／状态 Schema、受控条件及映射。节点可引用完整 Agent 发布、Tool／MCP 或受信函数；不直接引用另一个 Agent 的私有 Executor。它需要独立的发布依赖闭包、输入与恢复合同，首版不提供画布或可调用 API；不能因为使用 LangGraph 就宣称该产品已经实现。

## 3. 资源目录与导入

Tool、Skill、MCP 分别管理，按固定版本加入应用角色。MCP 固定 connection version、member_key、名称及 Schema，开启连接但未选成员不能发布。资源类型及上游支持详见[能力类型](capabilities.md)。

```text
ResourceVersion
  resource_id, version, kind, content_ref, manifest_ref, content_hash
  source, resolved_commit?, status, created_at
CapabilityBindingDraft
  binding_key, kind, resource_ref, member_key?, visible_name, config, authorization
MiddlewareBindingDraft
  registry_key, implementation_version, order, config, failure_policy
```

共享资源不归应用独占；撤销资源后平台阻止新装配，并保存受影响活动 Run 的停止命令。允许保留恢复和必要清理依赖，不继续执行已撤销的业务操作。

- **Tool：**保存受信 registry_key、实现摘要、Schema、超时和副作用策略；通过 LangChain 工具接口调用，不接受用户指定任意服务器 import。
- **Skill：**导入校验 SKILL.md、元信息、资源清单及基础 profile，拒绝越界链接、设备文件和绝对路径；Git 解析到 commit SHA，包固定摘要。不在导入时运行脚本。未支持的权限／执行扩展返回 SKILL_PROFILE_UNSUPPORTED，不能删字段后假装兼容。
- **MCP：**管理员保存连接及凭据引用，再显式 discovery 并选择成员，生成就绪版本；运行不自动扩展到新发现工具。远端 Schema 漂移需明确失败或发布新版本。
- **Middleware／Hook：**选择后端受信注册项；系统授权、控制和预算逻辑不可由用户关闭，失败时阻止新动作。仅非关键观测允许降级。不能把旧 SDK HookSet 直接序列化到 LangChain。
- **模型与环境：**连接、凭据、能力上限、存储和沙箱模板由管理员管理；执行实现由对应 provider／环境适配器负责。
- **知识内容：**后续上传、解析、索引及检索需独立生命周期；上传文件不等于拥有完整知识库。

资源导入先建立 ImportJob，获取、校验和内容落盘后才提交就绪 ResourceVersion。失败暂存包不出现在 Agent 可选目录。新增版本不修改已有草稿绑定或发布，编辑者须显式切换。

### 3.1 沙箱与工作空间

sandbox_enabled、workspace_access 和 retention 三者独立。平台分配应用／会话／Run 工作区；文件工具和脚本通过同一授权视图读写。Skill 按角色只读挂载，不能将所有角色包挂给每个 Executor。检查点、平台凭据与管理文件不在工作目录内。

只读工作区与写工具冲突、exec 缺少沙箱、环境模板不可用等可保留在未完成草稿，但 validate 和 publish 必须报错。普通文件路径限制不构成进程隔离；环境服务未接入时不开放 exec。保留文件不保留容器进程或内存。

## 4. 验证与发布步骤

validate 按 DTO → Prompt／Schema → 资源与授权 → 无外部调用的图装配／编译 → 环境依赖返回诊断，字段为 code/path/message，按 path/code 排序。使用无副作用模型／工具桩及内存 checkpointer 做构造检查；不得调用模型、MCP discovery、脚本或启动沙箱。连接测试和资源导入是明确的管理操作。

1. 固定草稿、资源、策略、图及实现版本，生成候选不可变 ExecutionSnapshot。
2. 短事务 CAS 校验 revision、权限和资源状态，建立 publishing gate、ReleaseJob 和幂等回执。
3. 关闭旧版本 Run 的准入，取消未启动任务并保存活动 Run 的停止命令；不持锁等待执行或网络。
4. 确认终态与必要清理后，切换当前 Release 并清除 gate；失败保留原任务和固定候选版本。
5. 下一次用户提问使用新 Release；旧 Run 恢复继续使用原图和快照，不将旧检查点直接装入新图。

候选版本不受发布过程中后续草稿编辑影响。详细顺序见[生命周期](storage-lifecycle.md)。

## 5. 配置如何进入运行层

ReleaseCompiler 保存可重建的值，AssemblyService 生成本机对象；模型、Tool／MCP 客户端、SkillLoader、中间件、checkpointer 和环境服务不直接序列化进草稿。ReAct 编译为 LangChain Agent；Plan 编译为固定拓扑的 LangGraph 图和按角色装配的子图。

前端只消费平台 DTO 和 HTTP／SSE，不能导入后端执行对象。运行映射、单写者、补充、停止及故障对账见[LangChain / LangGraph 接入](runtime-integration.md)。
