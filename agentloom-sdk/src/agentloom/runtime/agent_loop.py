"""ReAct node program. It prepares outcomes; only NodeDriver commits them."""

from ..configuration.json_documents import canonical_document
from ..errors import ConfigurationError, InfrastructureError, RequestError
from ..nodes import (
    CommitReceipt,
    Complete,
    ContinuationRef,
    Fail,
    PayloadRef,
    Progress,
    ReactContinuationV1,
    Wait,
    WaitCondition,
)
from ..nodes.contracts import validate_binding
from .output import validate_output


class AgentLoop:
    def __init__(self, agent, node_context):
        self.agent, self.context = agent, node_context

    async def activate(self, input, continuation):
        context = self.context
        validate_binding(input, continuation, context)
        task = context.context_port.snapshot()
        if (
            continuation is None
            or continuation.content_hash
            != PayloadRef(context.task_id, task["continuation"]).content_hash
        ):
            raise InfrastructureError("CONTINUATION_INVALID")
        if input.input_ref.content_hash != PayloadRef(context.task_id, task["input"]).content_hash:
            raise RequestError("NODE_INPUT_MISMATCH")
        while True:
            task = context.context_port.snapshot()
            current = ReactContinuationV1(**task["continuation"])
            if current.role_key != context.scope.role_key:
                raise InfrastructureError("CONTINUATION_INVALID")
            revision = task["node_revision"]

            def ref(value):
                return ContinuationRef(context.task_id, value.to_dict(), revision)

            phase = current.phase
            if phase == "prepare_context":
                effect = await context.context_port.prepare("prepare_context")
                outcome = Progress(ref(current.advance(phase="prepare_model")), (effect,))
            elif phase == "prepare_model":
                op_id, effect = await context.model_port.prepare(
                    current.role_loop_index, context.context_port
                )
                outcome = Progress(
                    ref(current.advance(phase="compact_context", model_operation_id=op_id)),
                    (effect,),
                )
            elif phase in {"compact_context", "call_model"}:
                response = context.model_port.result(current.model_operation_id)
                if response is None:
                    yield Wait(
                        ref(current),
                        WaitCondition("operation_completed", current.model_operation_id),
                        PayloadRef(context.task_id, {"operation_id": current.model_operation_id}),
                    )
                    return
                outcome = Progress(ref(current.advance(phase="interpret_response")))
            elif phase == "interpret_response":
                response = context.model_port.result(current.model_operation_id)
                if response is None:
                    raise InfrastructureError("OPERATION_RESULT_MISSING")
                if response["status"] != "succeeded":
                    yield Fail(response["error"])
                    return
                has_calls = bool(response["result"]["tool_calls"])
                batch_id = (
                    context.capability_port.batch_id(current.role_loop_index) if has_calls else None
                )
                effect = await context.context_port.prepare(
                    "accept_model", operation_id=current.model_operation_id
                )
                outcome = Progress(
                    ref(
                        current.advance(
                            phase="wait_batch" if has_calls else "validate_output",
                            pending_batch_id=batch_id,
                            accepted_decision_ref=current.model_operation_id,
                        )
                    ),
                    (effect,),
                )
            elif phase == "wait_batch":
                if not context.capability_port.settled(current.pending_batch_id):
                    yield Wait(
                        ref(current),
                        WaitCondition("batch_settled", current.pending_batch_id),
                        PayloadRef(context.task_id, {"batch_id": current.pending_batch_id}),
                    )
                    return
                outcome = Progress(ref(current.advance(phase="consume_results")))
            elif phase == "consume_results":
                effect = await context.context_port.prepare(
                    "consume_batch", batch_id=current.pending_batch_id
                )
                outcome = Progress(
                    ref(
                        current.advance(
                            phase="prepare_context",
                            role_loop_index=current.role_loop_index + 1,
                            model_operation_id=None,
                            current_loop_ticket_id=None,
                            pending_batch_id=None,
                            pending_operation_refs=(),
                            compaction_ref=None,
                            projection_ref=None,
                            accepted_decision_ref=None,
                            validation_feedback_ref=None,
                        )
                    ),
                    (effect,),
                )
            elif phase == "validate_output":
                try:
                    value = validate_output(task["candidate"], self.agent.definition.output_schema)
                except RequestError:
                    effect = await context.context_port.prepare(
                        "format_feedback",
                        message=(
                            "The final answer did not satisfy the required output format. Return a corrected complete answer. Required schema: "
                            + canonical_document(self.agent.definition.output_schema)
                        ),
                    )
                    outcome = Progress(
                        ref(
                            current.advance(
                                phase="prepare_context",
                                role_loop_index=current.role_loop_index + 1,
                                model_operation_id=None,
                                current_loop_ticket_id=None,
                                accepted_decision_ref=None,
                            )
                        ),
                        (effect,),
                    )
                except ConfigurationError as exc:
                    yield Fail(exc.to_dict())
                    return
                else:
                    yield Complete(
                        PayloadRef(context.task_id, value), ref(current.advance(phase="completed"))
                    )
                    return
            else:
                raise InfrastructureError("CONTINUATION_INVALID")
            receipt = yield outcome
            if (
                not isinstance(receipt, CommitReceipt)
                or receipt.allocated_refs.get("task_id") != context.task_id
                or not any(v.get("node_revision") == revision + 1 for v in receipt.entity_versions)
            ):
                raise InfrastructureError("NODE_COMMIT_RECEIPT_INVALID")
