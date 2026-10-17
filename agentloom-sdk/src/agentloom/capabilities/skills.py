"""Immutable Skill manifests and on-demand, non-executing resource reads."""

import hashlib
import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from ..configuration import digest, freeze
from ..errors import AgentLoomError, ConfigurationError
from .skill_profile import validate_body, validate_metadata


def parse_arguments(raw_arguments=None, arguments=None):
    if raw_arguments is not None and arguments is not None:
        raise AgentLoomError("SKILL_ARGUMENTS_INVALID")
    if raw_arguments is not None and not isinstance(raw_arguments, str):
        raise AgentLoomError("SKILL_ARGUMENTS_INVALID")
    if arguments is not None and (
        not isinstance(arguments, (list, tuple)) or any(not isinstance(a, str) for a in arguments)
    ):
        raise AgentLoomError("SKILL_ARGUMENTS_INVALID")
    try:
        words = (
            shlex.split(raw_arguments, comments=False, posix=True)
            if raw_arguments is not None
            else list(arguments or [])
        )
    except ValueError as exc:
        raise AgentLoomError("SKILL_ARGUMENTS_INVALID") from exc
    return (raw_arguments if raw_arguments is not None else shlex.join(words)), words


@dataclass(frozen=True)
class SkillPackage:
    root: Path
    metadata: dict
    files_manifest: dict
    package_key: str

    def __post_init__(self):
        validate_metadata(self.metadata)
        object.__setattr__(self, "root", Path(self.root).absolute())
        object.__setattr__(self, "metadata", freeze(self.metadata))
        object.__setattr__(self, "files_manifest", freeze(self.files_manifest))
        if (
            not isinstance(self.package_key, str)
            or not self.package_key
            or "SKILL.md" not in self.files_manifest
        ):
            raise ConfigurationError("SKILL_PACKAGE_INVALID")

    @classmethod
    def from_directory(cls, root, *, package_key=None):
        root = Path(root).absolute()
        if root.is_symlink():
            raise ConfigurationError("SKILL_PATH_FORBIDDEN")
        try:
            text = (root / "SKILL.md").read_text(encoding="utf-8")
            match = re.match(r"\A---\s*\n(.*?)\n---(?:\n|$)", text, re.S)
            if not match:
                raise ConfigurationError("SKILL_METADATA_INVALID")
            metadata = yaml.safe_load(match[1])
            validate_metadata(metadata)
            validate_body(text)
            manifest = {}
            for path in sorted(root.rglob("*")):
                if path.is_symlink():
                    raise ConfigurationError("SKILL_PATH_FORBIDDEN")
                if path.is_file():
                    manifest[path.relative_to(root).as_posix()] = hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
            return cls(root, freeze(metadata), freeze(manifest), package_key or metadata["name"])
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ConfigurationError("SKILL_PACKAGE_INVALID", type(exc).__name__) from exc


class DirectorySkillLoader:
    def list_entries(self, package):
        return [
            {
                "resource_key": package.package_key,
                "entry_name": package.metadata["name"],
                "name": package.metadata["name"],
                "description": package.metadata["description"],
                "entry_path": "SKILL.md",
            }
        ]

    def read_resource(self, package, path):
        parts = PurePosixPath(path).parts
        if not parts or path.startswith("/") or ".." in parts or "\0" in path or "\\" in path:
            raise AgentLoomError("SKILL_PATH_FORBIDDEN", category="permission")
        path = PurePosixPath(*parts).as_posix()
        expected = package.files_manifest.get(path)
        if expected is None:
            raise AgentLoomError("SKILL_PATH_FORBIDDEN", category="permission")
        # Reuse the no-follow dirfd walker, including package-root ancestors.
        from ..environment.local import LocalEnvironment, _directory

        parent = package.root.joinpath(*parts[:-1])
        try:
            import os

            fd = _directory(parent)
            try:
                data = LocalEnvironment._read_bytes(fd, parts[-1])
            finally:
                os.close(fd)
        except OSError as exc:
            raise AgentLoomError("SKILL_CONTENT_UNAVAILABLE", category="not_found") from exc
        if hashlib.sha256(data).hexdigest() != expected:
            raise AgentLoomError("SKILL_CONTENT_CHANGED", category="conflict")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if path == "SKILL.md":
            validate_body(text)
        return {
            "text": text,
            "resource_key": package.package_key,
            "path": path,
            "sha256": expected,
            "size": len(data),
            "rendered_arguments_hash": None,
        }

    def read_entry(self, package, *, raw_arguments=None, arguments=None):
        raw, words = parse_arguments(raw_arguments, arguments)
        result = self.read_resource(package, "SKILL.md")
        result["text"] = re.sub(
            r"\$ARGUMENTS|\$([1-9][0-9]*)",
            lambda m: (
                raw
                if m[0] == "$ARGUMENTS"
                else (words[int(m[1]) - 1] if int(m[1]) <= len(words) else "")
            ),
            result["text"],
        )
        if raw_arguments is not None or arguments is not None:
            result["rendered_arguments_hash"] = digest(
                {
                    "render_version": "skill-args-v1",
                    "raw_arguments": raw_arguments,
                    "arguments": arguments,
                }
            )
        return result
