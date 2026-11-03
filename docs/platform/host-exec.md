# 本机命令执行

更新：2026-11-03。`exec` 已实现为独立本机执行服务，无需外部沙箱。应用默认关闭命令执行；部署执行服务后，在「运行环境」选择「本机执行」，再在「能力配置」为 ReAct Agent 或 Plan 执行器选择 `exec`。工作区必须可读写。草稿测试与正式发布使用相同实现，设置和工具版本随快照固定。

## 命令行为

`exec(command, timeout_seconds=60)` 使用 `/bin/sh -c`，当前目录为任务或会话的工作空间。可以运行服务器已有的 Python、Shell 等程序；依赖需事先安装或由命令在工作空间准备。文件与 `read_file`、`write_file` 等工具互通。

- 返回 `stdout`、`stderr`、`exit_code`、`stdout_truncated`、`stderr_truncated`、`timed_out`、`cancelled`。
- 每条命令最多 300 秒，同时受任务剩余期限限制。每路输出保留最多 64 KiB，超出后继续排空，避免堵塞进程。
- 非零退出、超时、取消返回工具错误及结构化结果，模型可以据实处理。
- 停止任务、Worker 断开或命令正常结束时，执行服务终止命令进程组。后台命令不作为持久服务保留，不支持交互式 stdin。
- 工具调用沿用持久回执。Worker 在取得结果前被强杀时，结果不明的命令阻止自动重放。

## 执行账号与访问范围

命令由独立系统服务和 `agentloom-exec` 账号执行，无登录 Shell、无 sudo，仅指定 Worker UID 可连接本机 Unix socket。服务只依赖 Python 标准库，不加载平台配置。

命令仅接收固定 `PATH`、工作区 `HOME`／`TMPDIR` 和语言设置，不继承模型 Key、数据库密码、平台加密密钥或 Python／Shell 启动钩子。服务文件由 root 持有；客户端与执行服务核对实现摘要，版本不同则拒绝执行。

安装脚本隐藏服务内的用户主目录，只将工作空间挂载到 `/var/lib/agentloom-exec/workspaces`。平台私密父目录与凭据权限保持原样。工作区按组共享，共享处理不跟随符号链接。服务配置依据 [systemd 执行环境文档](https://www.freedesktop.org/software/systemd/man/255/systemd.exec.html)。

**本机模式不提供每个应用或会话的独立沙箱。** 相同执行账号的命令可能访问其他已共享工作空间及账号有权限访问的网络、系统文件；有意脱离进程组的程序不属于进程组清理保证。仅为可信用户和应用启用，不用于互不信任的租户。

## Linux 部署

系统管理员执行一次安装，路径按实际部署替换：

```bash
sudo bash agentloom-platform/deploy/install-host-exec.sh \
  bmilk \
  /home/bmilk/agentloom-platform/shared/data/workspaces \
  /home/bmilk/agentloom-platform/current/api/src/agentloom_platform/exec_service.py
```

脚本创建执行账号、安装 root 所有的源码与 `agentloom-exec.service`，配置开机启动，验证不同执行 UID、Unix socket、干净环境、真实文件写入及权限。失败时恢复原服务文件，不修改 Nginx、证书或平台凭据内容。最多 4 条并发命令；服务限制进程数和内存，命令限制 CPU 时间、单文件大小及文件描述符数。

在 API、Worker 配置中设置并重启对应服务：

```dotenv
PLATFORM_EXEC_SOCKET_PATH=/run/agentloom-exec/runner.sock
```

`GET /api/v1/health` 中 `host_exec_available=true` 表示服务可连且版本一致，`sandbox_available` 仍为 `false`。未配置时不会降级为 Worker 账号直接执行。容器部署默认关闭本机执行，需要另行映射 socket、工作空间并匹配 Worker UID。

迁移 `0003` 为既有禁用 `exec` 资源追加可用版本，保留旧版本和发布快照，不自动授权任何应用。发布过的应用依照现有升级规则重新发布，以更新运行实现摘要。升级执行服务前应等待活动命令结束；停止 `agentloom-exec` 系统服务会终止其全部进程。
