# agentloom-platform

基于 **LangChain + LangGraph** 的团队 Agent 平台。当前已提供可启动的 Vue 前端、FastAPI 后端、独立 Worker、维护进程及 PostgreSQL 部署配置；不依赖 agentloom-sdk。

这是首个开发版本。已实现登录与空间角色、独立 Tool／Skill／MCP 页面、应用配置与版本发布、私有会话、ReAct 和顺序 Plan 执行、持久事件及同机检查点恢复。[当前实现与限制](../docs/platform/implementation.md)列出了已经接通的功能和未完成的验收。

## 本地启动

使用 Python 3.14、Node.js 24；前后端各有锁文件。以下命令从仓库根目录开始。SQLite 仅用于本地开发，部署配置使用 PostgreSQL。

```bash
cd agentloom-platform/api
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/agentloom-platform init --username admin --space "团队空间"
```

初始化会提示输入管理员密码（至少 12 字符），生成仅当前用户可读的 `.env` 密钥文件，并运行数据库迁移。不会设置默认密码或重置已有账号。`.env` 与 `data/` 已忽略，不应提交到 Git。

分别开启三个终端：

```bash
# 终端 1，工作目录 agentloom-platform/api
.venv/bin/agentloom-platform api

# 终端 2，相同目录；开发模式下也执行维护扫描
.venv/bin/python -m agentloom_platform.worker

# 终端 3，工作目录 agentloom-platform/web
npm ci
npm run dev
```

打开 [本地工作台](http://localhost:5173)，使用初始化账号登录。前端开发代理将 `/api/v1` 转发到 `127.0.0.1:8000`。API 只返回 JSON／SSE，不托管 SPA。

1. 在“模型与环境”添加 OpenAI 兼容连接，填写服务地址和模型 ID，再单独保存凭据。
2. 在 Tool／Skill／MCP 页面准备资源。MCP 需要先发现，再明确选择成员；Skill 支持直接填写 `SKILL.md` 或导入文本目录。
3. 新建应用时填写名称、选择 **ReAct 或 Plan**，可填写描述。模式无默认值，创建后不可变。
4. 配置提示词、模型、各角色资源与工作空间。Plan 至少添加一个专业执行器。
5. 保存、校验并发布，再从应用卡片点击“使用”。会话只对创建者可见。

沙箱尚未接入，`exec` 禁用；目录中的外部集成模板显示为不可用。系统代理环境下访问本机模型／MCP，需将对应内网地址加入 `NO_PROXY`。

## 独立部署

2026-10-21 已部署至 a2，采用独立用户服务和 PostgreSQL 集群，公网域名等待 DNS 恢复。服务路径、验证结果与回滚方式见 [a2 部署记录](../docs/platform/deployment-a2.md)。

前端镜像只包含静态页面与 Nginx；后端镜像只包含 Python 服务。`deploy/compose.yaml` 分别启动 Web、API、Worker、控制维护及 PostgreSQL，并把图检查点放在独立 `graph` schema。

先在 `agentloom-platform` 目录生成部署密钥文件：

```bash
api/.venv/bin/python - <<'PY'
import os, secrets
from cryptography.fernet import Fernet
fd = os.open("deploy/.env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as output:
    output.write("POSTGRES_PASSWORD=" + secrets.token_hex(24) + "\n")
    output.write("PLATFORM_SECRET_KEY=" + Fernet.generate_key().decode() + "\n")
PY
docker compose --env-file deploy/.env -f deploy/compose.yaml build
docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm api \
  agentloom-platform init --username admin --space "团队空间"
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d
```

访问 [容器工作台](http://localhost:8080)。默认仅绑定本机端口；团队访问通过自己的 HTTPS 入口代理，并在 `deploy/.env` 中设置 `PLATFORM_ALLOWED_ORIGINS`（JSON 数组）和 `PLATFORM_COOKIE_SECURE=true`。跨源 Web 可以修改公开的 `web/public/config.js` 的 `apiBase`，API 的允许源需对应配置。凭据不能放入前端运行配置。

当前支持一台执行主机及共享工作卷；`PLATFORM_WORKER_HOST_ID` 必须稳定，同一部署的 Worker 和控制进程使用相同值。进程文件锁避免同机重复写入，尚不支持跨主机接管。运行结果不明的工具调用会阻止自动重放，并阻塞相关发布，需人工核对；尚未提供完整的管理员处置界面。

备份需同时保留 PostgreSQL、工作卷和 `.env` 加密密钥。升级前停止新提交并等待任务结束；当前尚未完成在线升级和灾备验收。

## 验证与接口

```bash
# 在 api/ 下
.venv/bin/ruff check src migrations ../tests
PYTHONPATH=src .venv/bin/python -m pytest ../tests -q

# 在 web/ 下
npm test
npm run build
```

测试使用显式开启的确定性模型及本机 OpenAI 兼容 HTTP 服务，并启动本机 MCP／SSE 服务；包含真实 Worker 中途强杀恢复，不需要外部模型密钥。前端使用 Vitest 检查保存、导航与请求重试。测试证据见[回归记录](../docs/platform/testing-2026-10-21.md)。PostgreSQL CI 使用 `PLATFORM_TEST_DATABASE_URL` 和 `PLATFORM_TEST_CHECKPOINT_URL` 指向专用测试库，测试会重建该库的平台表，不能指向业务数据库。

API 启动后访问 `/api/v1/docs` 或 `/api/v1/openapi.json`。当前实现路径以实际 OpenAPI 为准，[API 设计](../docs/platform/api.md)保留后续目标协议。CI 独立检查前端、SQLite／PostgreSQL 和容器部署。

| 目录 | 内容 |
| --- | --- |
| `api/src/agentloom_platform/` | HTTP、身份、资源、发布、LangChain／LangGraph 与 Worker |
| `api/migrations/` | 固定 Alembic 迁移 |
| `web/src/` | 独立 Vue + TypeScript 工作台 |
| `web/prototypes/` | 历史交互原型，不参与业务页面构建 |
| `tests/` | API、执行链路、恢复、权限与 MCP 验证 |
| `deploy/` | 前后端独立镜像及 Compose |

继续阅读：[实现状态](../docs/platform/implementation.md)、[实施方案](../docs/platform/implementation-plan.md)、[运行设计](../docs/platform/runtime-integration.md)、[交互设计](../docs/platform/interaction-design.md)、[SDK／Platform 边界](../docs/sdk-platform-boundary.md)。
