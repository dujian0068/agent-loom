"""Close activation resources without replacing an established task outcome."""

import logging

from ..resources import close_resource
from .commands import RuntimeCommit

logger = logging.getLogger(__name__)


async def close_activation(runner, task_id, activation_id, stream, *, primary=None):
    def report(_diagnostic):
        # A node's exception text can contain private model/provider material.
        # Keep a single bounded code on the activation, not in the TaskResult.
        try:
            task = runner.task(task_id)
            runner.coordinator.commit(
                RuntimeCommit(
                    "node-cleanup:" + activation_id,
                    "RecordActivationCleanup",
                    runner.run_id,
                    task_id,
                    activation_id,
                    task["node_revision"],
                    {"code": "NODE_GENERATOR_CLOSE_FAILED"},
                ),
                runner.lease,
            )
        except Exception:
            # Diagnostic persistence is best effort; neither a lost lease nor a
            # storage failure may replace the primary failure or committed receipt.
            logger.warning("Node cleanup diagnostic unavailable: NODE_GENERATOR_CLOSE_FAILED")

    await close_resource(stream.aclose, report, primary=primary)
