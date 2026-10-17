import asyncio
import math

from ..errors import RequestError, WaitTimeout
from ..storage import TERMINAL
from ..types import RunEvent
from .projections import result_from


class RunHandle:
    """A runtime-bound view of one durable Run; observing it does not own execution."""

    def __init__(self, runtime, run_id, session_id, input_id):
        self._runtime = runtime
        self.run_id, self.session_id, self.input_id = run_id, session_id, input_id

    async def wait(self, *, timeout_s=None):
        """Observe business completion and return its RunResult.

        Args:
            timeout_s: Finite positive seconds for this wait, or None to wait
                indefinitely. It does not change the Run's execution deadline.

        Returns:
            RunResult for any business terminal status, including failure, stop
            and timeout. Cleanup may still be pending; repeated waits are safe.

        Raises:
            WaitTimeout: This observation exceeded timeout_s; the Run continues.
            RequestError: Invalid timeout or a Run whose record is unavailable.
            InfrastructureError: Bound runtime closed/on another event loop,
                storage failure, or a local execution-driver failure.

        Cancelling this wait does not cancel the Run. Use stop() to request a
        durable stop. After closing this runtime, attach the Run to another open
        runtime sharing its storage instead of reusing this handle.
        """
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
        """Yield committed RunEvents after the exclusive nonnegative after_seq cursor.

        Stops after the terminal event, independently of cleanup. Invalid/ahead
        cursors raise RequestError; runtime, storage and local driver failures
        raise InfrastructureError. Cancelling observation leaves the Run active.
        """
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
        """Return the latest public Run projection, including cleanup/maintenance errors.

        Requires the bound runtime to be open. Missing records raise RequestError;
        runtime or storage failures raise InfrastructureError. This does not wait.
        """
        return await self._runtime.runs.get(self.run_id)

    async def stop(self, *, command_id, reason="user"):
        """Persist a stop request and return its receipt without waiting for termination.

        command_id is an idempotency key: reuse only for the same Run and reason
        text. A conflicting/invalid command raises RequestError; runtime/storage
        failure raises InfrastructureError. Then use wait() to observe completion.
        """
        return await self._runtime.runs.stop(self.run_id, command_id=command_id, reason=reason)
