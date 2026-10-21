# a2 部署记录

部署日期：2026-10-21。应用版本：`2e86aed9bb08f8e89ab053e9e0778194ae68c6ba`。

新版已在 `oracle-a2` 启动，服务已设置开机自启。用户指定正式域名为 [agentloom.bmilk.cn](https://agentloom.bmilk.cn)，平台允许源与访问资料已更新，内部登录和 CSRF 检查通过。

**公网入口等待源站证书激活**：新域名的 DNS 已解析至 Cloudflare，浏览器当前返回 `526 Invalid SSL certificate`。a2 现有系统 Nginx 只配置了旧域名证书；新域名的 Nginx 配置和证书需要 sudo，当前 SSH 账号执行 sudo 会要求密码。域名激活脚本已经上传并通过语法和校验和检查，见下方操作步骤。

初次部署通过固定 IP 验证的是旧域名 `test-agentloom.bmilk.cn` 的源站 HTTPS，保留 SNI 和证书校验；不能将该结果当成新域名已经可用。旧域名无需再配置 DNS。

## 部署结构

服务器使用 Ubuntu 24.04、ARM64、Python 3.14.7 和 PostgreSQL 17。前端独立构建为 Vue 静态页面；API 不托管页面。服务由 `bmilk` 的 systemd 用户实例管理，`Linger=yes`。

| 组件 | 用户服务 | 监听地址 |
| --- | --- | --- |
| HTTPS 入口 | 系统 Nginx，新域名配置待 sudo 激活 | 443，转发至 8766 |
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
| `/home/bmilk/agentloom-platform/backups/domain-agentloom-bmilk-cn` | 新域名切换前的平台私密配置备份 |
| `/home/bmilk/agentloom-platform/shared/config/activate-domain.sh` | 已上传的域名激活脚本，需 sudo 执行 |

本次使用新数据库，初始化了管理员 `admin`、“团队空间”和 24 项 Tool 目录。**旧版账号和业务数据没有迁移**；原 `/home/bmilk/agent-loom`、原数据库和 `agentloom-db-tunnel.service` 均保留。未配置真实模型连接，需登录后在“模型与环境”添加连接及凭据。

Cookie 设置为 `Secure`，允许源为目标 HTTPS 域名，测试模型关闭。沙箱及外部集成的实现边界见[当前实现](implementation.md)。

## 已完成的验收

- a2 上使用独立 PostgreSQL 测试库完成 31 项回归，全部通过；记录保存在 `shared/postgresql-verification.txt`。
- 6 条前端路由返回本次发布的静态文件；API 路由、登录退出、Secure／HttpOnly Cookie、CSRF、24 项 Tool 目录和测试模型禁用检查通过。
- 使用指定源站 IP 的旧域名 HTTPS 请求完成校验，正常检查证书；未关闭 TLS 验证。新域名的允许源已更新，内部入口的登录与 CSRF 再次通过。
- 五个常驻服务均启用开机自启；原 `agentloom-test.service` 已停用，内部预发布服务已停止。

新域名的公网 HTTPS、真实供应商模型连接、整机重启、备份恢复演练尚未在本次验收中通过。代码层面的详细测试与限制见[回归记录](testing-2026-10-21.md)。

## 激活新域名

在本地终端运行以下命令，在 sudo 提示中输入 a2 的系统密码。无需将密码放入脚本或聊天。

```bash
ssh -t oracle-a2 'sudo bash /home/bmilk/agentloom-platform/shared/config/activate-domain.sh'
```

[脚本源码](../../agentloom-platform/deploy/a2-activate-domain.sh)会备份相关 Nginx 文件，添加 `agentloom.bmilk.cn` 的 HTTP 验证入口，使用服务器已安装的 Certbot 及现有账号申请证书，再启用 HTTPS、验证源站健康接口并安装续期后的 Nginx 重载钩子。原站点配置保留；失败会恢复修改前的 Nginx 文件并重载。脚本不会调整 Cloudflare 的 TLS 校验策略。

脚本执行成功后，还需用正常域名验证公网登录及 CSRF，然后将 `shared/deployment.json` 的 `public_access` 更新为 `verified`。如证书申请失败，先检查 Cloudflare 中该域名是否回源到 `137.131.3.202`，以及 ACME 验证路径是否能到达 a2；保留 Certbot 的错误信息用于排查。

## 检查与排障

通过 `ssh oracle-a2` 登录后执行：

```bash
systemctl --user status agentloom-platform-db agentloom-platform-api \
  agentloom-platform-worker agentloom-platform-control agentloom-platform-web
journalctl --user -u agentloom-platform-api -u agentloom-platform-worker \
  -u agentloom-platform-control -n 100 --no-pager
curl --fail http://127.0.0.1:18766/api/v1/health
```

激活后，排查源站时可以使用以下命令；它只对单次请求指定地址，仍校验域名证书：

```bash
curl --noproxy '*' --fail \
  --resolve agentloom.bmilk.cn:443:137.131.3.202 \
  https://agentloom.bmilk.cn/api/v1/health
```

## 回滚与后续升级

回滚前停止新任务并等待运行结束。在 a2 上停用新版入口、API 和执行进程，再恢复原服务：

```bash
systemctl --user disable --now agentloom-platform-web agentloom-platform-api \
  agentloom-platform-worker agentloom-platform-control
systemctl --user enable --now agentloom-test
```

新版数据库继续保留；旧版和新版数据相互独立，回滚不会把新版操作同步到旧版。旧版若继续使用新域名，需另外核对其允许源、Cookie 和登录设置，不能仅通过恢复进程就宣称登录链路已恢复。

升级前需同时备份数据库、工作目录和 `platform.env` 中的加密密钥。当前服务单元固定指向本次发布目录，**只改变 `current` 符号链接不会切换服务版本**；后续升级需更新单元的发布路径，执行 `systemctl --user daemon-reload`，迁移并验证后重启相关服务。部署文件备份与完整业务数据备份不是同一件事，目前尚未配置自动业务备份。
