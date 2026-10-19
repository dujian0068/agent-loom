"""Skill imports and resource reads share path, size and error contracts."""

import errno
import hashlib
import os

import pytest

from agentloom import AgentLoomError, ConfigurationError, DirectorySkillLoader, SkillPackage
from agentloom.capabilities import skill_files
from agentloom.filesystem.posix import MAX_FILE_BYTES


def package(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text("---\nname: test\ndescription: test skill\n---\nBody")
    (root / "asset").write_text("resource")
    return root


@pytest.mark.parametrize("location", ["root", "ancestor", "file", "directory"])
def test_import_rejects_links_before_accepting_package(tmp_path, location):
    root = package(tmp_path / "actual" / "package")
    if location == "root":
        (tmp_path / "alias").symlink_to(root, target_is_directory=True)
        root = tmp_path / "alias"
    elif location == "ancestor":
        (tmp_path / "alias").symlink_to(root.parent, target_is_directory=True)
        root = tmp_path / "alias" / "package"
    else:
        (root / "link").symlink_to(
            root if location == "directory" else root / "asset",
            target_is_directory=location == "directory",
        )
    with pytest.raises(ConfigurationError, match="SKILL_PATH_FORBIDDEN"):
        SkillPackage.from_directory(root)


def test_import_and_read_share_resource_size_limit(tmp_path):
    root = package(tmp_path)
    with (root / "asset").open("wb") as fp:
        fp.truncate(MAX_FILE_BYTES + 1)
    with pytest.raises(ConfigurationError, match="SKILL_CONTENT_TOO_LARGE"):
        SkillPackage.from_directory(root)
    prepared = SkillPackage(
        root,
        {"name": "test", "description": "test"},
        {"SKILL.md": "unused", "asset": "unused"},
        "test",
    )
    with pytest.raises(AgentLoomError, match="SKILL_CONTENT_TOO_LARGE") as caught:
        DirectorySkillLoader().read_resource(prepared, "asset")
    assert caught.value.category == "validation"


def test_import_limit_includes_skill_entry(tmp_path):
    root = package(tmp_path)
    with (root / "SKILL.md").open("ab") as fp:
        fp.truncate(MAX_FILE_BYTES + 1)
    with pytest.raises(ConfigurationError, match="SKILL_CONTENT_TOO_LARGE"):
        SkillPackage.from_directory(root)


def test_exact_limit_is_readable_and_manifest_is_unchanged(tmp_path):
    root = package(tmp_path)
    data = b"x" * MAX_FILE_BYTES
    (root / "asset").write_bytes(data)
    loaded = SkillPackage.from_directory(root)
    read = DirectorySkillLoader().read_resource(loaded, "asset")
    assert read["size"] == MAX_FILE_BYTES and read["sha256"] == hashlib.sha256(data).hexdigest()
    (root / "asset").write_text("changed")
    with pytest.raises(AgentLoomError, match="SKILL_CONTENT_CHANGED"):
        DirectorySkillLoader().read_resource(loaded, "asset")


@pytest.mark.parametrize(
    "code,category",
    [(errno.ENOENT, "not_found"), (errno.EACCES, "permission"), (errno.EIO, "unknown")],
)
def test_resource_io_errors_keep_skill_category(tmp_path, monkeypatch, code, category):
    loaded = SkillPackage.from_directory(package(tmp_path))

    def fail(*args, **kwargs):
        raise OSError(code, "private path")

    monkeypatch.setattr(skill_files, "read_regular", fail)
    with pytest.raises(AgentLoomError) as caught:
        DirectorySkillLoader().read_resource(loaded, "asset")
    assert caught.value.code.startswith("SKILL_") and caught.value.category == category
    assert "private path" not in str(caught.value)


def test_import_last_component_swap_is_not_followed(tmp_path, monkeypatch):
    root = package(tmp_path / "root")
    secret = tmp_path / "secret"
    secret.write_text("private")
    original, swapped = os.open, []

    def swap(path, flags, *args, **kwargs):
        if path == "asset" and flags & os.O_NOFOLLOW and not swapped:
            swapped.append(True)
            (root / "asset").unlink()
            (root / "asset").symlink_to(secret)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap)
    with pytest.raises(ConfigurationError, match="SKILL_PATH_FORBIDDEN"):
        SkillPackage.from_directory(root)
    assert swapped


def test_resource_ancestor_replaced_after_import_is_forbidden(tmp_path):
    root = package(tmp_path / "parent" / "pkg")
    loaded = SkillPackage.from_directory(root)
    (tmp_path / "parent").rename(tmp_path / "moved")
    (tmp_path / "parent").symlink_to(tmp_path / "moved", target_is_directory=True)
    with pytest.raises(AgentLoomError, match="SKILL_PATH_FORBIDDEN") as caught:
        DirectorySkillLoader().read_resource(loaded, "asset")
    assert caught.value.category == "permission"
