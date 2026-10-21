# a2 部署记录

部署日期：2026-10-21。应用版本：`2e86aed9bb08f8e89ab053e9e0778194ae68c6ba`。

新版已在 `oracle-a2` 启动并切换到原 HTTPS 入口，服务已设置开机自启。**公网访问仍等待 DNS 恢复**：部署时 `test-agentloom.bmilk.cn` 返回 NXDOMAIN；需要为 `test-agentloom` 添加指向 `137.131.3.202` 的 A 记录。目标地址为 [团队工作台](https://test-agentloom.bmilk.cn)。

源站 HTTPS 已通过固定 IP 检查，保留域名、SNI 和证书校验；这个结果不代表公共 DNS 已生效。

## 部署结构

服务器使用 Ubuntu 24.04、ARM64、Python 3.14.7 和 PostgreSQL 17。前端独立构建为 Vue 静态页面；API 不托管页面。服务由 `bmilk` 的 systemd 用户实例管理，`Linger=yes`。

| 组件 | 用户服务 | 监听地址 |
| --- | --- | --- |
| HTTPS 入口 | 原系统 Nginx，配置保持不变 | 443，转发至 8766 |
| 前端与 API 代理 | `agentloom-platform-web.service` | `127.0.0.1:8766` |
| FastAPI | `agentloom-platform-api.service` | `127.0.0.1:18766` |
| 执行 Worker | `agentloom-platform-worker.service` | 无 HTTP 端口 |
| 控制维护 | `agentloom-platform-control.service` | 无 HTTP 端口 |
| 独立 PostgreSQL 集群 | `agentloom-platform-db.service` | `127.0.0.1:17654` |

API 代理关闭响应缓冲以支持 SSE。数据库 `agentloom` 保存平台数据，`graph` schema 保存 LangGraph 检查点。独立测试库 `agentloom_verify` 用于回归，不能把测试指向业务库。

| 路径 | 用途 |
| --- | --- |
| `/home/bmilk/agentloom-platform/releases/2e86aed` | 固定版本代码、Python 虚拟环境、前端构建 |
| `/home/bmilk/agentloom-platform/current` | 当前发布的符号链接 |
| `/home/bmilk/agentloom-platform/shared/platform.env` | 数据库连接、加密密钥和运行配置，权限 `0600` |
| `/home/bmilk/agentloom-platform/shared/initial-admin.json` | 初始管理员访问资料，权限 `0600`，不得提交 Git |
| `/home/bmilk/agentloom-platform/shared/postgres` | 独立 PostgreSQL 数据目录 |
| `/home/bmilk/agentloom-platform/shared/data` | 工作文件、运行锁和平台数据目录 |
| `/home/bmilk/agentloom-platform/shared/log` | 前端代理访问与错误日志 |
| `/home/bmilk/agentloom-platform/shared/deployment.json` | 发布版本、切换时间、验收结果与公网访问状态 |
| `/home/bmilk/agentloom-platform/backups/pre-platform-2e86aed` | 原服务单元、入口配置和版本记录 |

本次使用新数据库，初始化了管理员 `admin`、“团队空间”和 24 项 Tool 目录。**旧版账号和业务数据没有迁移**；原 `/home/bmilk/agent-loom`、原数据库和 `agentloom-db-tunnel.service` 均保留。未配置真实模型连接，需登录后在“模型与环境”添加连接及凭据。

Cookie 设置为 `Secure`，允许源为目标 HTTPS 域名，测试模型关闭。沙箱及外部集成的实现边界见[当前实现](implementation.md)。

## 已完成的验收

- a2 上使用独立 PostgreSQL 测试库完成 31 项回归，全部通过；记录保存在 `shared/postgresql-verification.txt`。
- 6 条前端路由返回本次发布的静态文件；API 路由、登录退出、Secure／HttpOnly Cookie、CSRF、24 项 Tool 目录和测试模型禁用检查通过。
- 使用指定源站 IP 的 HTTPS 请求完成校验，正常检查证书；未关闭 TLS 验证。
- 五个常驻服务均启用开机自启；原 `agentloom-test.service` 已停用，内部预发布服务已停止。

公共 DNS、真实供应商模型连接、整机重启、备份恢复演练尚未在本次验收中通过。代码层面的详细测试与限制见[回归记录](testing-2026-10-21.md)。

## 检查与排障

通过 `ssh oracle-a2` 登录后执行：

```bash
systemctl --user status agentloom-platform-db agentloom-platform-api \
  agentloom-platform-worker agentloom-platform-control agentloom-platform-web
journalctl --user -u agentloom-platform-api -u agentloom-platform-worker \
  -u agentloom-platform-control -n 100 --no-pager
curl --fail http://127.0.0.1:18766/api/v1/health
```

DNS 恢复后，应在服务器外使用正常域名验证 HTTPS 页面与 `/api/v1/health`，再将部署记录的 `public_access` 更新为 `verified`。排查源站时可以使用以下命令；它只对单次请求指定地址，仍校验域名证书：

```bash
curl --noproxy '*' --fail \
  --resolve test-agentloom.bmilk.cn:443:137.131.3.202 \
  https://test-agentloom.bmilk.cn/api/v1/health
```

## 回滚与后续升级

回滚前停止新任务并等待运行结束。在 a2 上停用新版入口、API 和执行进程，再恢复原服务：

```bash
systemctl --user disable --now agentloom-platform-web agentloom-platform-api \
  agentloom-platform-worker agentloom-platform-control
systemctl --user enable --now agentloom-test
```

新版数据库继续保留；旧版和新版数据相互独立，回滚不会把新版操作同步到旧版。恢复旧入口同样需要域名解析正常。

升级前需同时备份数据库、工作目录和 `platform.env` 中的加密密钥。当前服务单元固定指向本次发布目录，**只改变 `current` 符号链接不会切换服务版本**；后续升级需更新单元的发布路径，执行 `systemctl --user daemon-reload`，迁移并验证后重启相关服务。部署文件备份与完整业务数据备份不是同一件事，目前尚未配置自动业务备份。
