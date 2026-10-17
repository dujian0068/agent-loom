# SDK 系统测试修复结果

日期：2026-10-17。SDK 基线：`ec1c93d`；验证时工作区 HEAD 为 `17353e7`（其间仅有文档主题提交）。本报告对应其后的 SDK 工作区修改。

**复评确认的 11 类问题，以及 MCP 的 Retry-After 遗漏均已修复。完整回归 447 项全部通过，用时 186.44 秒。** 原正常测试 202 项、新增专项回归 135 项和 QA 110 项一起执行；QA 的 Schema 与 usage 断言按设计进行了调整。原始 TEST_REPORT、ASSESSMENT 及旧证据保留历史状态。

## 修复与模块职责

| 项目 | 修复行为 | 负责模块 |
| --- | --- | --- |
| AUD-02：悬空 Schema | 装配时验证本地引用可解析；运行期 Schema 异常归 INVALID_SCHEMA 并收束，不再遗留 running 或进行无效格式纠正 | configuration.schemas |
| AUD-03：字面 `$ref` | 按真实 Schema 位置与资源／anchor 作用域遍历，保留 enum、const、默认值及属性名的原语义 | configuration.schemas、configuration.json_values |
| AUD-04：异常 usage | 在模型响应边界验证对象及计数类型；非法统计形成 protocol / MODEL_PROTOCOL_ERROR，缺失统计仍为未知 | models.material、models.openai；runtime.calls 额外保护持久汇总 |
| AUD-05：空幂等 ID | 写入前拒绝 None、空白及错误类型；停止、补充、删除和运行准入共用身份校验 | public.validation |
| AUD-06：非法游标 | 先校验类型、范围和页长，再读存储或比较游标，返回稳定 SDK 参数错误 | public.validation、runtime.services |
| AUD-07：Skill 绕过 | 已准备包与目录导入共用 metadata 合同，拒绝未支持字段和错误布尔类型 | capabilities.skill_profile |
| AUD-08：工具原始材料丢失 | Schema 失败仍保存工具实际返回的完整 JSON，包括 null 与外部凭证；保持 failed，记录 completed 执行事实，后置 Hook 可读，重开不重发 | runtime.handlers、runtime.calls |
| MCP Retry-After | 从 HTTPStatusError／异常组保留响应头，经统一驱动持久保存等待截止，重开沿用原截止并遵守停止／总截止 | capabilities.mcp、models.http、runtime.calls |
| AUD-10：模型超窗错误 | 有界读取错误体，只按明确 code 映射 MODEL_CONTEXT_LENGTH_EXCEEDED，不凭错误文字判断或重试 | models.provider_errors、models.http |
| AUD-11：中断流证据 | 文本、reasoning、不完整调用按 Attempt 留存；保留索引／ID，停止时在有效租约下保存；限制总大小，禁止混入重试消息或执行工具 | models.material、ProviderEvent、runtime.handlers／calls |
| AUD-12：慢清理无法完成 | 清理独立续租，校验 job／worker／epoch／有效期；旧租约不可续活、不可提交；关闭宿主时收束本机线程 | runtime.cleanup_lease、runtime.cleanup |
| AUD-13：无 owner 截止 | 用存储时间提交停止意图，再走既有领取执行权、撤权及终态路径；保存首个控制原因，独立控制扫描不被慢清理阻塞 | runtime.deadlines、runtime.control、runtime.maintenance |

职责拆分保留现有包结构：Schema 不进入 Runtime 持久事务，模型材料校验不负责调度，截止扫描不构造 Agent，清理租约只授权固定清理任务。默认仍是文件后端，未增加数据库实现；Workflow 和原报告七组 GAP 不属于本轮交付范围。

## 测试断言调整

- AUD-02 构造用例验证完整 Agent 装配边界，允许 Definition 或 Agent 构造拒绝。独立运行用例注入旧的无效 Schema，验证 INVALID_SCHEMA、一次模型调用、唯一持久终态、票据释放和同 Session 可继续，不再因构造拒绝而提前跳过兜底检查。
- AUD-04 的非法 usage 映射已明确写入 docs/sdk/capabilities.md。回归同时验证错误类别、Operation 结束、唯一终态、预算释放及会话继续，不只检查返回错误码。
- 增加本地引用／anchor／递归／嵌入资源、双协议片段归属、失败工具结果经 Hook 与重开保留、双宿主清理、到期／被替换租约拒绝、真实子进程退出、停止／截止竞争，以及 MCP 秒数／日期／配置等待和重开等专项测试。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| 正常 SDK 测试 | 337 passed：原 202 项加新增 135 项 |
| QA | 110 passed；原 AUD-01 和模型 AUD-09 同时回归 |
| 合并完整回归 | 447 passed，186.44 秒，无失败／跳过 |
| Ruff lint / format | 通过；85 个 Python 文件 |
| 离线 wheel | 构建、独立目录安装成功；八个新增模块和 py.typed 在包内，测试／QA／SQLite 不在包内 |
| 两进程默认文件存储 | 首进程成功调用模型 1 次，第二进程重开原结果且模型调用 0 次；均从已安装 wheel 导入 |
| 文档构建与链接 | 构建通过；56 个页面、1241 条内部链接、431 个锚点和 239 个资源引用检查通过；有大于 500 kB 的非阻断 chunk 提示 |

完整命令：

```sh
.venv/bin/python -m pytest -q agentloom-sdk/tests agentloom-sdk/qa/test_sdk_audit.py --tb=short
.venv/bin/ruff check agentloom-sdk/src agentloom-sdk/tests agentloom-sdk/qa
.venv/bin/ruff format --check agentloom-sdk/src agentloom-sdk/tests agentloom-sdk/qa
.venv/bin/python -m pip wheel ./agentloom-sdk --no-build-isolation --no-deps --no-index --no-cache-dir -w /tmp/agentloom-wheels
npm run docs:build
npm run docs:check
```

[完整日志](evidence/fixes-20261017/regression.txt)、[JUnit](evidence/fixes-20261017/regression.junit.xml)、[环境与文件摘要](evidence/fixes-20261017/summary.json)、[Ruff](evidence/fixes-20261017/ruff.txt)、[格式检查](evidence/fixes-20261017/ruff-format.txt)、[wheel 构建](evidence/fixes-20261017/wheel.txt)、[第一进程](evidence/fixes-20261017/smoke-first.json)、[第二进程](evidence/fixes-20261017/smoke-second.json)、[文档构建](evidence/fixes-20261017/docs-build.txt)、[文档链接](evidence/fixes-20261017/docs-check.txt)。

模型 HTTP 使用 MockTransport；MCP 限流在生产 invoke 的外部 session 边界注入错误，真实 stdio MCP 继续由原有回归覆盖。文件恢复含真实进程退出；未执行真实付费模型、远程 MCP、多机／NFS 或断电测试。上述结果证明本次修复及已覆盖场景通过，完整目标设计仍以独立验收矩阵为准。
