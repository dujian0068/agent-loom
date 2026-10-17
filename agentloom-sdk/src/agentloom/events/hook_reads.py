"""Version-pinned, role-scoped Hook reads; raw audit fields are never exposed."""

from ..configuration import freeze
from ..errors import AgentLoomError
from .hook_views import HookRecordPage, HookRecordView

MESSAGE_FIELDS = frozenset({"content", "tool_calls", "tool_call_id", "name"})


class HookReadPort:
    def __init__(self, records, payload, context_id, revision, *, guard=None):
        self._payload = freeze(payload)
        self._ref = freeze({"context_id": context_id, "revision": revision})
        self._guard, self._active = guard, True
        self._records = tuple(
            HookRecordView(
                entry["record_id"],
                entry.get("context_seq", index),
                entry["message"]["role"],
                entry["kind"],
                {k: v for k, v in entry["message"].items() if k in MESSAGE_FIELDS},
                tuple(k for k in entry["message"] if k in MESSAGE_FIELDS),
            )
            for index, entry in enumerate(records, 1)
        )
        self._protected = tuple(entry["protected"] for entry in records)

    def _check(self):
        if not self._active:
            raise AgentLoomError("HOOK_READ_PORT_EXPIRED")
        if self._guard is not None:
            self._guard()

    def close(self):
        self._active = False

    @property
    def ref(self):
        self._check()
        return self._ref

    def messages(self):
        """Compatibility projection of the same authorized, immutable records."""
        self._check()
        return freeze(
            [
                {
                    "record_id": record.record_id,
                    "message": {"role": record.role, **record.effective_payload},
                    "protected": protected,
                }
                for record, protected in zip(self._records, self._protected)
            ]
        )

    async def read_payload(self):
        self._check()
        return self._payload

    async def read_context(self, ref, *, after_seq=0, limit=100):
        self._check()
        if not isinstance(ref, dict) or type(ref.get("revision")) is not int or ref != self._ref:
            raise AgentLoomError("HOOK_CONTEXT_FORBIDDEN")
        if (
            type(limit) is not int
            or not 1 <= limit <= 256
            or type(after_seq) is not int
            or after_seq < 0
        ):
            raise AgentLoomError("INVALID_CURSOR")
        remaining = [record for record in self._records if record.context_seq > after_seq]
        page = remaining[:limit]
        return HookRecordPage(page, page[-1].context_seq if len(remaining) > limit else None)
