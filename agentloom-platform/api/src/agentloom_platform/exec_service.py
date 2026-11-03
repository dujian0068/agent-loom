"""Credential-free, same-host command runner. Run under a dedicated Unix account.

This service is not a per-task sandbox. Only the configured Worker UID may use
its Unix socket. The system service grants access to the workspace mount only;
it never loads Platform settings, model keys or database credentials.
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import resource
import signal
import socket
import stat
import struct
from pathlib import Path

PROTOCOL = 1
IMPLEMENTATION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
OUTPUT_LIMIT = 64 * 1024  # Per stream; keep draining after truncation.
MAX_TIMEOUT = 300


def peer_uid(writer):
    peer = writer.get_extra_info("socket")
    if hasattr(socket, "SO_PEERCRED"):
        return struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    if hasattr(peer, "getpeereid"):
        return peer.getpeereid()[0]
    return None


async def capture(stream):
    output = bytearray()
    truncated = False
    while chunk := await stream.read(8192):
        remaining = OUTPUT_LIMIT - len(output)
        output.extend(chunk[:remaining])
        truncated |= len(chunk) > remaining
    return output.decode("utf-8", errors="replace"), truncated


async def terminate_group(process):
    # Always kill the group, even if its shell has already exited: background
    # children may still hold files or output pipes open.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        if sig == signal.SIGTERM:
            await asyncio.sleep(0.2)
    await process.wait()


async def run_command(workspace, command, timeout):
    def limits():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        resource.setrlimit(resource.RLIMIT_CPU, (math.ceil(timeout) + 1,) * 2)

    # Never inherit even apparently harmless environment variables: Python,
    # shell startup hooks and proxy variables can carry code or credentials.
    spawning = asyncio.create_task(
        asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            command,
            cwd=workspace,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": str(workspace),
                "TMPDIR": str(workspace),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            },
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            umask=0o007,
            preexec_fn=limits,
        )
    )
    cancelled = False
    try:
        process = await asyncio.shield(spawning)
    except asyncio.CancelledError:
        # Cancellation can arrive during fork/exec; retain the handle and reap
        # the child before reporting that the command has stopped.
        process = await spawning
        cancelled = True
    streams = [asyncio.create_task(capture(process.stdout)), asyncio.create_task(capture(process.stderr))]
    timed_out = False
    try:
        if not cancelled:
            await asyncio.wait_for(process.wait(), timeout)
    except TimeoutError:
        timed_out = True
    except asyncio.CancelledError:
        cancelled = True
    finally:
        # Shield cleanup from a second cancellation (e.g. client disconnect
        # during service shutdown). A receipt is sent only after cleanup.
        cleanup = asyncio.create_task(terminate_group(process))
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
    try:
        captured = await asyncio.wait_for(asyncio.gather(*streams), 2)
    except TimeoutError:
        # A process that deliberately escaped its group is outside this local
        # execution contract; never claim successful cleanup in that case.
        return {"error": "COMMAND_CLEANUP_UNCONFIRMED"}
    return {
        "exit_code": process.returncode,
        "stdout": captured[0][0],
        "stderr": captured[1][0],
        "stdout_truncated": captured[0][1],
        "stderr_truncated": captured[1][1],
        "timed_out": timed_out,
        "cancelled": cancelled,
    }


class ExecServer:
    def __init__(self, root, allowed_uid, *, test_mode=False):
        self.root = Path(root).resolve(strict=True)
        self.allowed_uid, self.test_mode = allowed_uid, test_mode
        self.slots = asyncio.Semaphore(4)
        self.connections = set()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        execution = disconnected = None
        acquired = False
        try:
            uid = peer_uid(writer)
            if uid != self.allowed_uid and not (uid is None and self.test_mode):
                raise ValueError("UNAUTHORIZED_WORKER")
            payload = json.loads(await asyncio.wait_for(reader.readline(), 5))
            if payload == {"op": "ping", "protocol": PROTOCOL}:
                result = {
                    "protocol": PROTOCOL,
                    "implementation": IMPLEMENTATION,
                    "ready": True,
                    "uid": os.getuid(),
                }
            else:
                if not isinstance(payload, dict) or set(payload) != {
                    "op",
                    "protocol",
                    "scope",
                    "command",
                    "timeout",
                    "implementation",
                }:
                    raise ValueError("INVALID_REQUEST")
                if payload["op"] != "exec" or payload["protocol"] != PROTOCOL:
                    raise ValueError("INVALID_PROTOCOL")
                if payload["implementation"] != IMPLEMENTATION:
                    raise ValueError("EXEC_VERSION_MISMATCH")
                scope, command, timeout = payload["scope"], payload["command"], payload["timeout"]
                if not isinstance(scope, str) or not re.fullmatch(r"[0-9a-f]{32}", scope):
                    raise ValueError("INVALID_WORKSPACE")
                workspace = self.root / scope
                if workspace.is_symlink() or not workspace.is_dir():
                    raise ValueError("INVALID_WORKSPACE")
                if (
                    not isinstance(command, str)
                    or not command.strip()
                    or len(command.encode()) > 8192
                    or "\0" in command
                ):
                    raise ValueError("INVALID_COMMAND")
                if type(timeout) not in (int, float) or not 0 < timeout <= MAX_TIMEOUT:
                    raise ValueError("INVALID_TIMEOUT")
                await asyncio.wait_for(self.slots.acquire(), 1)
                acquired = True
                execution = asyncio.create_task(run_command(workspace, command, timeout))
                disconnected = asyncio.create_task(reader.readline())
                done, _ = await asyncio.wait([execution, disconnected], return_when=asyncio.FIRST_COMPLETED)
                if disconnected in done and not execution.done():
                    execution.cancel()  # EOF or any second line means stop.
                result = await execution
            writer.write(json.dumps(result, ensure_ascii=True).encode() + b"\n")
            await writer.drain()
        except (ValueError, TimeoutError, OSError) as exc:
            code = str(exc) if isinstance(exc, ValueError) else "EXEC_SERVICE_UNAVAILABLE"
            try:
                writer.write(json.dumps({"error": code[:100]}).encode() + b"\n")
                await writer.drain()
            except (OSError, ConnectionError):
                pass
        finally:
            if disconnected:
                disconnected.cancel()
            if execution and not execution.done():
                execution.cancel()
                await execution
            if acquired:
                self.slots.release()
            writer.close()
            self.connections.discard(task)


async def serve(args):
    if os.getuid() == 0 and not args.test_allow_current_user:
        raise RuntimeError("The execution service must not run as root")
    if not args.test_allow_current_user and (
        os.getuid() == args.worker_uid or not hasattr(socket, "SO_PEERCRED")
    ):
        raise RuntimeError("Production execution requires Linux and a separate execution account")
    path = Path(args.socket)
    if path.exists():
        if not stat.S_ISSOCK(path.lstat().st_mode) or path.stat().st_uid != os.getuid():
            raise RuntimeError("Refusing to replace an unowned socket path")
        path.unlink()
    runner = ExecServer(args.workspaces, args.worker_uid, test_mode=args.test_allow_current_user)
    server = await asyncio.start_unix_server(runner.handle, path=path, limit=32768)
    path.chmod(0o660)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    try:
        async with server:
            await stopped.wait()
    finally:
        for task in list(runner.connections):
            task.cancel()
        await asyncio.gather(*list(runner.connections), return_exceptions=True)
        path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--workspaces", required=True)
    parser.add_argument("--worker-uid", required=True, type=int)
    parser.add_argument("--test-allow-current-user", action="store_true", help="Local automated tests only")
    asyncio.run(serve(parser.parse_args()))


if __name__ == "__main__":
    main()
