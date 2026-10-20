"""Loopback-only MCP fixture, started as a separate process by integration tests."""

import sys

from mcp.server.fastmcp import FastMCP

server = FastMCP("Platform test service", host="127.0.0.1", port=int(sys.argv[1]))


@server.tool()
def add(left: int, right: int) -> int:
    """Add two integers for a connection test."""
    return left + right


@server.tool()
def echo(text: str) -> str:
    """Return test text without external effects."""
    return text


if __name__ == "__main__":
    server.run(transport="streamable-http")
