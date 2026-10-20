import os
import sys
import uuid
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).parents[1] / "api" / "src"))

from agentloom_platform.config import Settings
from agentloom_platform.http import create_app
from agentloom_platform.persistence import Base, Database, Membership, Resource, ResourceVersion, Space, User
from agentloom_platform.resources import initialize_catalog
from agentloom_platform.security import password_hash


@pytest.fixture
def platform(tmp_path):
    settings = Settings(
        database_url=os.environ.get("PLATFORM_TEST_DATABASE_URL", f"sqlite:///{tmp_path}/platform.db"),
        checkpoint_url=os.environ.get("PLATFORM_TEST_CHECKPOINT_URL", f"sqlite:///{tmp_path}/checkpoints.db"),
        data_dir=tmp_path,
        secret_key=Fernet.generate_key().decode(),
        allow_test_model=True,
        poll_seconds=0.01,
        _env_file=None,
    )
    settings.prepare()
    db = Database(settings.database_url)
    Base.metadata.drop_all(db.engine)
    Base.metadata.create_all(db.engine)
    with db.session(write=True) as s:
        space = Space(name="测试空间")
        other = Space(name="另一个空间")
        s.add_all([space, other])
        s.flush()
        users = {}
        for role in ("admin", "editor", "viewer"):
            user = User(username=role, password_hash=password_hash("test-password-123"))
            s.add(user)
            s.flush()
            users[role] = user.id
            s.add(Membership(user_id=user.id, space_id=space.id, role=role))
        initialize_catalog(s, space.id)
        model = Resource(space_id=space.id, kind="model", name="测试模型", description="确定性模型")
        s.add(model)
        s.flush()
        s.add(
            ResourceVersion(
                resource_id=model.id,
                version=1,
                status="ready",
                spec={
                    "provider": "test",
                    "model_id": "test",
                    "endpoint": "http://localhost",
                    "max_output_tokens": 1024,
                },
            )
        )
    app = create_app(settings, db)
    clients = {}
    for role in users:
        client = TestClient(app)
        response = client.post("/api/v1/auth/login", json={"username": role, "password": "test-password-123"})
        assert response.status_code == 200, response.text
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        clients[role] = client
    yield {
        "db": db,
        "settings": settings,
        "clients": clients,
        "space": space.id,
        "other_space": other.id,
        "model": model.id,
        "users": users,
    }
    for client in clients.values():
        client.close()
    db.engine.dispose()


def request(client, method, path, body=None, revision=None, key=None):
    headers = {"Idempotency-Key": key or uuid.uuid4().hex}
    if revision is not None:
        headers["If-Match"] = f'"{revision}"'
    return client.request(method, "/api/v1" + path, json=body, headers=headers)


def configured_agent(platform, mode="react", bindings=None):
    client = platform["clients"]["admin"]
    response = request(
        client,
        "POST",
        f"/spaces/{platform['space']}/agents",
        {"name": "验收应用", "mode": mode, "description": "描述"},
    )
    assert response.status_code == 201, response.text
    app = response.json()
    draft = app["draft"]
    draft["model"] = {"resource_id": platform["model"], "version": 1, "members": []}
    draft["definition"]["prompt"] = "回答用户的问题"
    draft["definition"]["capabilities"] = bindings or []
    if mode == "plan_executor":
        draft["executors"] = {
            "research": {"name": "研究", "description": "调查问题", "prompt": "研究任务", "capabilities": []},
            "writer": {"name": "写作", "description": "整理报告", "prompt": "撰写报告", "capabilities": []},
        }
    result = request(client, "PUT", f"/agents/{app['id']}/draft", draft, app["revision"])
    assert result.status_code == 200, result.text
    return result.json()


def publish(platform, app):
    from agentloom_platform.worker import maintenance

    response = request(
        platform["clients"]["admin"],
        "POST",
        f"/agents/{app['id']}/releases",
        {"draft_revision": app["revision"]},
        app["revision"],
    )
    assert response.status_code == 202, response.text
    maintenance(platform["settings"], platform["db"])
    return response.json()


def submit(platform, app, user="viewer", content="验证应用运行"):
    client = platform["clients"][user]
    response = request(client, "POST", f"/agents/{app['id']}/sessions")
    assert response.status_code == 201, response.text
    session = response.json()
    response = request(
        client,
        "POST",
        f"/sessions/{session['id']}/inputs",
        {"client_message_id": uuid.uuid4().hex, "content": content},
    )
    assert response.status_code == 202, response.text
    return session, response.json()


def find_tool(platform, name):
    with platform["db"].session() as s:
        for resource in s.scalars(
            select(Resource).where(Resource.space_id == platform["space"], Resource.kind == "tool")
        ):
            version = s.scalar(select(ResourceVersion).where(ResourceVersion.resource_id == resource.id))
            if version.spec["registry_key"] == name:
                return {"resource_id": resource.id, "version": 1, "members": []}
    raise AssertionError(name)
