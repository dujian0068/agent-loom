"""Logical root aliases preserve glob matching and the authorized search boundary."""

from pathlib import Path

import pytest

from agentloom import AgentLoomError, LocalEnvironment, WorkspaceBinding
from agentloom.workspace.paths import path_parts
from agentloom.workspace.service import WorkspaceService


@pytest.fixture
def local_workspace(tmp_path):
    environment = LocalEnvironment()
    scope = environment.prepare(WorkspaceBinding(tmp_path / "work"), "s", "r", 1)
    files = WorkspaceService(environment, scope, lambda: None, {})
    files.mkdir("sub/深层", parents=True)
    files.write("sub/a.txt", "one")
    files.write("sub/深层/中.txt", "two")
    files.write("sub/ignored.md", "three")
    files.write("outside.txt", "private")
    return environment, scope, files


@pytest.mark.parametrize("root", ["./sub", "sub/./", "/work/./sub/.", "sub//", "/work//sub//"])
def test_glob_aliases_preserve_recursion_order_and_limits(local_workspace, root):
    files = local_workspace[2]
    result = files.glob("**/*.txt", root=root)
    assert [path_parts(path) for path in result["paths"]] == [
        ("sub", "a.txt"),
        ("sub", "深层", "中.txt"),
    ]
    assert result["truncated"] is False
    assert files.glob("**/*.txt", root=root, limit=1) == {
        "paths": result["paths"][:1],
        "truncated": True,
    }
    directories = files.glob("*", root=root)["paths"]
    assert ("sub", "深层") in [path_parts(path) for path in directories]
    assert files.glob("*.absent", root=root) == {"paths": [], "truncated": False}


@pytest.mark.parametrize("root", [".", "./", "././", "/work", "/work/.", "/work//./"])
def test_workspace_root_aliases_match_direct_children(local_workspace, root):
    result = local_workspace[2].glob("*.txt", root=root)
    assert [path_parts(path) for path in result["paths"]] == [("outside.txt",)]


@pytest.mark.parametrize("root", ["../sub", "sub/../sub", "/work/sub/../sub", "/tmp", "/work/."])
def test_glob_alias_normalization_does_not_expand_authorization(local_workspace, root):
    environment, scope, _ = local_workspace
    files = WorkspaceService(environment, scope, lambda: None, {"paths": ["sub"]})
    with pytest.raises(AgentLoomError, match="PATH_FORBIDDEN"):
        files.glob("**/*", root=root)


def test_glob_still_omits_symlinks_with_aliased_root(local_workspace, tmp_path):
    _, scope, files = local_workspace
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "leak.txt").write_text("private")
    (Path(scope["path"]) / "sub" / "link").symlink_to(outside, target_is_directory=True)
    result = files.glob("**/*.txt", root="/work/./sub/.")
    assert len(result["paths"]) == 2
    assert not any("leak" in path for path in result["paths"])


@pytest.mark.parametrize("operation", ["read", "write", "delete", "list", "glob", "grep"])
def test_repeated_alias_separator_cannot_address_host_files(local_workspace, tmp_path, operation):
    _, scope, files = local_workspace
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.txt").write_text("PRIVATE")
    (outside / "private.txt").write_text("PRIVATE")
    logical = str(outside).lstrip("/")
    files.mkdir(logical, parents=True)
    version = files.write(logical + "/a.txt", "public")
    alias = "/work/" + str(outside)
    assert path_parts(alias) == path_parts(logical)
    if operation == "read":
        assert files.read(alias + "/a.txt")["text"] == "public"
    elif operation == "write":
        files.write(alias + "/a.txt", "changed", mode="replace", if_match=version["etag"])
        assert files.read(logical + "/a.txt")["text"] == "changed"
    elif operation == "delete":
        files.delete(alias + "/a.txt", version["etag"])
        assert not (Path(scope["path"]) / logical / "a.txt").exists()
    elif operation == "list":
        assert [entry["name"] for entry in files.list(alias)["entries"]] == ["a.txt"]
    elif operation == "glob":
        assert [Path(path).name for path in files.glob("*.txt", alias)["paths"]] == ["a.txt"]
    else:
        assert files.grep("PRIVATE", alias)["matches"] == []
    assert (outside / "a.txt").read_text() == "PRIVATE"
    assert (outside / "private.txt").read_text() == "PRIVATE"
