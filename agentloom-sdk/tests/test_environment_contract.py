import difflib
from pathlib import Path

import pytest

from agentloom import AgentLoomError, LeaseLost, LocalEnvironment, WorkspaceBinding
from agentloom.capabilities import WorkspaceService


def test_file_versions_exact_patch_and_conflict(tmp_path):
    env = LocalEnvironment()
    scope = env.prepare(WorkspaceBinding(tmp_path), "session", "run", 1)
    first = env.write(scope, "a.txt", "one\ntwo\n")
    with pytest.raises(AgentLoomError, match="FILE_EXISTS"):
        env.write(scope, "a.txt", "overwrite")
    with pytest.raises(AgentLoomError, match="FILE_CHANGED"):
        env.write(scope, "a.txt", "wrong", mode="replace", if_match="stale")
    edited = env.edit(scope, "a.txt", "two", "three", first["etag"])
    assert edited["matched_count"] == 1
    diff = "".join(
        difflib.unified_diff(
            "one\nthree\n".splitlines(True),
            "one\nfour\n".splitlines(True),
            fromfile="a/a.txt",
            tofile="b/a.txt",
        )
    )
    patched = env.patch(scope, "a.txt", diff, edited["etag"])
    assert env.read(scope, "a.txt")["text"] == "one\nfour\n"
    with pytest.raises(AgentLoomError, match="FILE_CHANGED"):
        env.patch(scope, "a.txt", diff, edited["etag"])
    moved = env.move(scope, "a.txt", "b.txt", patched["etag"])
    assert moved["etag"] == patched["etag"] and not (Path(scope["path"]) / "a.txt").exists()
    env.delete(scope, "b.txt", moved["etag"])
    assert env.list(scope) == []


def test_epoch_fence_scope_grants_and_symlinks(tmp_path):
    env = LocalEnvironment()
    binding = WorkspaceBinding(tmp_path)
    first = env.prepare(binding, "one", "run", 1)
    env.write(first, "a.txt", "hello")
    second = env.prepare(binding, "one", "run", 2)
    with pytest.raises(LeaseLost):
        env.read(first, "a.txt")
    assert env.read(second, "a.txt")["text"] == "hello"
    for path in ("../escape", "/etc/passwd", "a/../../escape", "x\0y"):
        with pytest.raises(AgentLoomError, match="PATH_FORBIDDEN"):
            env.read(second, path)
    (Path(second["path"]) / "link").symlink_to(tmp_path)
    with pytest.raises(AgentLoomError, match="SYMLINK_FORBIDDEN"):
        env.write(second, "link/outside", "no")
    readonly = WorkspaceService(env, second, lambda: None, {"read_only": True})
    with pytest.raises(AgentLoomError, match="WORKSPACE_READ_ONLY"):
        readonly.write("new", "no")
    restricted = WorkspaceService(env, second, lambda: None, {"paths": ["allowed"]})
    with pytest.raises(AgentLoomError, match="PATH_FORBIDDEN"):
        restricted.read("a.txt")
    other = env.prepare(binding, "two", "different", 1)
    env.write(other, "retained", "yes")
    env.revoke(second)
    env.cleanup(second)
    assert not Path(second["path"]).exists() and (Path(other["path"]) / "retained").exists()


def test_persistent_workspace_and_search_past_first_slice(tmp_path):
    env = LocalEnvironment()
    binding = WorkspaceBinding(tmp_path, persistent_sandbox=True)
    first = env.prepare(binding, "s", "one", 1)
    env.write(first, "large.txt", "x" * 70000 + "\nNEEDLE\n")
    assert env.grep(first, "NEEDLE")[0]["line"] == 2
    env.mkdir(first, "sub")
    env.write(first, "sub/b.txt", "data")
    assert env.glob(first, "*.txt")["paths"] == ["large.txt"]
    assert env.glob(first, "**/*.txt")["paths"] == ["large.txt", "sub/b.txt"]
    env.revoke(first)
    env.cleanup(first)
    second = env.prepare(binding, "s", "two", 1)
    assert second["path"] == first["path"]
    with pytest.raises(LeaseLost):
        env.write(first, "stale", "no")
    assert env.read(second, "sub/b.txt")["text"] == "data"


def test_search_order_and_file_path_and_root_stat(tmp_path):
    env = LocalEnvironment()
    scope = env.prepare(WorkspaceBinding(tmp_path), "s", "r", 1)
    env.write(scope, "z.txt", "MATCH")
    env.mkdir(scope, "a")
    env.write(scope, "a/b.txt", "MATCH")
    service = WorkspaceService(env, scope, lambda: None, {})
    result = service.grep("MATCH", limit=1)
    assert result["truncated"] and result["matches"][0]["path"] == "a/b.txt"
    assert service.grep("MATCH", "z.txt")["matches"][0]["path"] == "z.txt"
    assert service.stat(".")["kind"] == "directory"
