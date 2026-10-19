# 第九轮修复验证：1764 项完整回归

**结论：本次 1764 passed 的结果与当前代码匹配；第九轮 3 个 P2 缺陷可以按原复现范围关闭。** 原第九轮 49 项 QA 文件未修改，全部通过。此前收尾期间出现的节点／检查点改动现已提交并纳入这次完整回归，可以使用这份结果作为后续审核的新基线。

本次范围是结果有效性校验、原缺陷关闭、节点／检查点新增测试覆盖核对及耗时比较。没有重新运行 pytest，没有修改生产代码或已有测试；只新增本报告和核对证据。

## 1. 测试结果与当前代码匹配

| 项目 | 核对结果 |
| --- | --- |
| 当前 HEAD | `a5ec1bcd2b885bad5bfcce745f925be9771d08ed` |
| 用户提供的结果 | `.agentloom/test-results/20261018-153852-709052Z` |
| pytest | **1764 passed，0 failed，0 errors，0 skipped** |
| 时间 | pytest **720.45 秒**；统一脚本记录总耗时 **721.123 秒** |
| 范围 | SDK tests **1192 项** + QA **572 项** |
| 有效性检查 | `./scripts/test_all.py --check` 返回 **0**，当前源码、测试、配置和环境匹配 |
| 运行中完整性 | `sources_unchanged=true`，前后源码／测试／配置清单共 **198 个文件** |
| Git 状态 | 测试从旧 HEAD 加未提交改动开始，结束时为当前提交且工作区干净；文件摘要一致，因此提交动作本身没有让本次结果失效 |

原始证据：[测试摘要](/Users/bmilk/code/project/agent-loom/.agentloom/test-results/20261018-153852-709052Z/summary.json)、[JUnit](/Users/bmilk/code/project/agent-loom/.agentloom/test-results/20261018-153852-709052Z/junit.xml)、[完整日志](/Users/bmilk/code/project/agent-loom/.agentloom/test-results/20261018-153852-709052Z/pytest.log)。

本次核对证据：[验证摘要](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round9-fix-verification-20261018/summary.json)、[基线检查输出](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round9-fix-verification-20261018/baseline-check.txt)、[新增用例清单](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/evidence/review-round9-fix-verification-20261018/added-cases.json)。

## 2. 原测试保留情况与缺陷关闭

对比上次 1665 项 JUnit 用例身份，本次 **没有缺失任何原用例**，新增 **99 项**：

| 新增来源 | 用例数 | 结果 |
| --- | ---: | --- |
| 第九轮 QA | 49 | 全部通过 |
| 节点协议测试 | 32 | 全部通过 |
| 检查点协议测试 | 18 | 全部通过 |
| 合计 | 99 | 全部通过 |

原八份 QA 测试文件的摘要均与 1665 项基线一致。第九轮 [test_sdk_review_round9.py](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/test_sdk_review_round9.py) 与审核时执行版本的 SHA-256 相同：

```text
ae77bdac3d7e343531e1190e884851ee028e38dfc2655d5477db72c2283a2117
```

因此第九轮的通过结果沿用了原失败断言，没有通过修改该测试文件来消除失败。

| 编号 | 本次修复与源码核对 | 原场景本次结果 | 状态 |
| --- | --- | --- | --- |
| R9-01：result_read 超末尾报错 | [results.py:16](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/context/results.py:16) 保留类型／非负数／limit 校验，`offset >= len(data)` 返回空文本及原 offset，调用层得到 `next_offset=None` | 16/16 通过；包含 ASCII、中文、EOF、EOF+1、2**52、双后端；原失败的 8 项全部通过 | 原复现范围内关闭 |
| R9-02：Unicode 补丁错误分行 | [mutations.py:93](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/environment/mutations.py:93) 对补丁与原文件统一使用按 LF 分行的 `lf_lines`，保留正文里的 Unicode 分隔字符 | 6/6 通过；原 U+2028、U+2029、U+0085、U+000B 四项失败全部通过 | 原复现范围内关闭 |
| R9-03：glob pattern 规范化遗漏 | [mutations.py:195](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/environment/mutations.py:195) 使用 `path_parts(pattern)`；同时单独保留末尾 `/` 的仅目录语义 | 7/7 通过；原点段／重复斜线五项失败全部通过 | 原复现范围内关闭 |

