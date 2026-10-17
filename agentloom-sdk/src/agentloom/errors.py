"""Stable SDK errors. Error text is never used to decide retry eligibility."""


class AgentLoomError(Exception):
    def __init__(
        self,
        code,
        message=None,
        *,
        category="business",
        phase="execute",
        external_outcome="known",
        retryable_transport=False,
        field_path=None,
    ):
        self.code = code
        self.message = message or code
        self.category = category
        self.phase = phase
        self.external_outcome = external_outcome
        self.retryable_transport = retryable_transport
        self.field_path = field_path
        super().__init__(f"{code}: {self.message}")

    def to_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "category": self.category,
            "phase": self.phase,
            "external_outcome": self.external_outcome,
            "retryable_transport": self.retryable_transport,
            "field_path": self.field_path,
        }


class ConfigurationError(AgentLoomError):
    pass


class RequestError(AgentLoomError):
    pass


class InfrastructureError(AgentLoomError):
    pass


class LeaseLost(InfrastructureError):
    def __init__(self, message="Execution ownership has expired or was revoked"):
        super().__init__("LEASE_LOST", message)


class WaitTimeout(TimeoutError):
    pass


def error_record(exc):
    if isinstance(exc, AgentLoomError):
        return exc.to_dict()
    if isinstance(exc, FileNotFoundError):
        return AgentLoomError("FILE_NOT_FOUND", category="not_found").to_dict()
    if isinstance(exc, FileExistsError):
        return AgentLoomError("FILE_EXISTS", category="conflict").to_dict()
    if isinstance(exc, PermissionError):
        return AgentLoomError("FILE_ACCESS_DENIED", category="permission").to_dict()
    # Arbitrary provider exceptions can contain credentials and host paths.
    return AgentLoomError(
        "ADAPTER_ERROR", type(exc).__name__, category="unknown", external_outcome="unknown"
    ).to_dict()
