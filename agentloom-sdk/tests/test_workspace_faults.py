"""POSIX fault injection, failed-move compensation and process-death cleanup."""

import errno
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentloom import AgentLoomError, LeaseLost, LocalEnvironment, WorkspaceBinding


@pytest.fixture
def local(tmp_path):
    env = LocalEnvironment()
    scope = env.prepare(WorkspaceBinding(tmp_path.resolve()), "s", "r", 1)
    return env, scope


@pytest.mark.parametrize(
    "damage", ["missing", "json", "null", "boolean_epoch", "active_string", "symlink"]
)
def test_fence_damage_blocks_reads_writes_and_revoke(local, tmp_path, damage):
    env, scope = local
    env.write(scope, "a", "original")
    epoch = Path(scope["lock"] + ".epoch")
    if damage == "missing":
        epoch.unlink()
    elif damage == "symlink":
        copy = tmp_path / "outside-epoch"
        copy.write_bytes(epoch.read_bytes())
        epoch.unlink()
        epoch.symlink_to(copy)
    else:
        value = json.loads(epoch.read_text())
        if damage == "boolean_epoch":
            value["epoch"] = True
        elif damage == "active_string":
            value["active"] = "true"
        epoch.write_text(
            "bad" if damage == "json" else json.dumps(None if damage == "null" else value)
        )
    for call in (
        lambda: env.read(scope, "a"),
        lambda: env.write(scope, "b", "forbidden"),
        lambda: env.revoke(scope),
    ):
        with pytest.raises(LeaseLost):
            call()
    assert (Path(scope["path"]) / "a").read_text() == "original"
    assert not (Path(scope["path"]) / "b").exists()


def test_missing_fence_can_be_reprepared_under_new_epoch(local):
    env, scope = local
    env.write(scope, "a", "original")
    Path(scope["lock"] + ".epoch").unlink()
    # The caller supplies new execution authority; an ordinary file call cannot do this.
    binding = WorkspaceBinding(Path(scope["base"]).parents[1])
    new = env.prepare(binding, "s", "r", 2)
    assert new["path"] == scope["path"] and env.read(new, "a")["text"] == "original"
    with pytest.raises(LeaseLost):
        env.write(scope, "b", "old lease")


@pytest.mark.parametrize(
    "fault", ["permission", "rollback_denied", "destination_replaced", "source_removed"]
)
def test_failed_move_rolls_back_only_its_own_extra_link(local, monkeypatch, fault):
    env, scope = local
    version = env.write(scope, "source", "original")
    root = Path(scope["path"])
    unlink = os.unlink

    def fail(path, *args, **kwargs):
        if path == "source":
            if fault == "destination_replaced":
                unlink(root / "destination")
                (root / "destination").write_text("unrelated")
            elif fault == "source_removed":
                unlink(path, *args, **kwargs)
            raise PermissionError(errno.EACCES, "injected")
        if path == "destination" and fault == "rollback_denied":
            raise PermissionError(errno.EACCES, "injected rollback")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", fail)
    if fault == "permission":
        with pytest.raises(PermissionError):
            env.move(scope, "source", "destination", version["etag"])
        assert (root / "source").read_text() == "original"
        assert not (root / "destination").exists()
    else:
        with pytest.raises(AgentLoomError, match="FILE_MOVE_OUTCOME_UNKNOWN") as caught:
            env.move(scope, "source", "destination", version["etag"])
        assert caught.value.external_outcome == "unknown" and not caught.value.retryable_transport
        assert (root / "destination").read_text() == (
            "unrelated" if fault == "destination_replaced" else "original"
        )


def test_cross_device_move_is_explicit_and_has_no_destination(local, monkeypatch):
    env, scope = local
    version = env.write(scope, "source", "original")

    def cross_device(*args, **kwargs):
        raise OSError(errno.EXDEV, "injected")

    monkeypatch.setattr(os, "link", cross_device)
    with pytest.raises(AgentLoomError, match="FILE_MOVE_CROSS_DEVICE") as caught:
        env.move(scope, "source", "destination", version["etag"])
    assert caught.value.external_outcome == "not_started"
    assert not (Path(scope["path"]) / "destination").exists()


def test_no_follow_covers_last_component_race(local, tmp_path, monkeypatch):
    env, scope = local
    env.write(scope, "race", "public")
    secret = tmp_path / "secret"
    secret.write_text("not readable")
    original_open, swapped = os.open, []

    def swap(path, flags, *args, **kwargs):
        if path == "race" and flags & os.O_NOFOLLOW and not swapped:
            swapped.append(True)
            target = Path(scope["path"]) / "race"
            target.unlink()
            target.symlink_to(secret)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap)
    with pytest.raises(AgentLoomError, match="SYMLINK_FORBIDDEN"):
        env.read(scope, "race")
    assert swapped


