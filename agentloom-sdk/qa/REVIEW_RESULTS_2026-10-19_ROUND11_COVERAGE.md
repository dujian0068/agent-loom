# 第十一轮增量测试覆盖审核

**结论：新增测试还不齐全。已复用 2020 条全量通过结果，但补充 27 项定向测试后，得到 11 failed、16 passed，对应 3 个 P2 缺陷。** 其中 1 个是本次 Skill 读取重构引入的兼容性回归，另 2 个是新增节点 effect 校验仍未封住的合同缺口。11 个失败实例包含不同参数和两种存储，不能理解为 11 个独立缺陷。

本轮只新增 QA 测试、报告和证据，没有修改 SDK 实现或原有测试。未重跑全量，未使用真实 LLM、API Key 或远程 MCP；模型检查使用 ScriptedModel 和 httpx.MockTransport。没有采集行／分支覆盖率，以下结论来自文档合同、代码检查和实际反例。

## 1. 新提交与证据适用范围

| 项目 | 核对结果 |
| --- | --- |
| 增量起点 | `708bdaf`，此前的并发测试脚本提交 |
| SDK 修复 | `e53229a`：文件／Skill、节点 effect、控制、Responses、MCP 等边界修复 |
| 测试稳定性调整 | `f4a68d5`：按持久租约到期时间等待进程恢复，记录完整回归 |
| 平台设计与原型 | `e7f85c1`：设计文档及浏览器内模拟原型，无可运行平台后端 |
| 审核期间新增提交 | `6c32a32`：新增 `my_create_agent.py`；纳入静态覆盖检查，未执行真实模型调用 |
| 同期文档站提交 | `28b31b9`：VitePress 配置、首页与样式；不改变本轮 SDK 证据，未作页面功能验收 |
| 最后核对 HEAD | `28b31b93dfd496aa5459711d441a41c6c9a22a69` |
| 用户全量基线 | `20261019-145110-713072Z`，2020 passed，4 个进程，pytest 374.01 秒 |
| 全量统计组成 | SDK tests 1402 项、QA 618 项，失败／错误／跳过均为 0 |
| 原证据有效性 | 217 个原清单文件与当前对应文件完全一致，环境一致，JUnit 与摘要一致，运行前后源码一致 |
| 本轮定向测试 | 27 项：11 failed、16 passed、0 errors、0 skipped；最终运行约 11.8 秒 |

原全量结果在 `f4a68d5` 上运行，已包含 `e53229a` 的全部修复。后续平台文档／原型提交不改变这份 SDK 证据。新示例文件和本轮新 QA 文件未包含在原 2020 项中；`--check` 因新增文件提示不匹配，不代表原有 2020 项已经失败。

原始结果：[用户 summary.json](/Users/bmilk/code/project/agent-loom/.agentloom/test-results/20261019-145110-713072Z/summary.json)。原文件完整性：[审核前](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round11-20261019/audit-before.json)、[审核后](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round11-20261019/audit-after.json)。

## 2. 新增测试已经覆盖了什么

从用户 JUnit 按测试文件统计，SDK 修复新增的六个文件共 **125 项，全部已运行通过**：

| 测试文件 | 项数 | 已覆盖的主要合同 | 本轮评价 |
| --- | ---: | --- | --- |
| `test_node_effect_contract.py` | 37 | 缺字段／错误类型、未知操作、外任务引用、5 类非法模型 intent、同次写集回滚、Executor 失败隔离 | 形状和归属覆盖较好；遗漏请求与真实上下文的一致性、重复 effect 组合 |
| `test_control_faults.py` | 16 | stop 早于 start、Hook 停止／截止／普通错误、Run 消失、嵌套事务、输入索引损坏 | 双存储及控制结果有断言，本轮未发现这些已测分支被放宽 |
| `test_workspace_faults.py` | 29 | epoch 缺失／损坏／类型错误、move 补偿与跨设备、换链、非法参数、真实 SIGKILL 暂存清理、目录同步 | 已覆盖多种实际故障；不等于持久 mutation journal 或全部故障交错已实现 |
| `test_skill_files.py` | 12 | 包根／祖先／包内符号链接、20 MiB 精确上限和超限、权限／I/O 错误、换链 | 遗漏 CRLF 导入；读取期间变化原有用例未直接注入，本轮补充 6 项通过 |
| `test_response_shapes.py` | 21 | 15 类非法帧、4 类非法续传材料、双存储中的协议失败且不重试 | 本轮补充未知事件＋正常／空完成帧，共 2 项通过 |
| `test_mcp_diagnostics.py` | 10 | NaN／循环／序列化异常、完成回调、异常组 cause、目录缺失／重复 | 本轮静态核对未发现上述断言失效；不是远程 MCP 验收 |

另外，`test_test_runner.py` 的 9 项、`test_prompt_recovery.py` 的 28 项及 `test_control_reconciliation.py` 的 40 项也在 2020 项基线中。恢复测试由固定等待 0.35 秒改为等待已保存租约到期；慢撤权测试仍持续超过完整 TTL，并新增同 worker／epoch 和续期到未来的断言。这些调整没有把真实恢复／续租要求删除。

