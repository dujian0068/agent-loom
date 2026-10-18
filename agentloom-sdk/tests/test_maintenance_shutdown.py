"""Host shutdown must drain both maintenance threads before returning."""

import asyncio
import threading

import pytest

from agentloom import RuntimeEngine
from agentloom.runtime import host
from agentloom.runtime.cleanup import CleanupService


@pytest.mark.parametrize("phase", ["control", "cleanup"])
async def test_shutdown_waits_for_each_maintenance_worker(runtime_store, monkeypatch, phase):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    observed = []

    def blocked(runtime):
        entered.set()
        try:
            if not release.wait(5):
                raise TimeoutError("Test did not release maintenance")
            observed.append(runtime._status)
        finally:
            finished.set()

    monkeypatch.setattr(
        host, "request_expired_stops", blocked if phase == "control" else lambda r: None
    )
    monkeypatch.setattr(host, "settle_orphaned_stops", lambda r: None)
    monkeypatch.setattr(
        CleanupService,
        "scan_once",
        lambda self: blocked(self.runtime) if phase == "cleanup" else None,
    )
    runtime = RuntimeEngine(store=runtime_store)
    await runtime.__aenter__()
    closing = None
    try:
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        closing = asyncio.create_task(runtime.aclose())
        # The idle sibling can exit; it must not interrupt this worker's drain.
        await asyncio.sleep(0.05)
        assert not closing.done(), "Host returned while a maintenance thread was still running"
        assert runtime._status == "closing"
    finally:
        release.set()
        await asyncio.wait_for(closing if closing is not None else runtime.aclose(), 3)
        async with asyncio.timeout(3):
            while not finished.is_set():
                await asyncio.sleep(0.005)
    assert observed == ["closing"]
    assert runtime._status == "closed"
