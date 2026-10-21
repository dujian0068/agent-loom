import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager

from cryptography.fernet import Fernet
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, select

from .applications import (
    TERMINAL,
    app_view,
    append_event,
    compile_snapshot,
    digest,
    get_app,
    validate_references,
)
from .config import Settings
from .model_catalog import catalog_endpoint, fetch_model_catalog
from .persistence import (
    Application,
    Audit,
    ChatSession,
    Database,
    Input,
    LoginSession,
    Membership,
    Receipt,
    Release,
    ReleaseJob,
    Resource,
    ResourceVersion,
    Run,
    RunEvent,
    Secret,
    Space,
    User,
    uid,
)
from .resources import decrypt_secret, discover_mcp, latest_version, resource_view, validate_spec
from .schemas import (
    AgentCreate,
    Credential,
    DiscoverySelection,
    Draft,
    Login,
    MemberCreate,
    MemberRole,
    ModelCatalogRequest,
    ResourceCreate,
    Revision,
    UserInput,
)
from .security import Problem, authenticate, password_hash, password_matches, require_role, token_hash

PREFIX = "/api/v1"


def create_app(settings=None, database=None):
    settings = settings or Settings()
    settings.prepare()
    db = database or Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(_):
        yield
        db.engine.dispose()

    api = FastAPI(
        title="AgentLoom Platform",
        version="0.1.0",
        lifespan=lifespan,
        openapi_url=PREFIX + "/openapi.json",
        docs_url=PREFIX + "/docs",
        redoc_url=None,
    )
    api.state.db, api.state.settings = db, settings
    api.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token", "If-Match", "Idempotency-Key", "Last-Event-ID"],
        expose_headers=["ETag", "X-Request-ID"],
    )

    def error_response(status, code, message, request_id, violations=None):
        return JSONResponse(
            status_code=status,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "request_id": request_id,
                    "violations": violations or [],
                }
            },
        )

    @api.middleware("http")
    async def request_contract(request, call_next):
        request.state.request_id = uid()
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin not in settings.allowed_origins:
                return error_response(403, "ORIGIN_REJECTED", "请求来源未授权", request.state.request_id)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 2 * 1024 * 1024:
                    return error_response(
                        413, "BODY_TOO_LARGE", "请求不能超过 2 MiB", request.state.request_id
                    )
            request._body = bytes(body)
            if body:

                def unique_keys(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError("duplicate key")
                        result[key] = value
                    return result

                try:
                    json.loads(body, object_pairs_hook=unique_keys)
                except (ValueError, UnicodeError):
                    return error_response(
                        422, "INVALID_JSON", "JSON 无效或包含重复字段", request.state.request_id
                    )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @api.exception_handler(Problem)
    async def problem_handler(request, exc):
        return error_response(exc.status, exc.code, exc.message, request.state.request_id, exc.violations)

    @api.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        violations = [
            {"path": ".".join(map(str, e["loc"])), "code": e["type"], "message": e["msg"]}
            for e in exc.errors()
        ]
        return error_response(422, "INVALID_REQUEST", "请求字段不合法", request.state.request_id, violations)

    @api.exception_handler(Exception)
    async def unexpected_handler(request, exc):
        logging.getLogger(__name__).error(
            "Request %s failed (%s)", request.state.request_id, type(exc).__name__
        )
        return error_response(
            500, "INTERNAL_ERROR", "服务处理失败，请提供请求编号排查", request.state.request_id
        )

    def actor(request):
        return authenticate(request, db)[0]

    def request_identity(request, body):
        key = request.headers.get("idempotency-key", "")
        if not key or len(key) > 100:
            raise Problem(428, "IDEMPOTENCY_REQUIRED", "请提供 Idempotency-Key")
        fingerprint = hmac.new(
            settings.secret_key.encode(),
            json.dumps(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "body": body,
                    "if_match": request.headers.get("if-match"),
                },
                sort_keys=True,
            ).encode(),
            hashlib.sha256,
        ).hexdigest()
        return key, fingerprint

    def saved_receipt(s, request, user, key, fingerprint):
        existing = s.get(Receipt, (user.id, key))
        if existing:
            if existing.digest != fingerprint:
                raise Problem(409, "IDEMPOTENCY_CONFLICT", "相同幂等键对应了不同请求")
            authorize_path(s, user.id, request.url.path)
            return existing.body
        return None

    def mutate(request, user, body, work):
        key, fingerprint = request_identity(request, body)
        with db.session(write=True) as s:
            # Authorization is checked before reading a saved response to respect revoked membership.
            saved = saved_receipt(s, request, user, key, fingerprint)
            if saved is not None:
                return saved
            result = work(s)
            s.add(Receipt(actor_id=user.id, key=key, digest=fingerprint, body=result))
            return result

    def authorize_path(s, actor_id, path):
        parts = path.removeprefix(PREFIX).strip("/").split("/")
        if parts[0] == "spaces":
            require_role(s, actor_id, parts[1], "editor" if parts[2] == "agents" else "admin")
        elif parts[0] == "agents":
            get_app(s, actor_id, parts[1], "viewer" if parts[2] == "sessions" else "editor")
        elif parts[0] == "resources":
            resource_access(s, actor_id, parts[1], "admin")
        elif parts[0] in {"sessions", "runs"}:
            session_access(s, actor_id, parts[1], is_run=parts[0] == "runs")

    def check_revision(request, app, revision=None):
        header = request.headers.get("if-match")
        if not header:
            raise Problem(428, "REVISION_REQUIRED", "请提供 If-Match")
        if header.strip('"') != str(app.revision) or revision is not None and revision != app.revision:
            raise Problem(409, "REVISION_CONFLICT", "草稿已被修改，请重新加载后合并")

    def resource_access(s, actor_id, resource_id, role="editor"):
        resource = s.get(Resource, resource_id)
        if not resource:
            raise Problem(404, "NOT_FOUND", "资源不存在")
        require_role(s, actor_id, resource.space_id, role)
        return resource

    def session_access(s, actor_id, object_id, is_run=False):
        run = s.get(Run, object_id) if is_run else None
        session = s.get(ChatSession, run.session_id if run else object_id)
        if (is_run and not run) or not session or session.owner_id != actor_id:
            raise Problem(404, "NOT_FOUND", "会话不存在")
        app = get_app(s, actor_id, session.app_id)
        return session, app, run

    def run_view(s, run):
        session = s.get(ChatSession, run.session_id)
        release = s.get(Release, run.release_id)
        return {
            "id": run.id,
            "run_id": run.id,
            "session_id": run.session_id,
            "application_id": session.app_id,
            "release_id": run.release_id,
            "release_version": release.version,
            "status": run.status,
            "result": run.result,
            "error": run.error,
            "cleanup_state": run.cleanup_state,
            "dispatch_status": "pending" if run.status == "queued" else "delivered",
            "model_calls": run.model_calls,
            "tool_calls": run.tool_calls,
            "deadline_at": run.deadline_at,
            "observed_at": time.time(),
            "last_event_seq": s.scalar(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run.id)) or 0,
        }

    def audit(s, user, space_id, action, target):
        s.add(Audit(actor_id=user.id, space_id=space_id, action=action, target=target))

    @api.get(PREFIX + "/health")
    def health():
        return {
            "status": "ok",
            "version": "0.1.0",
            "sandbox_available": False,
            "active_supplements_available": False,
        }

    @api.post(PREFIX + "/auth/login")
    def login(body: Login, response: Response):
        with db.session(write=True) as s:
            user = s.scalar(select(User).where(User.username == body.username))
            # Keep the expensive hash even for nonexistent users.
            encoded = user.password_hash if user else password_hash("invalid-login")
            if not password_matches(body.password, encoded) or not user or not user.enabled:
                raise Problem(401, "LOGIN_FAILED", "用户名或密码错误")
            token, csrf = secrets.token_urlsafe(32), secrets.token_hex(32)
            s.add(
                LoginSession(
                    token_hash=token_hash(token),
                    user_id=user.id,
                    csrf=csrf,
                    expires_at=time.time() + settings.session_hours * 3600,
                )
            )
        response.set_cookie(
            "platform_session",
            token,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            max_age=settings.session_hours * 3600,
            path=PREFIX,
        )
        return {"id": user.id, "username": user.username, "csrf_token": csrf}

    @api.get(PREFIX + "/auth/me")
    def me(request: Request):
        user, session = authenticate(request, db)
        return {"id": user.id, "username": user.username, "csrf_token": session.csrf}

    @api.post(PREFIX + "/auth/logout")
    def logout(request: Request, response: Response):
        _, session = authenticate(request, db)
        with db.session(write=True) as s:
            s.delete(s.get(LoginSession, session.token_hash))
        response.delete_cookie("platform_session", path=PREFIX)
        return {"status": "signed_out"}

    @api.get(PREFIX + "/spaces")
    def spaces(request: Request):
        user = actor(request)
        with db.session() as s:
            return {
                "items": [
                    {"id": space.id, "name": space.name, "role": membership.role}
                    for space, membership in s.execute(
                        select(Space, Membership)
                        .join(Membership, Membership.space_id == Space.id)
                        .where(Membership.user_id == user.id)
                    )
                ],
                "next_cursor": None,
            }

    @api.get(PREFIX + "/spaces/{space_id}/agents")
    def agents(space_id: str, request: Request):
        user = actor(request)
        with db.session() as s:
            membership = require_role(s, user.id, space_id)
            apps = s.scalars(
                select(Application)
                .where(Application.space_id == space_id)
                .order_by(Application.created_at.desc())
            )
            return {
                "items": [
                    app_view(s, a, membership.role != "viewer")
                    for a in apps
                    if membership.role != "viewer" or a.release_id
                ],
                "next_cursor": None,
            }

    @api.post(PREFIX + "/spaces/{space_id}/agents", status_code=201)
    def create_agent(space_id: str, body: AgentCreate, request: Request):
        user = actor(request)

        def work(s):
            require_role(s, user.id, space_id, "editor")
            app = Application(
                space_id=space_id,
                name=body.name,
                mode=body.mode,
                draft=Draft(description=body.description).model_dump(),
            )
            s.add(app)
            s.flush()
            audit(s, user, space_id, "agent.create", app.id)
            return app_view(s, app)

        return mutate(request, user, body.model_dump(), work)

    @api.get(PREFIX + "/agents/{app_id}")
    def agent(app_id: str, request: Request, response: Response):
        user = actor(request)
        with db.session() as s:
            app = get_app(s, user.id, app_id)
            role = require_role(s, user.id, app.space_id).role
            response.headers["ETag"] = f'"{app.revision}"'
            return app_view(s, app, role != "viewer")

    @api.put(PREFIX + "/agents/{app_id}/draft")
    def save_draft(app_id: str, body: Draft, request: Request):
        user = actor(request)

        def work(s):
            app = get_app(s, user.id, app_id, "editor")
            check_revision(request, app)
            validate_references(s, app, body.model_dump())
            app.draft, app.revision = body.model_dump(), app.revision + 1
            return app_view(s, app)

        return mutate(request, user, body.model_dump(), work)

    @api.post(PREFIX + "/agents/{app_id}/validate")
    def validate(app_id: str, body: Revision, request: Request):
        user = actor(request)
        with db.session() as s:
            app = get_app(s, user.id, app_id, "editor")
            if body.draft_revision != app.revision:
                raise Problem(409, "REVISION_CONFLICT", "请重新加载草稿")
            snapshot, violations = compile_snapshot(s, app, settings)
            if not violations:
                from .runtime import validate_graph

                validate_graph(snapshot)
            return {"valid": not violations, "draft_revision": app.revision, "violations": violations}

    @api.post(PREFIX + "/agents/{app_id}/releases", status_code=202)
    def publish(app_id: str, body: Revision, request: Request):
        user = actor(request)

        def work(s):
            app = get_app(s, user.id, app_id, "editor")
            check_revision(request, app, body.draft_revision)
            if app.lifecycle != "active":
                raise Problem(409, "PUBLISHING", "应用已有发布任务")
            snapshot, violations = compile_snapshot(s, app, settings)
            if violations:
                raise Problem(422, "VALIDATION_FAILED", "配置尚未通过校验", violations)
            from .runtime import validate_graph

            validate_graph(snapshot)
            number = (s.scalar(select(func.max(Release.version)).where(Release.app_id == app.id)) or 0) + 1
            release = Release(
                app_id=app.id,
                version=number,
                draft_revision=app.revision,
                snapshot=snapshot,
                content_hash=digest(snapshot),
            )
            s.add(release)
            s.flush()
            job = ReleaseJob(app_id=app.id, release_id=release.id)
            s.add(job)
            app.lifecycle = "publishing"
            runs = s.scalars(
                select(Run)
                .join(ChatSession, Run.session_id == ChatSession.id)
                .where(ChatSession.app_id == app.id, Run.status.not_in(TERMINAL))
            )
            for run in runs:
                run.status = "stopping"
                append_event(s, run.id, "status", {"status": "stopping", "reason": "发布新版本"})
            s.flush()
            audit(s, user, app.space_id, "agent.publish", app.id)
            return {"job_id": job.id, "release_id": release.id, "version": number, "status": job.status}

        return mutate(request, user, body.model_dump(), work)

    @api.get(PREFIX + "/release-jobs/{job_id}")
    def release_job(job_id: str, request: Request):
        user = actor(request)
        with db.session() as s:
            job = s.get(ReleaseJob, job_id)
            if not job:
                raise Problem(404, "NOT_FOUND", "发布任务不存在")
            get_app(s, user.id, job.app_id, "editor")
            return {"job_id": job.id, "status": job.status, "release_id": job.release_id}

    @api.get(PREFIX + "/agents/{app_id}/releases")
    def releases(app_id: str, request: Request):
        user = actor(request)
        with db.session() as s:
            get_app(s, user.id, app_id, "editor")
            return {
                "items": [
                    {
                        "id": r.id,
                        "version": r.version,
                        "draft_revision": r.draft_revision,
                        "content_hash": r.content_hash,
                        "snapshot": r.snapshot,
                        "created_at": r.created_at,
                    }
                    for r in s.scalars(
                        select(Release).where(Release.app_id == app_id).order_by(Release.version.desc())
                    )
                ],
                "next_cursor": None,
            }

    @api.get(PREFIX + "/spaces/{space_id}/resources")
    def resources(space_id: str, request: Request, kind: str | None = None):
        user = actor(request)
        with db.session() as s:
            require_role(s, user.id, space_id, "editor")
            query = select(Resource).where(Resource.space_id == space_id)
            if kind:
                query = query.where(Resource.kind == kind)
            return {"items": [resource_view(s, r) for r in s.scalars(query)], "next_cursor": None}

    @api.post(PREFIX + "/spaces/{space_id}/resources/{kind}", status_code=201)
    def add_resource(space_id: str, kind: str, body: ResourceCreate, request: Request):
        user = actor(request)

        def work(s):
            require_role(s, user.id, space_id, "admin")
            spec, status = validate_spec(kind, body.spec, settings)
            resource = Resource(space_id=space_id, kind=kind, name=body.name, description=body.description)
            s.add(resource)
            s.flush()
            s.add(ResourceVersion(resource_id=resource.id, version=1, status=status, spec=spec))
            store_resource_credential(s, resource, body.credential)
            audit(s, user, space_id, "resource.create", resource.id)
            s.flush()
            return resource_view(s, resource)

        return mutate(request, user, body.model_dump(exclude_none=True), work)

    @api.post(PREFIX + "/resources/{resource_id}/versions", status_code=201)
    def resource_version(resource_id: str, body: ResourceCreate, request: Request):
        user = actor(request)

        def work(s):
            resource = resource_access(s, user.id, resource_id, "admin")
            spec, status = validate_spec(resource.kind, body.spec, settings)
            if (
                resource.kind == "model"
                and s.get(Secret, resource_id)
                and body.credential is None
                and spec["endpoint"].rstrip("/") != latest_version(s, resource_id).spec["endpoint"].rstrip("/")
            ):
                raise Problem(422, "MODEL_CREDENTIAL_REQUIRED", "API 地址已改变，请为新地址重新填写 API Key")
            version = latest_version(s, resource_id).version + 1
            s.add(ResourceVersion(resource_id=resource_id, version=version, status=status, spec=spec))
            store_resource_credential(s, resource, body.credential)
            audit(s, user, resource.space_id, "resource.version", resource_id)
            s.flush()
            return resource_view(s, resource)

        return mutate(request, user, body.model_dump(exclude_none=True), work)

    def store_resource_credential(s, resource, credential):
        if credential is None:
            return
        if resource.kind not in {"model", "mcp"}:
            raise Problem(422, "INVALID_KIND", "此资源不支持凭据")
        encrypted = Fernet(settings.secret_key.encode()).encrypt(credential.encode()).decode()
        s.merge(Secret(resource_id=resource.id, ciphertext=encrypted))

    @api.post(PREFIX + "/spaces/{space_id}/model-catalog")
    async def model_catalog(space_id: str, body: ModelCatalogRequest, request: Request):
        user = actor(request)
        endpoint = catalog_endpoint(body.endpoint)
        credential = body.credential or ""
        with db.session() as s:
            require_role(s, user.id, space_id, "admin")
            if body.resource_id:
                resource = resource_access(s, user.id, body.resource_id, "admin")
                if resource.kind != "model" or resource.space_id != space_id:
                    raise Problem(404, "NOT_FOUND", "模型连接不存在")
                current_endpoint = catalog_endpoint(latest_version(s, resource.id).spec["endpoint"])
                if body.credential is None:
                    if endpoint != current_endpoint:
                        raise Problem(422, "MODEL_CREDENTIAL_REQUIRED", "API 地址已改变，请为新地址重新填写 API Key")
                    credential = decrypt_secret(s, settings, resource.id)
        items = await fetch_model_catalog(endpoint, credential)
        # Discovery is read-only: no secrets or upstream payloads enter receipts.
        user = actor(request)
        with db.session() as s:
            require_role(s, user.id, space_id, "admin")
            if body.resource_id:
                resource = resource_access(s, user.id, body.resource_id, "admin")
                if resource.space_id != space_id:
                    raise Problem(404, "NOT_FOUND", "模型连接不存在")
        return {"items": items}

    @api.put(PREFIX + "/resources/{resource_id}/credential")
    def credential(resource_id: str, body: Credential, request: Request):
        user = actor(request)

        def work(s):
            resource = resource_access(s, user.id, resource_id, "admin")
            if resource.kind not in {"model", "mcp"}:
                raise Problem(422, "INVALID_KIND", "此资源不支持凭据")
            encrypted = Fernet(settings.secret_key.encode()).encrypt(body.secret.encode()).decode()
            s.merge(Secret(resource_id=resource_id, ciphertext=encrypted))
            audit(s, user, resource.space_id, "credential.set", resource_id)
            return {"credential_configured": True}

        return mutate(request, user, body.model_dump(), work)

    @api.post(PREFIX + "/resources/{resource_id}/discover")
    async def discover(resource_id: str, request: Request):
        user = actor(request)
        key, fingerprint = request_identity(request, {})
        with db.session() as s:
            resource = resource_access(s, user.id, resource_id, "admin")
            saved = saved_receipt(s, request, user, key, fingerprint)
            if saved is not None:
                return saved
            if resource.kind != "mcp":
                raise Problem(422, "INVALID_KIND", "仅 MCP 连接可以发现工具")
            version = latest_version(s, resource_id)
            spec, secret = version.spec, decrypt_secret(s, settings, resource_id)
        try:
            members = await discover_mcp(spec, secret)
        except Problem:
            raise
        except Exception:  # noqa: BLE001 -- remote MCP libraries expose several transport error types
            raise Problem(502, "MCP_DISCOVERY_FAILED", "MCP 发现失败，请检查服务地址与认证")

        def work(s):
            resource = resource_access(s, user.id, resource_id, "admin")
            current = latest_version(s, resource_id)
            if current.version != version.version:
                raise Problem(409, "RESOURCE_VERSION_CONFLICT", "连接配置已变化，请重新发现工具")
            new_version = current.version + 1
            s.add(
                ResourceVersion(
                    resource_id=resource_id,
                    version=new_version,
                    status="discovered",
                    spec={**spec, "members": members},
                )
            )
            audit(s, user, resource.space_id, "mcp.discover", resource_id)
            s.flush()
            return resource_view(s, resource)

        return mutate(request, user, {}, work)

    @api.post(PREFIX + "/resources/{resource_id}/enable-members")
    def enable_members(resource_id: str, body: DiscoverySelection, request: Request):
        user = actor(request)

        def work(s):
            resource = resource_access(s, user.id, resource_id, "admin")
            version = s.scalar(
                select(ResourceVersion).where(
                    ResourceVersion.resource_id == resource_id,
                    ResourceVersion.version == body.discovery_version,
                )
            )
            if resource.kind != "mcp" or not version or version.status != "discovered":
                raise Problem(422, "DISCOVERY_REQUIRED", "请先发现工具")
            members = [m for m in version.spec["members"] if m["name"] in body.members]
            if len(members) != len(set(body.members)):
                raise Problem(422, "INVALID_MEMBERS", "选择了发现结果以外的工具")
            s.add(
                ResourceVersion(
                    resource_id=resource_id,
                    version=latest_version(s, resource_id).version + 1,
                    spec={**version.spec, "members": members},
                    status="ready",
                )
            )
            audit(s, user, resource.space_id, "mcp.enable_members", resource_id)
            s.flush()
            return resource_view(s, resource)

        return mutate(request, user, body.model_dump(), work)

    @api.get(PREFIX + "/spaces/{space_id}/sessions")
    def sessions(space_id: str, request: Request):
        user = actor(request)
        with db.session() as s:
            require_role(s, user.id, space_id)
            rows = s.execute(
                select(ChatSession, Application)
                .join(Application)
                .where(ChatSession.owner_id == user.id, Application.space_id == space_id)
                .order_by(ChatSession.created_at.desc())
            )
            return {
                "items": [
                    {"id": c.id, "title": c.title, "app_id": a.id, "app_name": a.name} for c, a in rows
                ],
                "next_cursor": None,
            }

    @api.post(PREFIX + "/agents/{app_id}/sessions", status_code=201)
    def create_session(app_id: str, request: Request):
        user = actor(request)

        def work(s):
            app = get_app(s, user.id, app_id)
            if not app.release_id or app.lifecycle != "active":
                raise Problem(409, "APP_NOT_READY", "应用尚未发布或正在发布")
            session = ChatSession(app_id=app_id, owner_id=user.id)
            s.add(session)
            s.flush()
            return {"id": session.id, "app_id": app_id, "title": session.title}

        return mutate(request, user, {}, work)

    @api.post(PREFIX + "/sessions/{session_id}/inputs", status_code=202)
    def submit_input(session_id: str, body: UserInput, request: Request):
        user = actor(request)

        def work(s):
            session, app, _ = session_access(s, user.id, session_id)
            existing = s.scalar(
                select(Input).where(
                    Input.session_id == session_id, Input.client_message_id == body.client_message_id
                )
            )
            if existing:
                if existing.content != body.content:
                    raise Problem(409, "MESSAGE_CONFLICT", "相同消息编号对应了不同内容")
                return {"input_id": existing.id, "run_id": existing.run_id, "status": existing.status}
            if app.lifecycle != "active" or not app.release_id:
                raise Problem(409, "APP_NOT_READY", "应用正在发布，暂不接收新问题")
            if s.scalar(select(Run.id).where(Run.session_id == session_id, Run.status.not_in(TERMINAL))):
                raise Problem(409, "RUN_ACTIVE", "当前任务尚未结束；本阶段暂不支持运行中补充")
            release = s.get(Release, app.release_id)
            run = Run(
                session_id=session_id,
                release_id=release.id,
                deadline_at=time.time() + release.snapshot["draft"]["execution"]["timeout_seconds"],
            )
            s.add(run)
            s.flush()
            item = Input(
                session_id=session_id,
                run_id=run.id,
                client_message_id=body.client_message_id,
                content=body.content,
            )
            s.add(item)
            session.title = body.content[:60]
            append_event(s, run.id, "status", {"status": "queued"})
            s.flush()
            return {"input_id": item.id, "run_id": run.id, "status": "submitting"}

        return mutate(request, user, body.model_dump(), work)

    @api.get(PREFIX + "/sessions/{session_id}/history")
    def history(session_id: str, request: Request):
        user = actor(request)
        with db.session() as s:
            session, app, _ = session_access(s, user.id, session_id)
            runs = list(s.scalars(select(Run).where(Run.session_id == session_id).order_by(Run.created_at)))
            items = []
            for run in runs:
                item = s.scalar(select(Input).where(Input.run_id == run.id))
                if item:
                    items.append(
                        {
                            "id": item.id,
                            "role": "user",
                            "content": item.content,
                            "run_id": run.id,
                            "status": item.status,
                        }
                    )
                if run.result:
                    items.append({"id": run.id, "role": "assistant", "content": run.result, "run_id": run.id})
            return {
                "id": session.id,
                "app_id": app.id,
                "app_name": app.name,
                "title": session.title,
                "items": items,
                "runs": [run_view(s, r) for r in runs],
                "next_cursor": None,
            }

    @api.get(PREFIX + "/runs/{run_id}")
    def run_status(run_id: str, request: Request):
        user = actor(request)
        with db.session() as s:
            _, _, run = session_access(s, user.id, run_id, True)
            return run_view(s, run)

    @api.post(PREFIX + "/runs/{run_id}/stop", status_code=202)
    def stop(run_id: str, request: Request):
        user = actor(request)

        def work(s):
            _, _, run = session_access(s, user.id, run_id, True)
            if run.status not in TERMINAL and run.status != "stopping":
                run.status = "stopping"
                append_event(s, run.id, "status", {"status": "stopping"})
            return {
                "command_id": f"stop-{run.id}",
                "run_id": run.id,
                "status": "confirmed" if run.status in TERMINAL else "requested",
            }

        return mutate(request, user, {}, work)

    @api.get(PREFIX + "/runs/{run_id}/events")
    async def events(run_id: str, request: Request, after: int = 0):
        user = actor(request)
        with db.session() as s:
            session_access(s, user.id, run_id, True)
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise Problem(422, "INVALID_CURSOR", "事件游标必须是整数")

        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                try:
                    authenticate(request, db)
                    with db.session() as s:
                        _, _, run = session_access(s, user.id, run_id, True)
                        rows = list(
                            s.scalars(
                                select(RunEvent)
                                .where(RunEvent.run_id == run_id, RunEvent.seq > cursor)
                                .order_by(RunEvent.seq)
                                .limit(100)
                            )
                        )
                        terminal = run.status in TERMINAL
                except Problem:
                    yield "event: revoked\ndata: {}\n\n"
                    return
                for row in rows:
                    cursor = row.seq
                    yield f"id: {row.seq}\nevent: {row.kind}\ndata: {json.dumps(row.payload)}\n\n"
                if terminal and len(rows) < 100:
                    yield "event: finished\ndata: {}\n\n"
                    return
                yield ": heartbeat\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    @api.get(PREFIX + "/spaces/{space_id}/members")
    def members(space_id: str, request: Request):
        user = actor(request)
        with db.session() as s:
            require_role(s, user.id, space_id, "admin")
            return {
                "items": [
                    {"id": u.id, "username": u.username, "role": m.role}
                    for u, m in s.execute(
                        select(User, Membership).join(Membership).where(Membership.space_id == space_id)
                    )
                ],
                "next_cursor": None,
            }

    @api.post(PREFIX + "/spaces/{space_id}/members", status_code=201)
    def add_member(space_id: str, body: MemberCreate, request: Request):
        user = actor(request)

        def work(s):
            require_role(s, user.id, space_id, "admin")
            if s.scalar(select(User).where(User.username == body.username)):
                raise Problem(409, "USER_EXISTS", "账号已存在；请由部署管理员添加已有账号的空间成员关系")
            new_user = User(username=body.username, password_hash=password_hash(body.password))
            s.add(new_user)
            s.flush()
            s.add(Membership(user_id=new_user.id, space_id=space_id, role=body.role))
            audit(s, user, space_id, "member.create", new_user.id)
            return {"id": new_user.id, "username": new_user.username, "role": body.role}

        return mutate(request, user, body.model_dump(), work)

    @api.put(PREFIX + "/spaces/{space_id}/members/{user_id}")
    def change_role(space_id: str, user_id: str, body: MemberRole, request: Request):
        user = actor(request)

        def work(s):
            require_role(s, user.id, space_id, "admin")
            member = s.get(Membership, (user_id, space_id))
            if not member:
                raise Problem(404, "NOT_FOUND", "成员不存在")
            admins = s.scalar(
                select(func.count())
                .select_from(Membership)
                .where(Membership.space_id == space_id, Membership.role == "admin")
            )
            if member.role == "admin" and body.role != "admin" and admins <= 1:
                raise Problem(409, "LAST_ADMIN", "必须保留至少一位空间管理员")
            member.role = body.role
            audit(s, user, space_id, "member.role", user_id)
            return {"id": user_id, "role": body.role}

        return mutate(request, user, body.model_dump(), work)

    @api.get(PREFIX + "/spaces/{space_id}/audit")
    def audit_log(space_id: str, request: Request):
        user = actor(request)
        with db.session() as s:
            require_role(s, user.id, space_id, "admin")
            return {
                "items": [
                    {
                        "id": a.id,
                        "action": a.action,
                        "target": a.target,
                        "actor_id": a.actor_id,
                        "created_at": a.created_at,
                    }
                    for a in s.scalars(
                        select(Audit)
                        .where(Audit.space_id == space_id)
                        .order_by(Audit.created_at.desc())
                        .limit(100)
                    )
                ]
            }

    return api