@pytest.mark.parametrize("code", [errno.ENOTDIR, errno.ELOOP])
def test_parent_does_not_reclassify_body_errors(local, code):
    env, scope = local
    original = OSError(code, "body")
    with pytest.raises(OSError) as caught:
        with env._parent(scope, "a"):
            raise original
    assert caught.value is original


@pytest.mark.parametrize("value", [None, 0, 1001, 1.5, True])
def test_local_list_limit_is_not_a_cursor_error(local, value):
    env, scope = local
    with pytest.raises(AgentLoomError, match="FILE_ARGUMENTS_INVALID"):
        env.list_page(scope, limit=value)


@pytest.mark.parametrize("value", [None, 5, [], {}])
def test_local_edit_rejects_nontext_before_writing(local, value):
    env, scope = local
    version = env.write(scope, "a", "original")
    with pytest.raises(AgentLoomError, match="EDIT_ARGUMENTS_INVALID"):
        env.edit(scope, "a", "original", value, version["etag"])
    assert env.read(scope, "a")["text"] == "original"


def test_patch_cannot_create_or_implicitly_delete(local):
    env, scope = local
    version = env.write(scope, "a", "line\n")
    for first, second in (("/dev/null", "b/a"), ("a/a", "/dev/null")):
        with pytest.raises(AgentLoomError, match="PATCH_PATH_MISMATCH"):
            env.patch(
                scope,
                "a",
                f"--- {first}\n+++ {second}\n@@ -1 +1 @@\n-line\n+new\n",
                version["etag"],
            )
    assert env.read(scope, "a")["text"] == "line\n"


def test_process_death_staging_is_collected_without_touching_published_file(tmp_path):
    env = LocalEnvironment()
    binding = WorkspaceBinding(tmp_path.resolve(), persistent_sandbox=True)
    scope = env.prepare(binding, "s", "r", 1)
    code = """
import json, os, signal, sys
from agentloom.environment import LocalEnvironment
scope = json.loads(sys.argv[1])
original = os.unlink
def kill(path, *args, **kwargs):
    if str(path).startswith(".agentloom-write-"):
        os.kill(os.getpid(), signal.SIGKILL)
    return original(path, *args, **kwargs)
os.unlink = kill
LocalEnvironment().write(scope, "published", "value")
"""
    child = subprocess.run(
        [sys.executable, "-c", code, json.dumps(scope)], capture_output=True, timeout=10
    )
    assert child.returncode == -9
    root = Path(scope["path"])
    assert len(list(root.glob(".agentloom-write-*"))) == 1
    (root / ".agentloom-write-not-owned").write_text("keep")
    next_scope = env.prepare(binding, "s", "r", 2)
    assert [p.name for p in root.glob(".agentloom-write-*")] == [".agentloom-write-not-owned"]
    assert env.read(next_scope, "published")["text"] == "value"


def test_write_syncs_directory_after_staging_unlink(local, monkeypatch):
    env, scope = local
    original_sync, original_unlink = os.fsync, os.unlink
    events = []

    def sync(fd):
        events.append("sync")
        return original_sync(fd)

    def unlink(path, *args, **kwargs):
        if str(path).startswith(".agentloom-write-"):
            events.append("unlink")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "unlink", unlink)
    env.write(scope, "a", "value")
    assert events[-2:] == ["unlink", "sync"]


def test_staging_cleanup_does_not_follow_external_links(tmp_path):
    env = LocalEnvironment()
    binding = WorkspaceBinding(tmp_path.resolve() / "workspace", persistent_sandbox=True)
    scope = env.prepare(binding, "s", "r", 1)
    root = Path(scope["path"])
    outside = tmp_path / "outside"
    outside.mkdir()
    name = ".agentloom-write-" + "a" * 24
    (outside / name).write_text("keep external data")
    (root / "link").symlink_to(outside, target_is_directory=True)
    (root / name).symlink_to(outside / name)
    (root / "nested").mkdir()
    (root / "nested" / name).write_text("stale local data")
    env.prepare(binding, "s", "r", 2)
    assert (outside / name).read_text() == "keep external data"
    assert (root / name).is_symlink()
    assert not (root / "nested" / name).exists()


def test_provider_close_preserves_control_error():
    from types import SimpleNamespace

    from agentloom.workspace.runtime import WorkspaceResources

    error = LeaseLost()

    def close():
        raise error

    resources = WorkspaceResources(None, None)
    resources.backend = SimpleNamespace(close=close)
    with pytest.raises(LeaseLost) as caught:
        resources.close()
    assert caught.value is error
