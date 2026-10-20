"""Loopback OpenAI-compatible fixture. Never calls a provider or logs credentials."""

import json
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

directory = Path(sys.argv[2])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        names = [tool["function"]["name"] for tool in body.get("tools", [])]
        system = next((m["content"] for m in body["messages"] if m["role"] in {"system", "developer"}), "")
        phase = (
            "plan"
            if "Plan" in names
            else ("research" if "研究任务" in system else "writer" if "撰写报告" in system else "finish")
        )
        tool_results = [m for m in body["messages"] if m["role"] == "tool"]
        with (directory / "requests.jsonl").open("a") as log:
            log.write(json.dumps({"phase": phase, "tools": names, "tool_results": len(tool_results)}) + "\n")
        if body["model"] == "http-failure":
            self.send_json(500, {"error": {"message": "private-provider-diagnostic", "type": "server_error"}})
            return
        calls = []
        if phase == "plan":
            tasks = [{"executor": key, "instruction": f"Do {key} task"} for key in ("research", "writer")]
            if body["model"] == "unknown-executor":
                tasks[0]["executor"] = "not-authorized"
            calls = [self.call("Plan", {"tasks": tasks})]
        elif phase == "research" and not tool_results:
            calls = [self.call("write_file", {"path": "research.txt", "content": "Completed research"})]
        elif phase == "writer" and body["model"] == "plan-crash":
            (directory / "writer-entered").touch()
            deadline = time.monotonic() + 45
            while not (directory / "writer-released").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
        message = {"role": "assistant", "content": None if calls else f"{phase} complete"}
        if calls:
            message["tool_calls"] = calls
        self.send_json(
            200,
            {
                "id": "chatcmpl-" + uuid.uuid4().hex,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body["model"],
                "choices": [
                    {"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
        )

    @staticmethod
    def call(name, arguments):
        return {
            "id": "call_0",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }

    def send_json(self, status, body):
        data = json.dumps(body).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Expected when the test SIGKILLs a Worker with an open request.


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
