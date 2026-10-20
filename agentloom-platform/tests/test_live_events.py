import socket
import threading
import time

import httpx
import pytest
import uvicorn
from conftest import configured_agent, publish, submit
from sqlalchemy import select

from agentloom_platform.applications import append_event
from agentloom_platform.http import create_app
from agentloom_platform.persistence import LoginSession, Membership


@pytest.fixture
def live_api(platform):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(platform["settings"], platform["db"]),
            log_level="critical",
            lifespan="off",
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive()


@pytest.mark.parametrize("revocation", ["membership", "login_expiry"])
def test_open_event_stream_closes_when_authorization_is_revoked(platform, live_api, revocation):
    app = configured_agent(platform)
    publish(platform, app)
    _, receipt = submit(platform, app)
    with httpx.Client(base_url=live_api, trust_env=False, timeout=5) as client:
        login = client.post(
            "/api/v1/auth/login", json={"username": "viewer", "password": "test-password-123"}
        )
        assert login.status_code == 200
        path = f"/api/v1/runs/{receipt['run_id']}/events"
        with client.stream("GET", path) as response:
            assert response.status_code == 200
            lines = response.iter_lines()
            first = []
            for line in lines:
                first.append(line)
                if line.startswith("data:"):
                    break
            assert "id: 1" in first and "event: status" in first
        # Reconnect to the same live Run without replaying the consumed event.
        with client.stream("GET", path, headers={"Last-Event-ID": "1"}) as response:
            lines = response.iter_lines()
            assert next(lines) == ": heartbeat"
            with platform["db"].session(write=True) as s:
                if revocation == "membership":
                    s.delete(s.get(Membership, (platform["users"]["viewer"], platform["space"])))
                else:
                    for session in s.scalars(
                        select(LoginSession).where(
                            LoginSession.user_id == platform["users"]["viewer"],
                        )
                    ):
                        session.expires_at = time.time() - 1
                append_event(s, receipt["run_id"], "todos", {"todos": ["private-after-revocation"]})
            remaining = "\n".join(lines)
            assert "event: revoked" in remaining
            assert "private-after-revocation" not in remaining and "id: 1" not in remaining
