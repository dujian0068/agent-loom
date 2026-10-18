"""Default SPI implementation delegates to the existing POSIX driver."""

from ..environment import LocalEnvironment
from ..environment.limits import DEFAULT_FILE_READ_BYTES
from .contracts import WorkspaceBackend


class LocalWorkspaceBackend(WorkspaceBackend):
    def __init__(self):
        self.environment = LocalEnvironment()

    def read(self, context, path, *, offset=0, limit=DEFAULT_FILE_READ_BYTES):
        return self.environment.read(
            context.scope, path, context.check_active, offset=offset, limit=limit
        )

    def write(self, context, path, content, *, mode="create_only", if_match=None):
        return self.environment.write(
            context.scope, path, content, context.check_active, mode=mode, if_match=if_match
        )

    def list(self, context, path=".", *, cursor=None, limit=100):
        return self.environment.list_page(
            context.scope, path, context.check_active, cursor=cursor, limit=limit
        )

    def mkdir(self, context, path, *, parents=False):
        return self.environment.mkdir(context.scope, path, context.check_active, parents=parents)

    def stat(self, context, path):
        return self.environment.stat(context.scope, path, context.check_active)

    def edit(self, context, path, old_text, new_text, if_match, *, expected_matches=1):
        return self.environment.edit(
            context.scope,
            path,
            old_text,
            new_text,
            if_match,
            context.check_active,
            expected_matches=expected_matches,
        )

    def patch(self, context, path, unified_diff, if_match):
        return self.environment.patch(
            context.scope, path, unified_diff, if_match, context.check_active
        )

    def move(self, context, source, destination, if_match):
        return self.environment.move(
            context.scope, source, destination, if_match, context.check_active
        )

    def delete(self, context, path, if_match, *, recursive=False):
        return self.environment.delete(
            context.scope, path, if_match, context.check_active, recursive=recursive
        )

    def glob(self, context, pattern, root=".", *, limit=1000):
        return self.environment.glob(
            context.scope, pattern, root, context.check_active, limit=limit
        )

    def grep(self, context, pattern, path=".", *, limit=1000, literal=True):
        matches = self.environment.grep(
            context.scope, pattern, path, context.check_active, limit=limit + 1, literal=literal
        )
        return {"matches": matches[:limit], "truncated": len(matches) > limit}
