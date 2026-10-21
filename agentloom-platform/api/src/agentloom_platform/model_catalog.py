"""Read the model list from an authorized OpenAI-compatible connection."""

import asyncio
import json
from urllib.parse import urlsplit

import httpx

from .resources import validate_url
from .security import Problem


def catalog_endpoint(endpoint):
    endpoint = endpoint.strip().rstrip("/")
    try:
        validate_url(endpoint)
        url = urlsplit(endpoint)
        _ = url.port  # Validate malformed ports before making a request.
        if url.query or url.path.endswith(("/models", "/chat/completions", "/responses")):
            raise ValueError("Expected a base URL")
    except ValueError:
        raise Problem(422, "INVALID_ENDPOINT", "请输入 API 基础地址，例如 https://api.example.com/v1")
    return endpoint


async def fetch_model_catalog(endpoint, credential):
    endpoint = catalog_endpoint(endpoint)
    headers = {"Accept": "application/json"}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    try:
        # Never forward a saved credential to a redirect destination.
        async with (
            asyncio.timeout(20),
            httpx.AsyncClient(timeout=15, follow_redirects=False) as client,
            client.stream("GET", endpoint + "/models", headers=headers) as response,
        ):
            if response.status_code in (401, 403):
                raise Problem(502, "MODEL_CATALOG_AUTH", "模型服务拒绝认证，请检查 API Key 与访问权限")
            if response.status_code in (404, 405):
                raise Problem(
                    502, "MODEL_CATALOG_UNSUPPORTED", "服务未提供模型列表，请核对 API 地址或手动填写模型 ID"
                )
            if response.status_code == 429:
                raise Problem(502, "MODEL_CATALOG_RATE_LIMIT", "模型服务请求过于频繁，请稍后刷新")
            if response.status_code != 200:
                raise Problem(502, "MODEL_CATALOG_FAILED", "无法获取模型列表，请检查 API 地址与服务状态")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 1024 * 1024:
                    raise Problem(502, "MODEL_CATALOG_TOO_LARGE", "模型列表过大，暂时无法加载")
        payload = json.loads(data)
    except (TimeoutError, httpx.TimeoutException):
        raise Problem(504, "MODEL_CATALOG_TIMEOUT", "获取模型列表超时，请稍后重试")
    except (httpx.HTTPError, ValueError, UnicodeError):
        # Upstream errors and exception strings may contain credentials or URLs.
        raise Problem(502, "MODEL_CATALOG_FAILED", "模型列表获取失败，请核对地址、凭据和返回格式")
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > 1000:
        raise Problem(502, "MODEL_CATALOG_INVALID", "服务未返回有效的模型列表")
    identifiers = set()
    for row in rows:
        identifier = row.get("id") if isinstance(row, dict) else None
        if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 100:
            raise Problem(502, "MODEL_CATALOG_INVALID", "服务返回的模型 ID 格式不合法")
        identifiers.add(identifier.strip())
    return [{"id": identifier} for identifier in sorted(identifiers)]
