"""Optional official MCP client transport; registrations remain explicit and local."""

from contextlib import asynccontextmanager

from ..configuration import digest, json_copy
from ..errors import AgentLoomError, ConfigurationError
from ..resources import managed_resource
from .contracts import ValueToolAdapter
from .invocation import report_completion


class McpToolAdapter(ValueToolAdapter):
    """One session per attempt prevents shared server state leaking across runs.

    The SDK does not auto-discover or authorize tools during Agent construction.
    Use discover() explicitly, then register the selected immutable schemas.
    """

    def __init__(self, url=None, *, command=None, args=(), env=None, headers=None):
        if (url is None) == (command is None):
            raise ConfigurationError("MCP_TRANSPORT_REQUIRED")
        if url is not None and not url.startswith(("https://", "http://")):
            raise ConfigurationError("MCP_URL_INVALID")
        self.url, self.command = url, command
        self.args, self.env, self.headers = tuple(args), dict(env or {}), dict(headers or {})

    @asynccontextmanager
    async def _session(self):
        try:
            import httpx
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as exc:
            raise ConfigurationError("MCP_EXTRA_REQUIRED", "Install agentloom-sdk[mcp]") from exc
        if self.url:
            async with httpx.AsyncClient(
                headers=self.headers, timeout=None, follow_redirects=False
            ) as client:
                async with streamable_http_client(self.url, http_client=client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session
        else:
            import tempfile

            # Transport stderr is not mixed into public events or terminal output.
            with tempfile.TemporaryFile(mode="w+") as errlog:
                async with stdio_client(
                    StdioServerParameters(command=self.command, args=list(self.args), env=self.env),
                    errlog=errlog,
                ) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session

    async def discover(self):
        """Explicit I/O, outside Agent assembly; does not grant the discovered tools."""
        result, cursor, seen = [], None, set()
        async with self._session() as session:
            while True:
                page = await session.list_tools(cursor=cursor)
                result.extend(
                    tool.model_dump(mode="json", exclude_none=True) for tool in page.tools
                )
                if not page.nextCursor:
                    return result
                if page.nextCursor in seen:
                    raise AgentLoomError(
                        "MCP_CATALOG_CURSOR_INVALID", external_outcome="not_started"
                    )
                seen.add(page.nextCursor)
                cursor = page.nextCursor

    async def invoke(self, member, arguments, context):
        context.check_active()
        try:
            async with managed_resource(
                self._session(), getattr(context, "report_diagnostic", None)
            ) as session:
                spec = context.spec
                if spec is None:
                    raise AgentLoomError("MCP_CATALOG_REQUIRED", external_outcome="not_started")
                cursor, actual, seen = None, None, set()
                while True:
                    page = await session.list_tools(cursor=cursor)
                    for tool in page.tools:
                        if tool.name == spec.name:
                            if actual is not None:
                                raise AgentLoomError(
                                    "MCP_CATALOG_MISMATCH",
                                    category="conflict",
                                    external_outcome="not_started",
                                )
                            actual = tool
                    if not page.nextCursor:
                        break
                    if page.nextCursor in seen:
                        raise AgentLoomError(
                            "MCP_CATALOG_CURSOR_INVALID", external_outcome="not_started"
                        )
                    seen.add(page.nextCursor)
                    cursor = page.nextCursor
                if actual is None or digest([actual.inputSchema, actual.outputSchema]) != digest(
                    [json_copy(spec.input_schema), json_copy(spec.output_schema)]
                ):
                    raise AgentLoomError(
                        "MCP_CATALOG_MISMATCH", category="conflict", external_outcome="not_started"
                    )
                context.check_active()
                result = await session.call_tool(
                    spec.name, arguments, meta={"agentloom/operation_id": context.operation_id}
                )
                try:
                    value = json_copy(result.model_dump(mode="json", exclude_none=True))
                except (ConfigurationError, ValueError, TypeError) as exc:
                    error = AgentLoomError(
                        "MCP_PROTOCOL_ERROR", category="protocol", external_outcome="known"
                    )
                    error.execution_fact = "completed"
                    error.__cause__ = exc
                    report_completion(context, error=error)
                    raise error from exc
                if result.isError:
                    error = AgentLoomError(
                        "MCP_TOOL_ERROR", category="business", external_outcome="known"
                    )
                    error.result_material = value
                    report_completion(context, error=error)
                    raise error
                value = value.get("structuredContent", {"content": value.get("content", [])})
                report_completion(context, value=value)
                return value
        except Exception as exc:
            mapped = _transport_error(exc)
            if mapped is exc:
                raise
            if _contains_exception(exc, mapped):
                # A chosen leaf must not acquire its containing group as a cause.
                raise mapped from mapped.__cause__
            raise mapped from exc


def _contains_exception(group, target):
    return (
        group is target
        or isinstance(group, BaseExceptionGroup)
        and any(_contains_exception(child, target) for child in group.exceptions)
    )


def _transport_error(exc):
    # AnyIO transport task groups wrap errors on context exit. Classification
    # uses exception types/status codes, never matching server error text.
    if isinstance(exc, BaseExceptionGroup):
        leaves = [_transport_error(child) for child in exc.exceptions]
        return next((leaf for leaf in leaves if not leaf.retryable_transport), leaves[0])
    if isinstance(exc, AgentLoomError):
        return exc
    import httpx

    from ..models.http import http_error

    if isinstance(exc, httpx.HTTPStatusError):
        return http_error(exc.response.status_code, exc.response.headers)
    if isinstance(exc, (httpx.TransportError, ConnectionError)):
        return AgentLoomError(
            "MCP_CONNECTION_FAILED",
            category="transport",
            retryable_transport=True,
            external_outcome="unknown",
        )
    return AgentLoomError(
        "MCP_PROTOCOL_ERROR", type(exc).__name__, category="protocol", external_outcome="unknown"
    )
