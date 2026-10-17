"""Pure action admission checks, before Operation/Attempt or child creation."""

from ..configuration.actions import action_contract, validate_action_contract


def validate_action(runner, role, name, arguments):
    validate_action_contract(
        action_contract(runner.program, role, runner.tools(role)), name, arguments
    )
