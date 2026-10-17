import asyncio
import math

from ..errors import RequestError, WaitTimeout
from ..storage import TERMINAL
from ..types import FinalOutput, RunEvent, RunResult, RunUsage


def result_from(state):
    return RunResult(
        state["run_id"],
        state["session_id"],
        state["status"],
        FinalOutput(**state["output"]) if state["output"] else None,
        state["error"],
        tuple(state["partial_results"]),
        RunUsage(**state["usage"]),
        state["started_at"],
        state["ended_at"],
        state["cleanup_state"],
    )


class RunHandle:
    def __init__(self, runtime, run_id, session_id, input_id):
        self._runtime = runtime
        self.run_id, self.session_id, self.input_id = run_id, session_id, input_id

    async def wait(self, *, timeout_s=None):
        if timeout_s is not None and (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not math.isfinite(timeout_s)
            or timeout_s <= 0
        ):
            raise RequestError("WAIT_TIMEOUT_INVALID")

        async def observe():
            while True:
                self._runtime.ensure_open()
                state = self._runtime._load(self.run_id)
                if state["status"] in TERMINAL:
                    return result_from(state)
                self._runtime.raise_local_failure(self.run_id)
                await asyncio.sleep(0.01)

        try:
            async with asyncio.timeout(timeout_s):
                return await observe()
        except TimeoutError as exc:
            raise WaitTimeout("This wait expired; the run continues") from exc

    async def events(self, *, after_seq=0):
        if type(after_seq) is not int or after_seq < 0:
            raise RequestError("EVENT_CURSOR_INVALID")
        self._runtime.ensure_open()
        if after_seq > len(self._runtime._load(self.run_id)["events"]):
            raise RequestError("EVENT_CURSOR_AHEAD")
        while True:
            self._runtime.ensure_open()
            state = self._runtime._load(self.run_id)
            for event in state["events"][after_seq:]:
                yield RunEvent(**event)
                after_seq = event["seq"]
            if state["terminal_seq"] is not None and after_seq >= state["terminal_seq"]:
                return
            self._runtime.raise_local_failure(self.run_id)
            await asyncio.sleep(0.01)

    async def snapshot(self):
        return await self._runtime.runs.get(self.run_id)

    async def stop(self, *, command_id, reason="user"):
        return await self._runtime.runs.stop(self.run_id, command_id=command_id, reason=reason)
