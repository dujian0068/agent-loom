"""Same-host execution: OS locks survive coordinator failure, one subprocess per Run."""

import argparse
import asyncio
import fcntl
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext

from sqlalchemy import select

from .applications import TERMINAL, append_event
from .config import Settings
from .persistence import Application, ChatSession, Database, Operation, Release, ReleaseJob, Run


@contextmanager
def run_lock(settings, run_id):
    directory = settings.data_dir / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{run_id}.lock").open("a") as handle:
        acquired = False
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        try:
            yield acquired
        finally:
            if acquired:
                fcntl.flock(handle, fcntl.LOCK_UN)


def finish(db, run_id, generation, status, result=None, error=None):
    with db.session(write=True) as s:
        run = s.get(Run, run_id)
        if run.generation != generation or run.status in TERMINAL:
            return
        pending = s.scalar(
            select(Operation.call_id).where(Operation.run_id == run_id, Operation.status == "pending")
        )
        if pending:
            # Local cancellation cannot prove the outcome of a remote action.
            status, result, error = "failed", None, "工具操作结果未确认，需要人工核对；不会自动重放"
        elif run.status == "stopping":
            status, result = "cancelled", None
        run.status, run.result, run.error, run.updated_at = status, result, error, time.time()
        append_event(s, run_id, "status", {"status": status, "error": error})


async def run_one(settings, db, run_id, *, lock_held=False):
    with (nullcontext(True) if lock_held else run_lock(settings, run_id)) as acquired:
        if not acquired:
            return
        with db.session(write=True) as s:
            run = s.get(Run, run_id)
            if not run or run.status in TERMINAL:
                return
            if run.host_id and run.host_id != settings.worker_host_id:
                return
            release = s.get(Release, run.release_id)
            app = s.get(Application, release.app_id)
            if run.status == "stopping" or app.lifecycle != "active":
                run.status = "cancelled"
                append_event(s, run.id, "status", {"status": "cancelled"})
                return
            run.status, run.generation = "running", run.generation + 1
            run.host_id = settings.worker_host_id
            append_event(s, run.id, "status", {"status": "running"})
            snapshot = release.snapshot
        from .runtime import RuntimeBlocked, execute

        async def settle(status, result=None, error=None):
            # Each Run owns its process/event loop. A cancelled asyncio future can
            # leave a synchronous tool's thread running, so drain it before making
            # this session eligible for another Run or workspace cleanup.
            await asyncio.get_running_loop().shutdown_default_executor()
            finish(db, run.id, run.generation, status, result, error)

        task = asyncio.create_task(execute(settings, db, run, snapshot))
        try:
            while not task.done():
                await asyncio.sleep(settings.poll_seconds)
                with db.session() as s:
                    current = s.get(Run, run_id)
                    should_stop = current.status == "stopping"
                    timed_out = time.time() >= current.deadline_at
                if should_stop or timed_out:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        print(f"Run {run_id}: local execution cancelled", file=sys.stderr)
                    except Exception as exc:  # noqa: BLE001 -- cancellation can reveal a pending transport error
                        print(f"Run {run_id}: cancellation ({type(exc).__name__})", file=sys.stderr)
                    await settle(
                        "cancelled" if should_stop else "failed",
                        error=None if should_stop else "运行超过期限",
                    )
                    return
            result = await task
            await settle("completed", result)
        except RuntimeBlocked as exc:
            await settle("failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 -- execution boundary converts third-party failures to diagnostics
            # Never leak provider request bodies or credentials into public diagnostics.
            print(f"Run {run_id} failed: {type(exc).__name__}", file=sys.stderr)
            await settle("failed", error="模型或工具调用失败，请检查连接与运行日志")


def maintenance(settings, db):
    with db.session() as s:
        runs = list(
            s.scalars(
                select(Run).where(
                    (Run.status == "stopping")
                    | ((Run.status.in_(TERMINAL)) & (Run.cleanup_state != "completed"))
                )
            )
        )
    for candidate in runs:
        if candidate.host_id and candidate.host_id != settings.worker_host_id:
            continue
        with run_lock(settings, candidate.id) as acquired:
            if not acquired:
                continue
            if candidate.status == "stopping":
                finish(db, candidate.id, candidate.generation, "cancelled")
            with db.session() as s:
                run = s.get(Run, candidate.id)
                release = s.get(Release, run.release_id)
                retention = release.snapshot["draft"]["environment"]["retention"]
            if run.status not in TERMINAL:
                continue
            try:
                if retention == "run":
                    workspace = settings.data_dir / "workspaces" / run.id
                    if workspace.exists():
                        shutil.rmtree(workspace)
                with db.session(write=True) as s:
                    s.get(Run, run.id).cleanup_state = "completed"
            except OSError:
                with db.session(write=True) as s:
                    s.get(Run, run.id).cleanup_state = "failed"
    with db.session(write=True) as s:
        for job in s.scalars(select(ReleaseJob).where(ReleaseJob.status != "succeeded")):
            active = s.scalar(
                select(Run.id)
                .join(ChatSession, ChatSession.id == Run.session_id)
                .where(
                    ChatSession.app_id == job.app_id,
                    (Run.status.not_in(TERMINAL)) | (Run.cleanup_state != "completed"),
                )
            )
            if active:
                job.status = "waiting_cleanup"
                continue
            unresolved = s.scalar(
                select(Operation.call_id)
                .join(Run, Run.id == Operation.run_id)
                .join(ChatSession, ChatSession.id == Run.session_id)
                .where(ChatSession.app_id == job.app_id, Operation.status == "pending")
            )
            if unresolved:
                job.status = "needs_attention"
                continue
            app = s.get(Application, job.app_id)
            app.release_id, app.lifecycle, job.status = job.release_id, "active", "succeeded"


def serve(settings, db, control_only=False, once=False):
    children = {}
    try:
        while True:
            maintenance(settings, db)
            children = {key: process for key, process in children.items() if process.poll() is None}
            if not control_only:
                with db.session() as s:
                    candidates = list(
                        s.scalars(
                            select(Run).where(Run.status.in_(["queued", "running"])).order_by(Run.created_at)
                        )
                    )
                for run in candidates:
                    if len(children) >= 4:
                        break
                    if run.id in children or (run.host_id and run.host_id != settings.worker_host_id):
                        continue
                    with run_lock(settings, run.id) as free:
                        if not free:
                            continue
                    children[run.id] = subprocess.Popen(
                        [sys.executable, "-m", "agentloom_platform.worker", "--run", run.id],
                        env=os.environ.copy(),
                    )
            if once:
                for child in children.values():
                    child.wait(timeout=1900)
                maintenance(settings, db)
                return
            time.sleep(settings.poll_seconds)
    except KeyboardInterrupt:
        # Child processes keep their locks until they exit; a new coordinator must not steal them.
        return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run")
    parser.add_argument("--control-only", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    settings.prepare()
    db = Database(settings.database_url)
    if args.run:
        # Keep ownership through asyncio.run's default-executor shutdown.
        # Cancelling a tool await does not terminate an in-flight file I/O thread.
        with run_lock(settings, args.run) as acquired:
            if acquired:
                asyncio.run(run_one(settings, db, args.run, lock_held=True))
    else:
        serve(settings, db, args.control_only, args.once)


if __name__ == "__main__":
    main()
