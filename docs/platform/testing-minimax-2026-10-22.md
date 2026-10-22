# MiniMax 真实运行验收 · 2026-10-22

在 a2 当前平台版本 `eb183042e0d23d8d76ddda0cadf0d9660fce23db` 上，使用团队空间现有 `minimax` 连接实际完成 ReAct 和 Plan 两次运行，均通过。模型为 `MiniMax-M3.1-Flash`，连接版本 v1，OpenAI 兼容地址为 `https://api.minimax.cn/v1`，单次响应上限 4096 tokens。未修改现有连接或凭据。

这两次运行经过正式 HTTPS API、发布校验、生产 Worker、LangChain 与真实 MiniMax 服务。工具由平台工作空间执行，不使用模型响应夹具。验收时间为北京时间 08:42–08:43。

## 运行结果

| 模式 | 状态 | 模型调用 | 工具调用 | 实际执行 |
| --- | --- | ---: | ---: | --- |
| ReAct | completed | 5 | 4 | `write_todos`、`write_file`、`read_file`、`ls` 各 1 次 |
| Plan | completed | 8 | 4 | `writer` 调用 `write_file` 1 次；`reviewer` 调用 `read_file` 2 次、`edit_file` 1 次 |

模型调用数采用平台 Run 计数；工具调用数统计平台工具执行，不包含 LangChain 用于返回结构化计划的 `Plan` 工具。这些数字不是 token 用量或账单金额。

### ReAct

- 应用：`验收 · MiniMax ReAct · 2026-10-22`，已发布 v1。
- [查看会话](https://agentloom.bmilk.cn/sessions/91ce923a5de648e190fb360cec4ff0ed)（需登录本次验收的管理员账号）。
- Run：`0ea35adc9c5e4ee4b339e2c923e0d1cb`。
- 任务：记录待办、写文件、读回核对、列目录确认文件存在。
- 最终回答：`REACT_OK c5b7cc8dc266 status=verified`。
- 实际磁盘文件 `react-proof.txt` 内容如下，末尾保留换行：

```text
proof=c5b7cc8dc266
mode=react
status=verified
```

### Plan

- 应用：`验收 · MiniMax Plan · 2026-10-22`，已发布 v1。
- [查看会话](https://agentloom.bmilk.cn/sessions/706381bbc7454896ad9ab7e6f4a9bdc4)（需登录本次验收的管理员账号）。
- Run：`5e4c95f826b2494db64b428d0429c34a`。
- Planner 生成两个有序步骤：`writer` 创建文件；`reviewer` 读取、将 `status=written` 修改为 `status=verified`，再读回确认。
- 两个执行器共用该会话工作空间；`writer` 仅绑定写入工具，`reviewer` 仅绑定读取和修改工具，Planner 未绑定工作空间工具。
- 持久化计划事件依次显示两个任务待执行、writer 完成、两个任务完成；操作回执中的执行角色与上述授权一致。
- 最终回答包含 `PLAN_OK ba7a3934b50c` 和 `status=verified`。
- 实际磁盘文件 `plan-proof.txt` 内容如下，末尾保留换行：

```text
proof=ba7a3934b50c
mode=plan
status=verified
```

## 核对证据与范围

两次均核对了 Run 完成状态、模型与工具计数、每条持久化工具回执的 `completed`／`success`、实际磁盘文件的完整内容、最终回答中的随机校验标识，以及 SSE 的 `finished` 终态。Plan 的两条读取回执分别包含修改前的 `status=written` 与修改后的 `status=verified`。公网会话页另行检查展示结果和完成状态。

详细报告保留在 a2 的 `/home/bmilk/agentloom-platform/shared/verification/minimax-live-20261022/`，分别为 `react.json` 和 `plan_executor.json`。工作文件保留在 `shared/data/workspaces/<session_id>/`，两个验收应用和会话也保留供复查。

本次只测试上述已绑定工具，工作空间为读写、按会话保留，沙箱关闭。未新增 Skill、外部 MCP 或沙箱执行的真实 MiniMax 验收；相关自动化测试范围见[平台回归记录](testing-2026-10-21.md)。没有修改运行代码或重新部署服务。