第九轮剩余 20 项为非法 astream 游标的行为记录和正常流对照，均通过。**R9-R01 设计风险仍保留**：当前 [astream](/Users/bmilk/code/project/agent-loom/agentloom-sdk/src/agentloom/public/agents.py:138) 仍先 start，再由 events 校验游标。异常不代表 Run 从未被接纳。原测试对这一点是行为探查，没有“必须先拒绝且零副作用”的断言，不能依据它通过就将该设计风险关闭。

此外核对了三份既有 SDK 测试的改动：Attempt 完成故障注入适配 RuntimeCommit.command_id；旧格式恢复夹具移除新协议检查点后构造旧状态；清理测试等待持久终态再断言。改动对应新的执行结构，原用例仍在本次 JUnit 中，相关结果全部通过。

## 3. 节点／检查点覆盖与原功能缺口的变化

[节点协议 32 项](/Users/bmilk/code/project/agent-loom/agentloom-sdk/tests/test_node_protocol.py) 覆盖了按事件版本路由、非法版本类型、注册环检测、Progress 提交回执先于续执行、激活端口失效、非法节点交付分类、模型等待时结束激活、票据／消息／批次同次提交，以及恢复前损坏拒绝。

[检查点协议 18 项](/Users/bmilk/code/project/agent-loom/agentloom-sdk/tests/test_checkpoint_protocol.py) 覆盖了双后端的快照回退与增量重放、ScopeCommit 缺号／hash／链损坏拒绝、未知引擎或协议版本拒绝、删除运行记录时清理所属检查点，以及图快照合并失败不能回滚已提交业务结果。

结合 [当前实现说明](/Users/bmilk/code/project/agent-loom/docs/sdk/implementation.md:23) 和对应源码，上一份报告的缺口状态需要做以下更新：

| 原项目 | 当前应采用的状态 |
| --- | --- |
| G9-02 中节点等待／事件路由部分 | 已增加持久 ready／wait、调用及批次调度、按类型和版本路由，以及 Context／Environment Handler。独立可靠订阅 outbox、ACK、重投及全部目标等待合同仍不能视为完整交付 |
| G9-09 中增量检查点部分 | 已有不可变节点版本、ScopeCommit 增量、图快照合并和一致重放；旧报告“尚无增量日志”的笼统描述应限定在旧快照，不继续沿用到当前版本 |
| G9-09 中剩余存储／恢复边界 | 跨机器协调、任意历史版本迁移、所有注入 handler／客户端配置的完整恢复指纹仍未整体交付；SQLite／PostgreSQL 后端也未交付 |
| 其他目标能力 | 独立内容存储与 Artifact 授权、脚本／生命周期 Hook、完整公共查询 DTO、统一模型 discovery、完整 MCP 资源／授权、完整沙箱／多后端环境、RE2 grep 和目录 move 等，仍需按原清单及最新实现说明逐项交付 |

节点／检查点使用的内部内容引用属于当前持久化实现；业务侧完整 ContentStore／BlobStore 和 Artifact 授权仍是独立的待实现范围。Workflow 继续沿用原排除范围。

## 4. 耗时观察：原用例也明显变慢

以下只比较两份已存在的 JUnit，没有另跑性能测试：

| 指标 | 上次 | 本次 |
| --- | ---: | ---: |
| 完整 pytest 耗时 | 378.88 秒 | 720.45 秒 |
| 原 1665 项用例记录耗时之和 | 378.490 秒 | 702.488 秒 |
| 新增 99 项用例记录耗时之和 | — | 17.489 秒 |
| `test_exact_last_loop_and_format_correction_budget[100]` | 117.214 秒 | 280.125 秒 |

本次总耗时约为上次 **1.90 倍**，增长主要发生在已有用例。100 Loop 场景是增长最大的单项，由约 117 秒增加到 280 秒，值得优先检查提交、检查点保存与恢复核验的开销。

这是一项有数据支持的性能待查项；两次测试的机器负载没有受控，本次也未采集性能剖析，因此暂不把耗时增加归因为某个模块或认定为确定的生产性能缺陷。JUnit 用例耗时求和与 pytest 墙钟总时间口径略有不同。

后续审核可以直接复用这份 1764 项结果，前提是 `scripts/test_all.py --check` 仍通过。本报告新增的 Markdown／JSON／文本证据不会改变测试集合。本次结果支持已覆盖场景的修复结论；真实 LLM、真实远程 MCP、跨机存储及新增未覆盖边界仍需各自的验证证据。
