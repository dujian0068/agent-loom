"""Bounded model projections backed by immutable, committed effective results."""

import hashlib

from ..configuration.json_documents import canonical_document as canonical
from ..configuration.json_documents import document_digest as digest
from ..errors import AgentLoomError

PREVIEW_BYTES = 64 * 1024


def result_bytes(value):
    return (value if isinstance(value, str) else canonical(value)).encode("utf-8")


def text_slice(data, offset, limit):
    if (
        type(offset) is not int
        or not 0 <= offset <= len(data)
        or type(limit) is not int
        or not 1 <= limit <= PREVIEW_BYTES
    ):
        raise AgentLoomError("RESULT_SLICE_INVALID")
    try:
        data[:offset].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentLoomError("RESULT_SLICE_INVALID") from exc
    end = min(len(data), offset + limit)
    while end > offset:
        try:
            return data[offset:end].decode("utf-8"), end
        except UnicodeDecodeError:
            end -= 1
    return "", end


def project_result(response, run_id):
    if response["result"] is None:
        return response
    data = result_bytes(response["result"])
    if len(data) <= PREVIEW_BYTES:
        return response
    text, end = text_slice(data, 0, PREVIEW_BYTES)
    ref = {
        "run_id": run_id,
        "operation_id": response["operation_id"],
        "effective_result_hash": digest(response["result"]),
    }
    return {
        **response,
        "result": {
            "result_ref": ref,
            "media_type": "text/plain"
            if isinstance(response["result"], str)
            else "application/json",
            "total_bytes": len(data),
            "preview_start": 0,
            "preview_end": end,
            "text": text,
            "truncated": True,
            "source_operation_id": response["operation_id"],
            "effective_result_hash": ref["effective_result_hash"],
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    }


RESULT_READ_TOOL = {
    "name": "result_read",
    "description": "Read another UTF-8 slice of a result_ref previously returned to this task. Offsets and limits are bytes.",
    "parameters": {
        "type": "object",
        "properties": {
            "result_ref": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "operation_id": {"type": "string"},
                    "effective_result_hash": {"type": "string"},
                },
                "required": ["run_id", "operation_id", "effective_result_hash"],
                "additionalProperties": False,
            },
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": PREVIEW_BYTES},
        },
        "required": ["result_ref"],
        "additionalProperties": False,
    },
}


class ResultReadHandler:
    def __init__(self, runner):
        self.runner = runner

    async def handle(self, event, context):
        from ..configuration import validate_schema

        runner = self.runner
        runner.check_active()
        arguments = event.payload["arguments"]
        validate_schema(arguments, RESULT_READ_TOOL["parameters"])
        ref = arguments["result_ref"]
        current = runner.state()
        requester = current["operations"][event.operation_id]["task_id"]
        if ref["run_id"] == runner.run_id:
            source = current
            allowed_task = requester
        elif requester == current["root_task_id"] and ref["run_id"] in current["history_sources"]:
            source = runner.runtime._load(ref["run_id"])
            allowed_task = source["root_task_id"]
        else:
            raise AgentLoomError("RESULT_ACCESS_DENIED", category="permission")
        op = source["operations"].get(ref["operation_id"])
        if (
            not op
            or op["task_id"] != allowed_task
            or op["status"] == "pending"
            or op["event_type"] == "model.generate"
        ):
            raise AgentLoomError("RESULT_ACCESS_DENIED", category="permission")
        value = op["response"]["result"]
        if digest(value) != ref["effective_result_hash"]:
            raise AgentLoomError("RESULT_REFERENCE_INVALID")
        data = result_bytes(value)
        offset = arguments.get("offset", 0)
        text, end = text_slice(data, offset, arguments.get("limit", 16384))
        return {
            "result_ref": ref,
            "text": text,
            "offset": offset,
            "end_offset": end,
            "next_offset": end if end < len(data) else None,
            "total_bytes": len(data),
            "effective_result_hash": ref["effective_result_hash"],
        }
