"""Prepared Skill packages and directory imports share one metadata contract."""

import hashlib

import pytest
import yaml

from agentloom import ConfigurationError, DirectorySkillLoader, SkillPackage


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


@pytest.mark.parametrize(
    "body", ["Dynamic !`echo value`", "Session ${CLAUDE_SESSION_ID}", "${CODEX_ROOT}"]
)
def test_prepared_skill_body_is_validated_at_read_boundary(tmp_path, body):
    metadata = {"name": "test", "description": "test"}
    text = "---\n" + yaml.safe_dump(metadata) + "---\n" + body
    (tmp_path / "SKILL.md").write_text(text)
    package = SkillPackage(
        tmp_path, metadata, {"SKILL.md": hashlib.sha256(text.encode()).hexdigest()}, "test"
    )
    with pytest.raises(ConfigurationError, match="SKILL_PROFILE_UNSUPPORTED"):
        SkillPackage.from_directory(tmp_path)
    with pytest.raises(ConfigurationError, match="SKILL_PROFILE_UNSUPPORTED"):
        DirectorySkillLoader().read_entry(package)
    with pytest.raises(ConfigurationError, match="SKILL_PROFILE_UNSUPPORTED"):
        DirectorySkillLoader().read_resource(package, "SKILL.md")


def test_argument_values_are_literal_after_source_validation(tmp_path):
    text = "---\nname: test\ndescription: test\n---\n$ARGUMENTS"
    (tmp_path / "SKILL.md").write_text(text)
    package = SkillPackage.from_directory(tmp_path)
    arguments = "!`echo ignored` ${CLAUDE_SESSION_ID} $ARGUMENTS"
    rendered = DirectorySkillLoader().read_entry(package, raw_arguments=arguments)
    assert rendered["text"].endswith(arguments)
