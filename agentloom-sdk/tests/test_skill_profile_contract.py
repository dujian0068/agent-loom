"""Prepared Skill packages and directory imports share one metadata contract."""

import hashlib

import pytest
import yaml

from agentloom import ConfigurationError, SkillPackage


@pytest.mark.parametrize(
    "patch,code",
    [
        ({"allowed-tools": "read"}, "SKILL_PROFILE_UNSUPPORTED"),
        ({"disallowed-tools": "write"}, "SKILL_PROFILE_UNSUPPORTED"),
        ({"model": "other"}, "SKILL_PROFILE_UNSUPPORTED"),
        ({"context": "fork"}, "SKILL_PROFILE_UNSUPPORTED"),
        ({"effort": "high"}, "SKILL_PROFILE_UNSUPPORTED"),
        ({"user-invocable": "false"}, "SKILL_METADATA_INVALID"),
        ({"disable-model-invocation": 0}, "SKILL_METADATA_INVALID"),
        ({"name": " "}, "SKILL_METADATA_INVALID"),
    ],
)
def test_skill_metadata_is_checked_by_both_construction_paths(tmp_path, patch, code):
    metadata = {"name": "test", "description": "test skill", **patch}
    text = "---\n" + yaml.safe_dump(metadata) + "---\nBody"
    (tmp_path / "SKILL.md").write_text(text)
    manifest = {"SKILL.md": hashlib.sha256(text.encode()).hexdigest()}
    with pytest.raises(ConfigurationError, match=code):
        SkillPackage(tmp_path, metadata, manifest, "test")
    with pytest.raises(ConfigurationError, match=code):
        SkillPackage.from_directory(tmp_path)


def test_supported_prepared_package_is_frozen(tmp_path):
    metadata = {"name": "test", "description": "test", "user-invocable": False}
    text = "---\n" + yaml.safe_dump(metadata) + "---\nBody"
    (tmp_path / "SKILL.md").write_text(text)
    manifest = {"SKILL.md": hashlib.sha256(text.encode()).hexdigest()}
    package = SkillPackage(tmp_path, metadata, manifest, "test")
    metadata["allowed-tools"] = "read"
    assert "allowed-tools" not in package.metadata
    assert package.metadata["user-invocable"] is False
