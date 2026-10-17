import errno
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentloom import ConfigurationError, FileRuntimeStore, InfrastructureError
from agentloom.configuration import canonical, digest
from agentloom.storage import CommitCoordinator, _files, claim


def namespace_path(root, namespace="default"):
    return root / "namespaces" / hashlib.sha256(namespace.encode()).hexdigest()


def worker(root, mode):
    return subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).parent / "support/file_store_process.py"),
            str(root),
            mode,
        ],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def seed(root):
    store = FileRuntimeStore(root)
    with store.transaction("default") as tx:
        tx.put("runs", "counter", {"value": 0})
    return store


def test_json_transactions_rollback_detached_values_and_namespace_isolation(tmp_path):
    store = FileRuntimeStore(tmp_path)
    namespace, kind, key = "../ns/中文", "../../kind", "../../../key"
    value = {"array": [1, None, "中文"]}
    with store.transaction(namespace) as tx:
        tx.put(kind, key, value)
        value["array"].append(99)
        tx.get(kind, key)["array"].append(42)
        assert tx.get(kind, key) == {"array": [1, None, "中文"]}
        tx.put(kind, "null", None)
        assert tx.list(kind) == [{"array": [1, None, "中文"]}, None]
    with pytest.raises(InfrastructureError, match="STORE_TRANSACTION_CLOSED"):
        tx.get(kind, key)
    with pytest.raises(ValueError):
        with store.transaction(namespace) as tx:
            tx.delete(kind, key)
            tx.put(kind, "aborted", True)
            raise ValueError("abort")
    # Closing one handle cannot erase persisted state or close another handle.
    store.close()
    with pytest.raises(InfrastructureError, match="STORE_CLOSED"):
        store.load(namespace, "any")
    reopened = FileRuntimeStore(tmp_path)
    with reopened.transaction(namespace) as tx:
        assert tx.get(kind, key) == {"array": [1, None, "中文"]}
        assert tx.get(kind, "aborted") is None
        tx.delete(kind, "null")
    with reopened.transaction("different") as tx:
        assert tx.list(kind) == []
    persisted = list(namespace_path(tmp_path, namespace).glob("objects/*.json"))
    assert len(persisted) == 1
    assert json.loads(persisted[0].read_text()) == {"array": [1, None, "中文"]}


@pytest.mark.parametrize("cut", ["record_sync", "before_publish", "after_publish", "after_commit"])
def test_process_death_never_exposes_half_a_file_transaction(tmp_path, cut):
    store = seed(tmp_path)
    process = worker(tmp_path, cut)
    _, error = process.communicate(timeout=10)
    assert process.returncode == 73, error
    with store.transaction("default") as tx:
        expected = 0 if cut in ("record_sync", "before_publish") else 1
        assert tx.get("runs", "counter") == {"value": expected}
        assert tx.get("commits", "increment") == (None if not expected else {"value": 1})
    assert store.collect_garbage() >= (1 if cut != "after_commit" else 0)
    assert store.collect_garbage() == 0
    assert store.load("default", "counter") == {"value": expected}


def test_independent_processes_serialize_read_modify_write_without_lost_updates(tmp_path):
    store = seed(tmp_path)
    processes = [worker(tmp_path, "increment") for _ in range(3)]
    try:
        for process in processes:
            _, error = process.communicate(timeout=15)
            assert process.returncode == 0, error
        with store.transaction("default") as tx:
            assert tx.get("runs", "counter") == {"value": 36}
            assert {v["value"] for v in tx.list("commits")} == set(range(1, 37))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()


def test_lock_timeout_is_bounded_and_other_namespaces_are_independent(tmp_path):
    store = seed(tmp_path)
    process = worker(tmp_path, "lock")
    try:
        assert process.stdout.readline().strip() == "locked"
        short = FileRuntimeStore(tmp_path, lock_timeout_s=0.02)
        before = time.monotonic()
        with pytest.raises(InfrastructureError, match="STORE_BUSY"):
            short.load("default", "counter")
        assert time.monotonic() - before < 1
        with short.transaction("other") as tx:
            tx.put("runs", "independent", True)
        process.communicate(input="release\n", timeout=5)
        assert process.returncode == 0
        assert store.load("default", "counter") == {"value": 0}
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_reentrant_transaction_rejected_without_deadlock_and_lock_released(tmp_path):
    store = seed(tmp_path)
    with store.transaction("default") as tx:
        with pytest.raises(InfrastructureError, match="STORE_TRANSACTION_NESTED"):
            FileRuntimeStore(tmp_path).load("default", "counter")
        tx.put("runs", "counter", {"value": 2})
    assert store.load("default", "counter") == {"value": 2}