## 3. 确认缺陷

| 编号 | 优先级 | 缺陷 | 本轮失败实例 |
| --- | --- | --- | ---: |
| R11-01 | P2 | begin_model 接受与已保存上下文不一致的请求，执行后无法正常收尾和恢复 | 6 |
| R11-02 | P2 | 重复 accept_model effect 导致持续重排队，或重复追加同一决策 | 4 |
| R11-03 | P2 | CRLF 格式 SKILL.md 从可导入退化为 SKILL_METADATA_INVALID | 1 |

### R11-01：模型请求与上下文的一致性检查晚于提交和调用

**位置：** [effect_validation.py:82](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/nodes/effect_validation.py:82)，调用处为 [effects.py:50](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/nodes/effects.py:50)。

`validate_model_intent` 检查 messages／record_ids 是列表、长度相同、ID 是字符串、消息含字符串 role，但未核对这些 ID 是否来自当前任务的有效上下文、是否重复，以及消息正文是否与引用的受保护记录一致。

通过真实 AgentNode／NodeDriver 链路，在 `begin_model` 交付前分别注入以下三类错误，其余字段保持合法：

1. 将全部 record_ids 替换成不存在的字符串。
2. 将所有 ID 替换成同一个已有记录 ID。
3. 保留正确 ID，只替换 SDK 系统指令正文。

两种存储中，**三类请求均先成功登记 Operation，并实际调用本地模型 1 次**。直到 Run 收尾时才触发 `PERSISTENCE_INVARIANT_BROKEN`，字段为 `prompt_contract.request_records` 或 `prompt_contract.request_content`，Run 保持 `running`。关闭 Runtime 后新建 Runtime，再用原 run_id 接续，仍报相同错误，无法正常恢复。

这不是仅有一个错误码不一致：提交端接纳了恢复端明确拒绝的现场，并且错误请求已经交给模型适配器。恢复校验本身位于 [prompts/recovery.py:65](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/prompts/recovery.py:65)。

**文档依据：** [runtime.md:169](/Users/bmilk/code/project/agent-loom/docs/sdk/runtime.md:169) 要求 outcome 在提交前检查所属身份、引用完整性和越权状态写；[implementation.md:31](/Users/bmilk/code/project/agent-loom/docs/sdk/implementation.md:31) 宣称非法 effect 应回滚并仅失败所属任务。SDK 系统指令受保护也是已实现合同。

**建议：** 在同一提交边界核验当前上下文的有效记录 ID／顺序／唯一性及消息映射，至少与既有 Prompt 恢复不变量保持一致；非法提案返回 `NODE_EFFECT_INVALID`，不写入 Operation、不调用模型。正常 Hook 修改仍走已有受保护记录与版本校验，不能为了修复此项直接禁用合法 Hook。

**覆盖边界：** 通过节点交付故障注入复现，不表示普通模型输出可以直接改写这些内部字段，也未观察到内置节点在正常运行时自行生成这种提案。正常请求及终态重新附着各后端均通过。

用例：[test_model_intent_must_match_its_owned_context_before_commit](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round11.py:151)。

### R11-02：重复接受同一模型决策未被统一拒绝或去重

**位置：** [effects.py:56](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/nodes/effects.py:56)，组合校验相关位置为 [effects.py:35](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/nodes/effects.py:35)。

在一个真实 `Progress` 中重复放入同一条 `accept_model` effect，有两种结果：

| 组合 | 内存／文件后端实际结果 |
| --- | --- |
| 保留 ContextHandler 生成的 expected_context_revision | 第一条 effect 递增 context revision，第二条用原 revision 检查而报 COMMIT_CONFLICT；事务回滚，但 Driver 按可恢复竞争重新排队，错误节点继续提交同一组合。5 秒观察窗口内分别出现 596／155 次激活，Run 仍 running、未接受决策 |
| 不带可选 expected_context_revision | 同一决策被追加两次；Run succeeded，模型只调用 1 次、Loop 只消费 1 次，却有 2 条相同 assistant 记录 |

持续重试发生在 [driver.py:100](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/nodes/driver.py:100)。无 revision 时，`consume_ticket` 对已消费票据直接返回，但调用方继续 append，故“票据不重复消费”未保证“上下文不重复接受”。

**文档依据：** [capabilities.md:441](/Users/bmilk/code/project/agent-loom/docs/sdk/capabilities.md:441) 的 C10 要求 accept＋ticket.consume＋batch 只提交一次；新增 effect 校验也承诺非法提案只失败所属任务。

**建议：** 提交前按操作身份检查重复／不兼容 effect；重复语义应明确拒绝或返回已接受事实，不重新追加上下文。对于组合写集，区分外部版本竞争与提案内部自相矛盾，不能将后者无限重排队。保留旧格式票据兼容时，需要独立的决策已接受事实，不能只凭 ticket=consumed 放行后续写入。

**覆盖边界：** 本轮通过节点交付故障注入触发，不是正常网络通知重复导致的证明；正常单条 effect 双后端均成功且只追加一条记录。未将观察到的重试速率当作性能基准。

