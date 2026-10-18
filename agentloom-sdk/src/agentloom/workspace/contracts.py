"""Synchronous file-provider SPI. Providers own their I/O and concurrency model."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Mapping, NotRequired, TypedDict

from ..configuration.json_values import freeze
from ..environment.limits import DEFAULT_FILE_READ_BYTES

FILE_OPERATIONS = (
    "read",
    "write",
    "list",
    "mkdir",
    "stat",
    "edit",
    "patch",
    "move",
    "delete",
    "glob",
    "grep",
)


class FileVersion(TypedDict):
    path: str
    kind: str
    size: int
    etag: str
    sha256: NotRequired[str]


class FileRead(FileVersion):
    text: str
    offset: int
    end_offset: int
    next_offset: int | None
    total_bytes: int


class DirectoryEntry(FileVersion):
    name: str


class DirectoryPage(TypedDict):
    entries: list[DirectoryEntry]
    etag: str
    next_cursor: str | None


class FileEdit(FileVersion):
    matched_count: int


class FilePatch(FileVersion):
    hunks_applied: int


class DirectoryCreated(TypedDict):
    path: str
    kind: str


class FileDeleted(TypedDict):
    deleted_path: str
    previous_etag: str


class GlobResult(TypedDict):
    paths: list[str]
    truncated: bool


class GrepMatch(TypedDict):
    path: str
    line: int
    column: int
    text: str


class GrepResult(TypedDict):
    matches: list[GrepMatch]
    truncated: bool


@dataclass(frozen=True, kw_only=True)
class WorkspaceCallContext:
    """Trusted SDK identity, never supplied by model arguments.

    workspace_id is stable across recovery, and across runs only for a persistent
    session workspace. Namespace and session/run identity must partition backend
    storage. scope is an immutable SDK descriptor, not provider credentials.
    check_active must be called before committing effects and during long I/O;
    use lease_epoch for backend fencing where required. Providers must finish or
    stop their writers before returning/raising, and respect deadline_at. SDK does
    not create an executor, event loop or hidden retries for these plain methods.
    Lifecycle contexts have purpose prepare/revoke/cleanup and no tool attempt.
    cleanup_job_id/cleanup_lease_epoch identify the independent cleanup authority;
    lease_epoch still identifies the original workspace execution domain.
    """

    workspace_id: str
    scope: Mapping = field(repr=False)
    check_active: Callable[[], None] = field(repr=False, compare=False)
    namespace: str = "default"
    session_id: str | None = None
    run_id: str = ""
    lease_epoch: int = 0
    role_key: str | None = None
    operation_id: str | None = None
    attempt_id: str | None = None
    attempt_no: int | None = None
    deadline_at: float | None = None
    purpose: str = "tool"
    cleanup_job_id: str | None = None
    cleanup_lease_epoch: int | None = None
    access_scope: Mapping = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "scope", freeze(self.scope))
        object.__setattr__(self, "access_scope", freeze(self.access_scope))


class WorkspaceBackend(ABC):
    """Implement all eleven methods and register the class through entry points.

    Paths are logical /work paths or relative paths, never host locations. Return
    the DTO dictionaries below; etags are opaque and conditional mutations must
    be atomic with version verification. Raise AgentLoomError for structured
    conflicts/transport errors, with no credentials/host paths in error text.
    A method performs one attempt; SDK owns Hook, retry and persistence policy.
    An instance is reused by one host and must support concurrent run/cleanup
    calls. Optional lifecycle methods below have usable defaults. close is owned
    by SDK, must be idempotent and retryable after failure.
    """

    @abstractmethod
    def read(
        self,
        context: WorkspaceCallContext,
        path: str,
        *,
        offset: int = 0,
        limit: int = DEFAULT_FILE_READ_BYTES,
    ) -> FileRead:
        """Read a UTF-8 byte slice at one version; retain UTF-8 boundaries."""

    @abstractmethod
    def write(
        self,
        context: WorkspaceCallContext,
        path: str,
        content: str,
        *,
        mode: str = "create_only",
        if_match: str | None = None,
    ) -> FileVersion:
        """Create exclusively, or replace only when the supplied etag matches."""

    @abstractmethod
    def list(
        self,
        context: WorkspaceCallContext,
        path: str = ".",
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> DirectoryPage:
        """Page immediate children in UTF-8 path order; reject stale cursors."""

    @abstractmethod
    def mkdir(
        self, context: WorkspaceCallContext, path: str, *, parents: bool = False
    ) -> DirectoryCreated:
        """Create a directory; existing directories succeed, files conflict."""

    @abstractmethod
    def stat(self, context: WorkspaceCallContext, path: str) -> FileVersion:
        """Return file/directory metadata and its current opaque version."""

    @abstractmethod
    def edit(
        self,
        context: WorkspaceCallContext,
        path: str,
        old_text: str,
        new_text: str,
        if_match: str,
        *,
        expected_matches: int = 1,
    ) -> FileEdit:
        """Exact text replacement at the expected version and match count."""

    @abstractmethod
    def patch(
        self, context: WorkspaceCallContext, path: str, unified_diff: str, if_match: str
    ) -> FilePatch:
        """Apply an exact single-file unified diff, with no fuzzy matching."""

    @abstractmethod
    def move(
        self, context: WorkspaceCallContext, source: str, destination: str, if_match: str
    ) -> FileVersion:
        """Move a matching file without overwriting the destination."""

    @abstractmethod
    def delete(
        self, context: WorkspaceCallContext, path: str, if_match: str, *, recursive: bool = False
    ) -> FileDeleted:
        """Delete a matching version; recursive directories require opt-in."""

    @abstractmethod
    def glob(
        self, context: WorkspaceCallContext, pattern: str, root: str = ".", *, limit: int = 1000
    ) -> GlobResult:
        """Return sorted logical paths; support component globs and **."""

    @abstractmethod
    def grep(
        self,
        context: WorkspaceCallContext,
        pattern: str,
        path: str = ".",
        *,
        limit: int = 1000,
        literal: bool = True,
    ) -> GrepResult:
        """Literal text search ordered by path/line/column; positions are 1-based."""

    def prepare(self, context: WorkspaceCallContext) -> None:
        """Optionally provision this logical workspace or acquire backend fences."""

    def revoke(self, context: WorkspaceCallContext) -> None:
        """Optionally revoke remote writers; return only when they are quiescent."""

    def cleanup(self, context: WorkspaceCallContext) -> None:
        """Remove temporary workspace contents using the independent cleanup lease.

        SDK never invokes this for persistent workspaces. Override to also remove
        provider-owned containers/metadata, preserving the same scope and fence.
        """
        from .service import WorkspaceService

        service = WorkspaceService(
            None, context.scope, context.check_active, {}, backend=self, context=context
        )
        while True:
            page = service.list(limit=100)
            if not page["entries"]:
                return
            for entry in page["entries"]:
                context.check_active()
                try:
                    service.delete(entry["path"], entry["etag"], recursive=True)
                except FileNotFoundError:
                    pass  # Safe to resume a partially completed cleanup.

    def close(self) -> None:
        """Release this host's resources; default providers hold no resources."""