@pytest.mark.parametrize(
    "fault",
    [
        "truncated_manifest",
        "checksum",
        "missing_object",
        "changed_object",
        "symlink",
        "missing_manifest",
    ],
)
def test_corruption_fails_closed_instead_of_restarting_work(tmp_path, fault):
    store = seed(tmp_path)
    directory = namespace_path(tmp_path)
    manifest = directory / "manifest.json"
    object_path = next((directory / "objects").glob("*.json"))
    if fault == "truncated_manifest":
        manifest.write_text('{"manifest":')
    elif fault == "checksum":
        envelope = json.loads(manifest.read_text())
        envelope["manifest"]["records"] = {}
        manifest.write_text(json.dumps(envelope))
    elif fault == "missing_manifest":
        manifest.unlink()
    elif fault == "missing_object":
        object_path.unlink()
    elif fault == "changed_object":
        object_path.write_text('{"value":99}')
    else:
        outside = tmp_path / "outside.json"
        object_path.rename(outside)
        object_path.symlink_to(outside)
    with pytest.raises(InfrastructureError, match="STORE_(CORRUPT|UNAVAILABLE)"):
        store.load("default", "counter")


def test_unsupported_version_is_not_overwritten(tmp_path):
    store = seed(tmp_path)
    path = namespace_path(tmp_path) / "manifest.json"
    envelope = json.loads(path.read_text())
    envelope["manifest"]["version"] = 99
    envelope["checksum"] = digest(envelope["manifest"])
    data = canonical(envelope)
    path.write_text(data)
    with pytest.raises(InfrastructureError, match="STORE_VERSION_UNSUPPORTED"):
        store.load("default", "counter")
    assert path.read_text() == data


def test_publication_io_failure_rolls_back_both_state_and_receipt(tmp_path, monkeypatch):
    store = seed(tmp_path)
    original = _files.os.replace

    def fail(source, target, **kwargs):
        if target == "manifest.json":
            raise OSError(errno.ENOSPC, "disk full")
        return original(source, target, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(_files.os, "replace", fail)
        with pytest.raises(InfrastructureError, match="STORE_UNAVAILABLE"):
            with store.transaction("default") as tx:
                tx.put("runs", "counter", {"value": 1})
                tx.put("commits", "increment", {"value": 1})
    with store.transaction("default") as tx:
        assert tx.get("runs", "counter") == {"value": 0}
        assert tx.get("commits", "increment") is None
    assert store.collect_garbage() == 1


def test_lost_directory_sync_ack_uses_same_committed_receipt(tmp_path, monkeypatch):
    store = FileRuntimeStore(tmp_path)
    with store.transaction("default") as tx:
        tx.put("runs", "run", {"status": "running", "revision": 0, "value": 0})
    lease = claim(store, "default", "run", "worker", 60)
    coordinator = CommitCoordinator(store, "default", lease)
    original_replace, original_sync = _files.os.replace, _files.os.fsync
    published = False
    failed = False
    changes = 0

    def replace(source, target, **kwargs):
        nonlocal published
        original_replace(source, target, **kwargs)
        published = target == "manifest.json"

    def sync(fd):
        nonlocal failed
        if published and not failed:
            failed = True
            raise OSError(errno.EIO, "lost acknowledgement")
        original_sync(fd)

    def change(state):
        nonlocal changes
        changes += 1
        state["value"] += 1

    monkeypatch.setattr(_files.os, "replace", replace)
    monkeypatch.setattr(_files.os, "fsync", sync)
    state = coordinator.commit("increment", change)
    assert failed and changes == 1 and state["value"] == state["revision"] == 1
    with store.transaction("default") as tx:
        assert len(tx.list("commits")) == 1


def test_invalid_json_never_commits_and_shared_content_survives_reclamation(tmp_path):
    store = seed(tmp_path)
    with pytest.raises(ConfigurationError):
        with store.transaction("default") as tx:
            tx.put("runs", "counter", {"value": 9})
            tx.put("runs", "bad", float("nan"))
    assert store.load("default", "counter") == {"value": 0}
    with store.transaction("default") as tx:
        tx.put("runs", "shared", {"value": 0})
        tx.put("runs", "counter", {"value": 1})
    assert store.load("default", "shared") == {"value": 0}
    assert store.collect_garbage() == 0
    assert len(list(namespace_path(tmp_path).glob("objects/*.json"))) == 2


def test_idempotent_put_does_not_acknowledge_corrupted_content(tmp_path):
    store = seed(tmp_path)
    next(namespace_path(tmp_path).glob("objects/*.json")).write_text("corrupted")
    with pytest.raises(InfrastructureError, match="STORE_CORRUPT"):
        with store.transaction("default") as tx:
            tx.put("runs", "counter", {"value": 0})