用例：[test_accept_effect_is_bounded_and_never_appends_twice](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round11.py:95)。

### R11-03：Skill 字节读取重构丢失 CRLF 兼容

**位置：** [skills.py:60](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/capabilities/skills.py:60)。

`from_directory` 原先通过 `Path.read_text()` 读取，Python 的文本读取会统一换行。现在改为 `entry.decode("utf-8")`，保留 CRLF，而 frontmatter 的结束分隔正则仍要求 `---` 后立即是 LF 或文件末尾。因此正常 Windows 换行的 `SKILL.md` 被报为 `SKILL_METADATA_INVALID`。

最小输入与 LF 对照只有换行符不同：`---\r\nname: test\r\ndescription: test skill\r\n---\r\nBody\r\n`。本轮还从 Git 读取 `708bdaf` 的旧 Skill 导入实现，在同一真实目录和相同原始字节上对比：**旧实现成功且 manifest hash 正确，当前实现失败**。不是将整包过大、非法 YAML 或符号链接误计为换行缺陷。

**建议：** 解析 frontmatter 时接受 CRLF／统一解析用文本的换行；manifest hash、读取 size 和返回资源仍基于原始文件字节，避免修复导入后破坏完整性校验。

用例：[换行与原始 hash 检查](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round11.py:80)；[旧实现与当前实现对比证据](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round11-20261019/skill-newline-before-after.json)。

## 4. 本轮补充测试的正向覆盖

16 项通过包含：LF Skill 导入及原字节 hash 1 项；正常 accept effect 双后端 2 项；正常 model intent、运行完成及重新附着双后端 2 项；空／普通文本／二进制 Skill 资源 3 项；导入与运行时读取期间同长度修改、截短、增长共 6 项；Responses 未知扩展事件后正常文本／空文本完成 2 项。

因此本轮并非只堆叠非法输入断言：同时检查正常场景没有被过度拒绝、数据完整性保持、正常恢复不重复调用模型。

## 5. 新示例、平台和未实现功能的测试边界

新提交的 [my_create_agent.py](/Users/bmilk/code/project/agent-loom/agentloom-sdk/tests/my_create_agent.py) 是从环境变量读取配置、手动调用真实模型并打印结果的示例。其文件名不符合当前 pytest 默认收集规则，内部也没有 `test_*` 测试函数和结果断言；**它不会因为放进 tests 目录就进入一键单元测试，也不能计为真实 LLM 集成测试通过**。建议放到示例目录；若需要自动验收，另设显式启用的 live 测试入口，断言状态、输出、工具调用、超时和恢复，避免默认全量命令意外产生付费调用。本轮只静态检查该文件，没有执行；[语法和收集规则检查记录](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round11-20261019/manual-example-check.json)单独保存。

[平台 README](/Users/bmilk/code/project/agent-loom/agentloom-platform/README.md:3) 和[交互设计](/Users/bmilk/code/project/agent-loom/docs/platform/interaction-design.md:3) 明确说明平台仍是骨架，原型为本地模拟。新增页面不能由 2020 条 SDK 测试证明正确；鉴权隔离、草稿 CAS、发布阻断、持久派发、停止回执、断线重连、删除流程等需要后续平台 contract／integration／e2e 测试。未将未开发的后端算成本次 SDK 回归缺陷。

[当前实现差异](/Users/bmilk/code/project/agent-loom/docs/sdk/implementation.md:543) 仍明确列出以下未完成能力，新增测试没有补齐这些实现：

- 持久预算申请序与 FIFO 唤醒；部分等待仍为轮询。
- StartExecutor 及部分调用／控制变更尚未统一结构化 RuntimeCommit、expected revisions 和语义 fingerprint。
- 独立 ContentStore／BlobStore、ArtifactRef 导入及完整授权；完整 outbox／订阅 ACK、生命周期修改 Hook、完整公共查询 DTO。
- 多机环境撤权、共享／对象存储后端、持久文件 mutation journal、完整沙箱协议。

Workflow 继续按原审核范围排除。真实 MiniMax／远程 MCP 和平台端到端也没有因单元测试全绿而被验收。本轮重点是新增修复覆盖，不宣称全仓库其他部分已无缺陷。

## 6. 交付与修复后验证

- [新增 27 项 QA 测试](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round11.py)
- [定向运行摘要、命令与测试 hash](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round11-20261019/summary.json)
- [最终运行日志及状态观察](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round11-20261019/pytest.log)
- [最终 JUnit](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round11-20261019/junit.xml)

修复后可先从仓库根目录只运行本轮文件：

```bash
cd /Users/bmilk/code/project/agent-loom
.venv/bin/python -m pytest -c agentloom-sdk/pyproject.toml \
  agentloom-sdk/qa/test_sdk_review_round11.py -q -ra
```

新增 QA 使用正确行为断言，没有 xfail 或跳过；已在统一测试脚本的自动收集范围内。因此在缺陷修复前，下一次全量运行会包含这些失败项。原 2020 条基线文件和 `latest.json` 均保留，不用本轮失败证据覆盖原始结果。
