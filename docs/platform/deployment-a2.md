# a2 部署记录

部署日期：2026-10-21。当前应用版本：`eb183042e0d23d8d76ddda0cadf0d9660fce23db`。

新版已在 `oracle-a2` 上线，正式入口为 [agentloom.bmilk.cn](https://agentloom.bmilk.cn)。公网浏览器登录成功，源站 HTTPS、登录退出及 CSRF 检查通过，五个常驻服务均运行并设置开机自启。

2026-10-21 23:42（北京时间）已更新补充回归发现的修复：重复提交与跨页面响应、固定已有模型连接地址、统一地址和模型 ID 校验。a2 的独立 PostgreSQL 回归 **71 项及 2 个子测试全部通过**，前端 **19 项通过**。升级前完成数据库、工作文件和配置备份；源站 HTTPS、登录退出及现有 MiniMax 的 9 项模型列表再次验证通过。前一应用版本 `98f2e66` 保留用于回退。

2026-10-21 23:24（北京时间）已升级模型列表获取与资源页面布局。升级前确认没有活动 Run，备份数据库、工作目录、加密配置和用户服务文件，再切换独立发布目录。现有 MiniMax 连接与凭据保持原值，真实列表接口返回 9 个模型；源站 HTTPS、登录退出与模型发现通过。前一应用版本 `2e86aed` 保留用于回退。

**域名入口已激活并验证**：Nginx 已加载 `agentloom.bmilk.cn` 的现有证书，真实 TLS 握手验证证书链和域名均通过，协商为 TLS 1.3，证书截止日期为 2027-01-19。`certbot.timer` 已运行并启用，激活脚本已配置续期后的 Nginx 重载钩子。

首次激活曾在 Nginx 异步重载后立即检查 HTTPS，遇到旧证书而自动回滚。修正版复用已签发证书，并对重载期间的 TLS 错误限时重试；用户执行后，本次分别完成新域名的源站验证和公网浏览器验证。旧域名 `test-agentloom.bmilk.cn` 无需再配置 DNS。

## 部署结构

服务器使用 Ubuntu 24.04、ARM64、Python 3.14.7 和 PostgreSQL 17。前端独立构建为 Vue 静态页面；API 不托管页面。服务由 `bmilk` 的 systemd 用户实例管理，`Linger=yes`。

| 组件 | 用户服务 | 监听地址 |
| --- | --- | --- |
| HTTPS 入口 | 系统 Nginx，`agentloom-platform.conf` 已启用 | 443，转发至 8766 |
| 前端与 API 代理 | `agentloom-platform-web.service` | `127.0.0.1:8766` |
| FastAPI | `agentloom-platform-api.service` | `127.0.0.1:18766` |
| 执行 Worker | `agentloom-platform-worker.service` | 无 HTTP 端口 |
| 控制维护 | `agentloom-platform-control.service` | 无 HTTP 端口 |
| 独立 PostgreSQL 集群 | `agentloom-platform-db.service` | `127.0.0.1:17654` |

API 代理关闭响应缓冲以支持 SSE。数据库 `agentloom` 保存平台数据，`graph` schema 保存 LangGraph 检查点。独立测试库 `agentloom_verify` 用于回归，不能把测试指向业务库。

| 路径 | 用途 |
| --- | --- |
| `/home/bmilk/agentloom-platform/releases/eb18304` | 当前版本代码、独立 Python 虚拟环境、前端构建 |
| `/home/bmilk/agentloom-platform/current` | 当前发布的符号链接 |
| `/home/bmilk/agentloom-platform/shared/platform.env` | 数据库连接、加密密钥和运行配置，权限 `0600` |
| `/home/bmilk/agentloom-platform/shared/initial-admin.json` | 初始管理员访问资料，权限 `0600`，不得提交 Git |
| `/home/bmilk/agentloom-platform/shared/postgres` | 独立 PostgreSQL 数据目录 |
| `/home/bmilk/agentloom-platform/shared/data` | 工作文件、运行锁和平台数据目录 |
| `/home/bmilk/agentloom-platform/shared/log` | 前端代理访问与错误日志 |
| `/home/bmilk/agentloom-platform/shared/deployment.json` | 发布版本、切换时间、验收结果与公网访问状态 |
| `/home/bmilk/agentloom-platform/shared/https-verification.json` | 新域名源站 HTTPS、页面和认证检查结果，不含凭据 |
| `/home/bmilk/agentloom-platform/shared/model-catalog-verification.json` | 本次升级的版本、备份位置、TLS／认证与真实模型列表验收 |
| `/home/bmilk/agentloom-platform/backups/before-model-regression-20261021-234227` | 补充回归修复上线前的完整备份，权限受限 |
| `/home/bmilk/agentloom-platform/backups/before-model-catalog-20261021-232425` | 本次升级前的数据库、工作文件、加密配置和用户服务备份，权限受限 |
| `/home/bmilk/agentloom-platform/backups/pre-platform-2e86aed` | 原服务单元、入口配置和版本记录 |
| `/home/bmilk/agentloom-platform/backups/domain-agentloom-bmilk-cn` | 新域名切换前的平台私密配置备份 |
| `/home/bmilk/agentloom-platform/shared/config/activate-domain.sh` | 已上传的域名激活脚本，需 sudo 执行 |

首次部署使用新数据库，初始化了管理员 `admin`、“团队空间”和 24 项 Tool 目录。**旧版账号和业务数据没有迁移**；原 `/home/bmilk/agent-loom`、原数据库和 `agentloom-db-tunnel.service` 均保留。管理员后来配置的 MiniMax 连接已保留，本次只读取模型列表，没有修改所选模型或发起文本生成。

Cookie 设置为 `Secure`，允许源为目标 HTTPS 域名，测试模型关闭。沙箱及外部集成的实现边界见[当前实现](implementation.md)。

## 已完成的验收

- a2 上使用独立 PostgreSQL 测试库完成 31 项回归，全部通过；记录保存在 `shared/postgresql-verification.txt`。
- 模型列表与布局更新在同一专用测试库完成 56 项回归及 2 个子测试，全部通过；记录保存在 `shared/postgresql-verification-98f2e66.txt`。本机前端 14 项测试与生产构建通过。
- 补充回归修复版本 `eb18304` 在专用测试库完成 71 项回归及 2 个子测试，全部通过；记录保存在 `shared/postgresql-verification-eb18304.txt`。前端 19 项测试、TypeScript 与生产构建通过，覆盖模型发现到独立 Worker 执行的 ReAct／Plan 流程。
- 6 条前端路由返回本次发布的静态文件；API 路由、登录退出、Secure／HttpOnly Cookie、CSRF、24 项 Tool 目录和测试模型禁用检查通过。
- 使用新域名和源站地址验证 HTTPS，保留 Host、SNI、证书链和域名校验；登录退出、Secure／HttpOnly Cookie 以及携带正式 Origin 的 CSRF 退出请求通过。
- 公网浏览器经正常域名登录成功，应用、Tool、Skill、MCP 页面均可正常加载；Tool 显示 24 项目录。该检查没有创建测试业务数据。
- 本次升级后，公网浏览器打开现有 MiniMax 连接的“选择模型”，自动获取 9 个模型，当前仍为 `MiniMax-M3.1-Flash`；关闭配置后确认资源列表与运行环境在页面顶部正常展示。
- 五个常驻服务均启用开机自启；原 `agentloom-test.service` 已停用，内部预发布服务已停止。
- 证书续期定时器 `certbot.timer` 运行并启用；实际自动续期尚未发生，不能视为已完成续期演练。
- 新增两项真实 TLS 回归，在本机和 a2 均通过：先返回旧域名证书、随后切换为正确证书时检查可以恢复；持续错误的证书仍被拒绝，且不会继续解析空响应产生 JSON 错误。
- a2 的证书预检回归通过，确认接受正确域名与证书链、拒绝错误域名。该项需要支持 `openssl verify -verify_hostname` 的 OpenSSL；本机系统 LibreSSL 不提供此参数，跳过该项，Linux CI 会执行。

真实供应商的模型列表接口已验证；文本生成与工具调用、整机重启、完整备份恢复演练尚未在本次验收中验证。代码层面的详细测试与限制见[回归记录](testing-2026-10-21.md)。

## 域名配置维护

本次激活已经完成，无需重复执行。后续需要重新应用该域名站点配置时，可在本地终端运行以下命令，在 sudo 提示中输入 a2 的系统密码。无需将密码放入脚本或聊天。

```bash
ssh -t oracle-a2 'sudo bash /home/bmilk/agentloom-platform/shared/config/activate-domain.sh'
```

[脚本源码](../../agentloom-platform/deploy/a2-activate-domain.sh)先验证已有证书，再备份相关 Nginx 文件，只新增或更新 `agentloom-platform.conf` 这个域名站点，重载并验证 HTTPS，最后安装续期后的 Nginx 重载钩子。现有证书不会重新申请，原站点配置保留。重载是异步的，检查会在保持证书验证的前提下限时重试，只有成功取得 HTTP 响应才解析 JSON；最终仍失败时恢复修改前的 Nginx 文件并重载。脚本不会调整 Cloudflare 的 TLS 校验策略。

这次修正的 [TLS 回归测试](../../agentloom-platform/tests/test_domain_activation.py)可独立运行，不访问业务数据库：

```bash
python3 agentloom-platform/tests/test_domain_activation.py -v
```

本次已完成公网登录、源站 HTTPS 与 CSRF 验证，`shared/deployment.json` 的 `public_access` 已更新为 `verified`。后续重新应用配置时，也应分别验证源站和公网入口。如果已有证书预检失败，脚本会在改动 Nginx 前退出，应先核对证书文件与域名；如果源站检查通过但 Cloudflare 仍报错，应检查该域名是否回源到 `137.131.3.202`。

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
