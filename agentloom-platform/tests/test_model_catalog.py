import asyncio
import json

import httpx
import pytest
from conftest import request
from sqlalchemy import select

from agentloom_platform import model_catalog
from agentloom_platform.persistence import Receipt, Resource, ResourceVersion, Secret
from agentloom_platform.resources import decrypt_secret


@pytest.fixture
def upstream(monkeypatch):
    calls = []
    result = {"status": 200, "body": {"data": [{"id": "chat-b"}, {"id": "chat-a"}, {"id": "chat-b"}]}}

    async def handle(incoming):
        calls.append(incoming)
        if result.get("timeout"):
            raise httpx.ReadTimeout("secret must not leak", request=incoming)
        return httpx.Response(result["status"], json=result["body"], headers=result.get("headers", {}))

    client = httpx.AsyncClient
    monkeypatch.setattr(
        model_catalog.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs),
    )
    return calls, result


def catalog_path(platform):
    return f"/spaces/{platform['space']}/model-catalog"


def test_model_catalog_fetches_deduplicated_ids_without_persisting_credentials(platform, upstream):
    calls, _ = upstream
    response = request(
        platform["clients"]["admin"],
        "POST",
        catalog_path(platform),
        {
            "endpoint": "https://models.example/v1/",
            "credential": "only-for-discovery",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [{"id": "chat-a"}, {"id": "chat-b"}]}
    assert str(calls[0].url) == "https://models.example/v1/models"
    assert calls[0].headers["authorization"] == "Bearer only-for-discovery"
    assert "only-for-discovery" not in response.text
    with platform["db"].session() as s:
        assert list(s.scalars(select(Receipt))) == []
        assert list(s.scalars(select(Secret))) == []


def test_saved_credential_is_used_only_for_its_original_connection(platform, upstream):
    client = platform["clients"]["admin"]
    assert (
        request(
            client, "PUT", f"/resources/{platform['model']}/credential", {"secret": "saved-key"}
        ).status_code
        == 200
    )
    body = {"resource_id": platform["model"], "endpoint": "http://localhost/"}
    response = request(client, "POST", catalog_path(platform), body)
    assert response.status_code == 200
    calls, _ = upstream
    assert calls[-1].headers["authorization"] == "Bearer saved-key"
    response = request(
        client, "POST", catalog_path(platform), {**body, "endpoint": "https://another.example/v1"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MODEL_CREDENTIAL_REQUIRED"
    assert len(calls) == 1


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_model_discovery_is_administrator_only(platform, upstream, role):
    response = request(
        platform["clients"][role], "POST", catalog_path(platform), {"endpoint": "http://localhost"}
    )
    assert response.status_code == 403
    assert upstream[0] == []


def test_model_discovery_checks_csrf_and_space_membership_before_network(platform, upstream):
    client = platform["clients"]["admin"]
    body = {"endpoint": "http://localhost"}
    response = client.post("/api/v1" + catalog_path(platform), json=body, headers={"X-CSRF-Token": "wrong"})
    assert response.status_code == 403
    response = request(client, "POST", f"/spaces/{platform['other_space']}/model-catalog", body)
    assert response.status_code == 404
    with platform["db"].session(write=True) as s:
        s.get(Resource, platform["model"]).space_id = platform["other_space"]
    response = request(client, "POST", catalog_path(platform), {**body, "resource_id": platform["model"]})
    assert response.status_code == 404
    assert upstream[0] == []


def test_create_and_version_save_model_and_encrypted_key_atomically(platform):
    client = platform["clients"]["admin"]
    body = {
        "name": "Team models",
        "spec": {"endpoint": "https://models.example/v1", "model_id": "chat-a"},
        "credential": "private-key",
    }
    path = f"/spaces/{platform['space']}/resources/model"
    created = request(client, "POST", path, body, key="model-create")
    assert created.status_code == 201, created.text
    resource = created.json()
    assert resource["credential_configured"]
    assert request(client, "POST", path, body, key="model-create").json() == resource
    version = request(
        client,
        "POST",
        f"/resources/{resource['id']}/versions",
        {
            "name": body["name"],
            "spec": {**body["spec"], "model_id": "chat-b"},
        },
    )
    assert version.status_code == 201
    with platform["db"].session() as s:
        assert decrypt_secret(s, platform["settings"], resource["id"]) == "private-key"
        assert "private-key" not in s.get(Secret, resource["id"]).ciphertext
        assert all("private-key" not in json.dumps(row.body) for row in s.scalars(select(Receipt)))
        assert all("private-key" not in json.dumps(row.spec) for row in s.scalars(select(ResourceVersion)))
    assert "private-key" not in created.text + version.text
    changed = request(
        client,
        "POST",
        f"/resources/{resource['id']}/versions",
        {
            "name": body["name"],
            "spec": {**body["spec"], "endpoint": "https://new.example/v1"},
        },
    )
    assert changed.status_code == 422
    with platform["db"].session() as s:
        assert (
            len(list(s.scalars(select(ResourceVersion).where(ResourceVersion.resource_id == resource["id"]))))
            == 2
        )


@pytest.mark.parametrize(
    "status,body,code",
    [
        (401, {"error": "private-key"}, "MODEL_CATALOG_AUTH"),
        (403, {"error": "private-key"}, "MODEL_CATALOG_AUTH"),
        (404, {}, "MODEL_CATALOG_UNSUPPORTED"),
        (429, {}, "MODEL_CATALOG_RATE_LIMIT"),
        (302, {}, "MODEL_CATALOG_FAILED"),
        (200, {"error": "private-key"}, "MODEL_CATALOG_INVALID"),
        (200, {"data": [{"id": None}]}, "MODEL_CATALOG_INVALID"),
        (200, {"data": [{"id": "x" * 101}]}, "MODEL_CATALOG_INVALID"),
        (200, {"data": [{"id": "a"}] * 1001}, "MODEL_CATALOG_INVALID"),
        (200, {"padding": "a" * (1024 * 1024)}, "MODEL_CATALOG_TOO_LARGE"),
    ],
)
def test_catalog_failures_are_bounded_and_do_not_echo_upstream_secrets(upstream, status, body, code):
    calls, result = upstream
    result.update(status=status, body=body, headers={"Location": "https://other.example/models"})
    with pytest.raises(model_catalog.Problem) as error:
        asyncio.run(model_catalog.fetch_model_catalog("https://models.example/v1", "private-key"))
    assert error.value.code == code
    assert "private-key" not in error.value.message
    assert len(calls) == 1


def test_catalog_timeout_and_empty_list(upstream):
    _, result = upstream
    result["timeout"] = True
    with pytest.raises(model_catalog.Problem) as error:
        asyncio.run(model_catalog.fetch_model_catalog("https://models.example/v1", "private-key"))
    assert error.value.code == "MODEL_CATALOG_TIMEOUT"
    result.update(timeout=False, body={"data": []})
    assert asyncio.run(model_catalog.fetch_model_catalog("https://models.example/v1", "")) == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "file:///tmp/models",
        "https://user:key@models.example/v1",
        "http://169.254.169.254",
        "https://models.example/v1?key=secret",
        "https://models.example/v1/chat/completions",
    ],
)
def test_catalog_rejects_non_base_urls_before_network(upstream, endpoint):
    with pytest.raises(model_catalog.Problem):
        asyncio.run(model_catalog.fetch_model_catalog(endpoint, "private-key"))
    assert upstream[0] == []
