"""Public results contain accepted output, never attempt audit material."""

from ..types import FinalOutput, RunResult, RunUsage


def result_from(state):
    """Project a committed Run without publishing private model fragments.

    partial_results in the execution ledger is an internal audit collection,
    including failed retries, compaction and Executor reasoning. None of those
    records is an authorized PayloadRef. Until a producer explicitly commits
    such public references, the public partial-results collection is empty.
    This rule also applies to old checkpoints and successful Runs.
    """
    return RunResult(
        run_id=state["run_id"],
        session_id=state["session_id"],
        status=state["status"],
        output=FinalOutput(**state["output"]) if state["output"] else None,
        error=state["error"],
        partial_results=(),
        usage=RunUsage(**state["usage"]),
        started_at=state["started_at"],
        ended_at=state["ended_at"],
        cleanup_state=state["cleanup_state"],
    )
