"""Exercise the disposable Compose deployment, including Nginx and a real Worker."""

import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.request
import uuid

base = "http://localhost:8080"
client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
csrf = ""


def call(path, method="GET", body=None, revision=None):
    headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf}
    if method != "GET":
        headers["Idempotency-Key"] = uuid.uuid4().hex
    if revision is not None:
        headers["If-Match"] = str(revision)
    req = urllib.request.Request(
        base + "/api/v1" + path,
        method=method,
        headers=headers,
        data=json.dumps(body).encode() if body is not None else None,
    )
    with client.open(req, timeout=10) as response:
        return json.load(response)


def wait_for(read, predicate):
    for _ in range(60):
        value = read()
        if predicate(value):
            return value
        time.sleep(0.5)
    raise AssertionError(f"Deployment did not converge: {value}")


def main():
    global csrf
    for _ in range(60):
        try:
            with client.open(base + "/api/v1/openapi.json", timeout=2) as response:
                assert json.load(response)["info"]["title"]
            break
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.5)
    else:
        raise AssertionError("API did not start")
    with client.open(base + "/apps/deep-route", timeout=10) as response:
        assert '<div id="app"></div>' in response.read().decode()
    try:
        call("/not-a-route")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404 and "json" in exc.headers["Content-Type"]
    else:
        raise AssertionError("Unknown API was rewritten to the SPA")
    identity = call(
        "/auth/login", "POST", {"username": "ci_admin", "password": os.environ["PLATFORM_ADMIN_PASSWORD"]}
    )
    csrf = identity["csrf_token"]
    space = call("/spaces")["items"][0]["id"]
    model = call(
        f"/spaces/{space}/resources/model",
        "POST",
        {"name": "Deployment test", "spec": {"provider": "test", "model_id": "test"}},
    )
    for mode in ["react", "plan_executor"]:
        app = call(f"/spaces/{space}/agents", "POST", {"name": mode, "mode": mode})
        draft = app["draft"]
        draft["model"] = {"resource_id": model["id"], "version": 1}
        draft["definition"]["prompt"] = "Answer the question"
        if mode == "plan_executor":
            draft["executors"] = {"writer": {"name": "Writer", "prompt": "Write the answer"}}
        app = call(f"/agents/{app['id']}/draft", "PUT", draft, app["revision"])
        release = call(
            f"/agents/{app['id']}/releases",
            "POST",
            {"draft_revision": app["revision"]},
            app["revision"],
        )
        wait_for(
            lambda job_id=release["job_id"]: call(f"/release-jobs/{job_id}"),
            lambda job: job["status"] == "succeeded",
        )
        session = call(f"/agents/{app['id']}/sessions", "POST")
        receipt = call(
            f"/sessions/{session['id']}/inputs",
            "POST",
            {"client_message_id": uuid.uuid4().hex, "content": "Deployment check"},
        )
        result = wait_for(
            lambda run_id=receipt["run_id"]: call(f"/runs/{run_id}"),
            lambda run: run["status"] in {"completed", "failed", "cancelled"},
        )
        assert result["status"] == "completed", result
        history = call(f"/sessions/{session['id']}/history")
        assert history["items"][-1]["role"] == "assistant"
    print("Compose smoke passed: independent Web, API, PostgreSQL checkpoints, ReAct and Plan Worker runs")


if __name__ == "__main__":
    main()
