# 第八轮缺陷修复后的测试结果核验

核验对象：用户于 2026-10-18 22:15:39–22:21:59（Asia/Shanghai）执行的完整测试，结果目录为 `.agentloom/test-results/20261018-141539-685079Z`。代码基于 `e8fd3b25625cfda1096650af5e17ef1a767c43e4`，包含本地尚未提交的修复；不能仅用该提交号代替本次源码快照。

**结论：1665 项全部通过，0 失败、0 错误、0 跳过。本次记录与当前源码、测试和环境匹配，可复用，无需重复执行相同的全量测试。第八轮两类缺陷在原复现用例覆盖范围内可关闭。**

本次只核验已有结果、文件摘要及相关修复代码，没有重新运行完整测试，也没有新增功能测试。

## 核验依据

| 检查 | 结果 |
| --- | --- |
| `./scripts/test_all.py --check` | 返回 0，当前源码／测试／配置及环境与记录匹配 |
| summary.json、JUnit XML、pytest.log | 三者一致：1665 passed；pytest 退出码 0 |
| 执行范围 | SDK 1142 项、QA 523 项，包含两个完整目录 |
| 测试期间文件变化 | `sources_unchanged=true` |
| 耗时 | pytest 378.88 秒；脚本整体约 379.32 秒 |
| 第八轮原 QA 文件 | SHA-256 与第八轮留存摘要相同，没有修改原测试断言 |
| 第八轮全部 QA | 61/61 通过；其中原先失败的 12 项均存在、通过且未跳过 |
| 新增专项 | `test_workspace_leases.py` 44/44 通过；`test_workspace_glob.py` 23/23 通过 |

## 缺陷状态

| 编号 | 原问题 | 当前结论 |
| --- | --- | --- |
| R8-01 / P1 | 同步文件后端执行期间阻断续租，引起 `LEASE_LOST` | 原 8 个失败场景全部通过。修复使用独立 LeaseHeartbeat 覆盖运行及 prepare 阶段，并检查续租错误、在终态提交前停止续租线程；实现方向与原问题对应 |
| R8-02 / P2 | glob 对 `./sub`、`sub/.` 等合法目录别名漏报 | 原 4 个失败场景全部通过。修复按规范逻辑路径分量计算相对路径，替代原始字符串前缀裁剪；实现方向与原问题对应 |

这表示原问题在已覆盖场景下修复，不表示全部目标设计已经实现，也不是对所有新代码完成了另一次全面审查。第八轮报告中的未实现功能清单仍需独立跟踪；本次没有新增真实 LLM、远端存储或跨机验收结论。

## 证据

- [测试摘要](/Users/bmilk/code/project/agent-loom/.agentloom/test-results/20261018-141539-685079Z/summary.json)
- [JUnit XML](/Users/bmilk/code/project/agent-loom/.agentloom/test-results/20261018-141539-685079Z/junit.xml)
- [完整日志](/Users/bmilk/code/project/agent-loom/.agentloom/test-results/20261018-141539-685079Z/pytest.log)
- [测试前文件摘要](/Users/bmilk/code/project/agent-loom/.agentloom/test-results/20261018-141539-685079Z/source-before.json)
- [测试后文件摘要](/Users/bmilk/code/project/agent-loom/.agentloom/test-results/20261018-141539-685079Z/source-after.json)
- [保留的第八轮原报告](/Users/bmilk/code/project/agent-loom/agentloom-sdk/qa/REVIEW_RESULTS_2026-10-18_ROUND8.md)
