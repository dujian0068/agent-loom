# Platform 配置、资源与发布

状态：详细设计，尚未实现。平台保存管理实体和可重建的配置，SDK 只接收解析后的值与运行依赖。执行默认值的唯一规范见 [SDK 行为约定](../sdk/requirements.md)。

## 1. 访问与应用

Space 包含应用和共享资源；应用对有权限的成员可见。Session 固定绑定一个应用及发起人，不因应用共享自动开放个人聊天历史。API Token 固定 actor、space 和授权范围，不隐含管理员权限。

| 操作 | viewer | editor | admin |
| --- | --- | --- | --- |
| 查看已发布应用、资源元信息 | 是 | 是 | 是 |
| 使用应用、管理自己的会话 | 是 | 是 | 是 |
| 编辑草稿、导入资源、发布 | 否 | 是 | 是 |
| 查看他人私有会话、停止他人执行 | 否 | 否 | 是，并记录审计 |
| 删除应用、成员和凭据管理 | 否 | 否 | 是 |

创建 Agent 时选择 react 或 plan_executor，后续不能改变 mode；需要另一模式则创建另一个应用。创建 Workflow 使用独立应用类型，进入节点编排界面，不把它冒充 Agent 的第三种内部角色。

## 2. 配置 DTO

HTTP 使用 snake_case；结构拒绝未知字段，明确声明的任意业务 JSON 除外。客户端不能指定管理实体 ID、物理文件根、SDK lease。草稿 revision 用 CAS，资源版本和 Release 不可变；Secret 单独保存。

```text
AgentDraft
  model: ModelBindingDraft | null
  roles: RoleDraft[]
  execution: ExecutionPolicy
  context: ContextPolicy
  environment: EnvironmentBindingDraft
  hooks: HookBindingDraft[]
```

创建事务分配 application_id、稳定 file_root_id 及 revision=1 草稿。文件目录延迟准备；不因一次文件系统失败留下半个管理实体。name 去首尾空白后 1..100，description 0..2000；名称不作身份。mode 和 file_root_id 不出现在可编辑草稿体中。

PUT draft 整体替换可编辑字段，If-Match 指定旧 revision。草稿允许尚未完整，但类型、范围及内部角色引用必须合法；缺模型等必需项在 validate 阶段报告。

RoleDraft 包含 role_key、kind、name、description、prompt、variables、input_schema?、output_schema?、capabilities。ReAct 一主角色，无 Executor；Plan 一 Planner，可有多个专业 Executor。Executor 的职责说明供模型选择，Prompt 独立，不开放模型选择。

Prompt 支持 inline 或已固定资源版本；变量声明 name/type/required/default/source，支持 input 或 fixed。仅进行纯文本占位替换，不执行 Jinja/Python，不递归展开变量值。JSON Schema 采用自包含定义，禁止远程引用。

模型字段为 connection_ref、credential_slot_id、model_id、think、context_window_tokens、max_output_tokens、metadata_source。供应商地址及 API／Coding Plan profile 由连接管理提供，用户选模型后带入已核验元信息；获取不到则显式填写。UI 使用 K（1K=1024），API 存整数 tokens，不用猜测窗口伪装自动识别。

凭据只由 CredentialsService 读写；模型 SDK 收到 client／credential resolver。更新 Key 不改变模型身份或 endpoint；新物理 Attempt 可解析已授权的新密钥，旧请求不换 Key。明文不进入定义、Run、检查点或公开事件。

## 3. 资源目录与导入

```text
ResourceVersion
  resource_id, version, kind, content_ref, manifest_ref, content_hash
  source, resolved_commit?, created_at
CapabilityBindingDraft
  binding_key, kind, resource_ref, member_key?, visible_name, config, authorization
```

共享资源有自己的生命周期，不归某一个 Agent 所有；Agent 删除不级联删除共享模型连接、Skill、MCP。撤销资源后平台拦截新的装配，并通过公共 SDK 控制接口停止受影响 Run。

上传文件夹或 Git 导入先产生 ImportJob，实际获取和校验完成后才提交 ResourceVersion。Git 固定 commit SHA，不用运行中的分支名指代资源；失败暂存包不进入可选目录。

- Skill：查找 SKILL.md，校验元数据及目录清单，拒绝越界软链接、设备文件、绝对路径；保存 scripts/references/assets，不在导入时运行脚本。SDK 再验证其支持的 profile。
- Tool：保存输入输出 Schema 和受信执行配置；不允许 HTTP 用户随意指定服务器 Python import。平台托管函数和外部标准接口最终都装配为 CapabilityAdapter。
- MCP：连接、认证、transport、发现的工具 Schema 独立保存；discovery 是管理操作，普通 Run 不自动查询最新工具再改变已发布目录。
- Hook：保存时机、matcher、顺序、配置及处理器定位；可信函数／隔离脚本绑定到 SDK HookAdapter，不获得管理数据库写入权。
- 模型：连接和元信息是管理资源，SDK ModelAdapter 负责协议请求、完整响应和错误归一。
- 知识文件：上传、格式与读取范围由目录管理；文本解析、检索或文件读取作为已绑定能力提供，不能把上传等同于自动具备完整 RAG。

资源名不能覆盖系统委派前缀；同角色可见能力名冲突在 validate 阶段拒绝。

## 4. 验证与发布步骤

validate 按 DTO → Prompt／Schema → 资源与授权 → SDK 纯构造／图编译 → 环境配置顺序返回所有诊断，字段包含 code/path/message；按 path/code 排序。不得为验证调用模型、执行脚本、启沙箱或修改草稿。

发布流程的权威规则见[存储与生命周期](storage-lifecycle.md)：

1. 解析草稿、固定资源版本、SDK 定义及工作流依赖；保存候选不可变 Release 数据。
2. 平台事务 CAS 校验 revision、权限与资源状态，建立 publishing gate、ReleaseJob 和幂等回执。
3. 通过 RunBinding／DependencyBinding 找到依赖旧配置的根 Run，调用 SDK stop；不持管理数据库锁等待。
4. SDK 收束并报告终态后，平台事务切换当前 Release 并清除 gate；失败保留诊断与可重入 Job。
5. 同一 Session 的下一提问采用新 Release；已有 Run 的恢复仍用原配置。SDK 不负责查版本或比较发布指纹。

WorkflowRelease 固定节点绑定及被调用 Agent 的 Release。被调用 Agent 的发布不会自动改写 Workflow。装配一个 Workflow 时解析完整依赖闭包，在 SDK 执行前提供所有可执行 Agent，外层运行中不回查目录。

## 5. 配置如何进入 SDK

管理配置可以包含用户、空间、发布和资源版本；AssemblyService 必须先解析它们。转换结果是 AgentDefinition／GraphDefinition、角色绑定、model client、CapabilityRegistry、HookBinding、环境 scope 和 Runtime adapters。

SDK 不接收一个 application_id 后自行查数据库，也不导入平台 ORM。具体映射、跨存储接入及故障恢复见[SDK 装配](sdk-integration.md)。
