# 用真实 MiniMax 测试 AgentLoom

此入口使用当前 SDK 的 `OpenAIChatAdapter` 发起真实的 Chat Completions 流式请求，
没有 Mock 或失败后转用假模型的逻辑。API Key 由你在本机输入。

## 1. 安装和首次连通

在仓库根目录执行。已有 `.venv` 时不需要重新创建；Python 要求 3.11 或以上。

```bash
cd /Users/bmilk/code/project/agent-loom
python3 -m venv .venv
.venv/bin/python -m pip install -e ./agentloom-sdk
.venv/bin/python examples/minimax/smoke.py --case basic
```

程序会提示输入 MiniMax API Key，输入时不回显，也不会将 Key 写入结果文件或命令历史。
也可预先在本机设置 `MINIMAX_API_KEY` 环境变量。SDK 和本示例均不会自动加载 `.env`。

默认使用国内站地址 `https://api.minimax.cn/v1`、模型 `MiniMax-M2.7`，
上下文窗口设为 `204800`。这些值依据 MiniMax 官方接口文档；模型应在你的账户中可用。
国际站 Key 请配套使用国际地址，不能仅因为都叫 MiniMax 就混用站点。

```bash
# 国际站账户
MINIMAX_BASE_URL=https://api.minimax.io/v1 \
  .venv/bin/python examples/minimax/smoke.py --case basic

# 换用账户可用的 M3，同时更新 SDK 的上下文窗口元数据
.venv/bin/python examples/minimax/smoke.py --case basic \
  --model MiniMax-M3 --context-window 1000000
```

来源（2026-10-18 核对）：
[国内站接口与模型窗口](https://platform.minimax.cn/docs/api-reference/text-openai-api)、
[国际站接口](https://platform.minimax.io/docs/api-reference/text-chat-openai)。

## 2. 逐项测试

首次 `basic` 通过后运行：

```bash
.venv/bin/python examples/minimax/smoke.py --case react
.venv/bin/python examples/minimax/smoke.py --case plan
.venv/bin/python examples/minimax/smoke.py --case schema
```

也可用 `--case all` 按上述顺序连同 `basic` 一起运行；第一个失败后停止。

| 用例 | 实际验证 | 通过条件 |
| --- | --- | --- |
| `basic` | 鉴权、网络、流式响应、SDK 收尾 | Run 成功并回显本次校验值 |
| `react` | LLM 发起原生工具调用，SDK 执行并回传 | 本地 `read_probe` 被 ReAct 调用，最终值正确 |
| `plan` | Planner 委派、Executor 工具调用、结果汇总 | 实际调用 Planner 和 worker，工具在 worker 中执行，最终值正确 |
| `schema` | SDK 系统 prompt 中的 output_schema、最终 JSON 解析校验 | 返回对象精确匹配预期内容 |

工具用例的随机值没有放进用户 prompt 或工具定义，只能从实际执行的 `read_probe`
获得。因此模型仅声称“已调用工具”不能通过测试。这个本地工具只返回测试值，不操作业务系统。

每个用例默认最多 8 轮、执行期限 180 秒、每次生成上限 4096 tokens，关闭自动重试。
正常情况下 `basic` / `react` / `plan` / `schema` 约为 1 / 2 / 4 / 1 次模型调用；
格式纠正或重复调用会增加次数，实际以日志为准。真实请求会使用 MiniMax 的余额或套餐额度。
此入口发送固定测试任务，不读取项目源码；不覆盖业务质量、长上下文、并发、恢复或异常注入测试。

## 3. 查看结果和系统 prompt

每次启动生成新的 `.agentloom/live-minimax/<UTC时间>-<随机后缀>/`，不会覆盖上次结果：

- `results.json`：本次已完成用例汇总。
- `<用例>-result.json`：通过条件、Run 状态、原始 SDK 输出、错误、token 用量、实际工具调用。
- `<用例>-model-calls.jsonl`：每次 SDK 模型请求的角色、messages、tools 及归一化返回；
  `messages` 中的 `role=system` 可查看实际业务 prompt 和 SDK 模式 prompt。
- `state/`：SDK 持久化记录；`workspace/`：隔离的示例工作目录。

终端退出码：`0` 表示所选用例通过，`1` 表示用例失败，`2` 表示配置/启动错误。
结果目录在仓库已有的 `.agentloom/` 忽略规则内。日志不包含鉴权头或 API Key。

## 4. MiniMax 兼容性应单独检查

这个示例保留当前 SDK 的行为，不给模型响应加修复逻辑：

- MiniMax 的部分模型会把 `<think>...</think>` 放在 `content` 前面。文本用例只在比对最终
  校验值时跳过这个前缀，日志及 SDK 输出保留原文；`schema` 用例不做任何清洗或宽松解析。
  如果因此 JSON 校验失败，应作为接入兼容问题处理。
- 官方要求多轮工具调用保留完整 assistant 消息及思考字段。当前 `OpenAIChatAdapter`
  会回传 `content` 和 `tool_calls`，但独立的 `reasoning_content` 仅作为流事件输出，
  没有写入下一轮 assistant 历史。因此即使简单工具用例通过，也还不能证明交错思考完全兼容。
- 当前适配器的 `ModelOptions.parameters` 不接受 MiniMax 的 `reasoning_split` 或 `thinking`。
  不要直接照搬官方 SDK 的 `extra_body` 配置；需要另做适配并验证。

来源：[MiniMax 工具使用要求](https://platform.minimax.cn/docs/guides/text-m3-function-call)、
[Chat Completions 参数](https://platform.minimax.cn/docs/api-reference/text-chat-openai)。

常见排查方向：

| 结果 | 检查项 |
| --- | --- |
| `HTTP_401` / `HTTP_403` | Key 是否有效、Key 与国内/国际站是否匹配、模型访问权限 |
| `HTTP_429` | 余额、套餐额度或限流；查看 MiniMax 控制台 |
| `MODEL_CONNECTION_FAILED` | 网络、代理、TLS、API 地址 |
| `MODEL_RESPONSE_INCOMPLETE` | 是否因生成上限结束；尝试 `--max-output-tokens 8192` |
| 输出不符合 schema / 轮数耗尽 | 检查 model-calls 中归一化返回及 SDK 纠正消息，尤其是 `<think>` 前缀 |
| Run 成功但测试 `FAIL` | 查看 checks；可能没有执行工具/委派或没有返回要求的值 |

截至创建示例时，只完成了本地离线验证；没有提供 API Key，尚未执行真实 MiniMax 请求。
已用模拟 HTTP 流验证四条成功链路、文本中的思考前缀、严格 JSON 拒绝思考前缀、
未调用工具时判为失败、401 后停止继续请求，以及缺少 Key 时直接退出。
这些检查证明示例的装配和判定逻辑可运行，不代表 MiniMax 已通过真实测试。
