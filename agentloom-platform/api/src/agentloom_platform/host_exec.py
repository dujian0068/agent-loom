"""Worker-side client; command processes never inherit the Worker's environment."""

import asyncio
import json
import socket
import time

from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, Field

from .exec_service import IMPLEMENTATION, PROTOCOL


def exec_available(settings):
    if not settings.exec_socket_path:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.3)
            client.connect(str(settings.exec_socket_path))
            client.sendall(json.dumps({"op": "ping", "protocol": PROTOCOL}).encode() + b"\n")
            result = json.loads(client.recv(1024))
            return (
                result.get("ready") is True
                and result.get("protocol") == PROTOCOL
                and result.get("implementation") == IMPLEMENTATION
            )
    except (OSError, ValueError):
        return False


class ExecArguments(BaseModel):
    command: str = Field(
        min_length=1, max_length=8192, description="Shell command to run in the current workspace"
    )
    timeout_seconds: int = Field(default=60, ge=1, le=300, description="Maximum command duration in seconds")


async def execute_command(settings, scope, command, timeout):
    if not settings.exec_socket_path:
        raise ToolException("本机执行服务未配置")
    if not command.strip() or "\0" in command or len(command.encode()) > 8192:
        raise ToolException("命令为空、包含无效字符或超过 8 KB")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(settings.exec_socket_path), limit=1024 * 1024), 3
        )
    except (OSError, TimeoutError):
        raise ToolException("本机执行服务不可用，请联系管理员") from None
    response = None
    try:
        writer.write(
            json.dumps(
                {
                    "op": "exec",
                    "protocol": PROTOCOL,
                    "implementation": IMPLEMENTATION,
                    "scope": scope,
                    "command": command,
                    "timeout": timeout,
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()
        response = asyncio.create_task(reader.readline())
        try:
            raw = await asyncio.wait_for(asyncio.shield(response), timeout + 8)
        except asyncio.CancelledError:
            writer.write(b'{"cancel":true}\n')
            await writer.drain()
            # Let the Worker mark the command's operation settled only after
            # the runner confirms its process group has exited.
            raw = await asyncio.wait_for(asyncio.shield(response), 5)
        result = json.loads(raw)
        if result.get("error") == "COMMAND_CLEANUP_UNCONFIRMED":
            raise RuntimeError("命令清理结果未确认，禁止自动重放")
        if result.get("error"):
            raise ToolException("本机执行被拒绝：" + str(result["error"]))
        if type(result.get("exit_code")) is not int:
            raise RuntimeError("命令执行回执无效")
        if result["exit_code"] != 0 or result.get("timed_out") or result.get("cancelled"):
            raise ToolException(json.dumps(result, ensure_ascii=False))
        return result
    finally:
        writer.close()  # Disconnect also cancels a dispatched command.
        await writer.wait_closed()
        if response and not response.done():
            response.cancel()


def command_tool(settings, scope, deadline):
    async def execute(command: str, timeout_seconds: int = 60):
        remaining = deadline - time.time()
        if remaining <= 0:
            raise ToolException("任务已超时")
        return await execute_command(settings, scope, command, min(timeout_seconds, remaining))

    return StructuredTool.from_function(
        name="exec",
        coroutine=execute,
        args_schema=ExecArguments,
        handle_tool_error=True,
        description="Run a shell command in this session's workspace on the execution host. "
        "Returns stdout, stderr, exit_code and truncation/timeout flags. "
        "No interactive input or persistent background jobs. This is not a sandbox.",
    )
