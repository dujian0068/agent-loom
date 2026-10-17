"""A real JSON-RPC stdio peer for testing the optional official MCP client."""

import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    with open(sys.argv[1], "a", encoding="utf-8") as audit:
        audit.write(json.dumps(request) + "\n")
        audit.flush()
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {
            "protocolVersion": request["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fixture", "version": "1"},
        }
    elif method == "tools/list":
        result = {
            "tools": [{"name": "echo", "description": "Echo", "inputSchema": {"type": "object"}}]
        }
    elif method == "tools/call":
        args = request["params"]["arguments"]
        result = {
            "content": [
                {
                    "type": "text",
                    "text": "transport reset is just business text" if args.get("fail") else "echo",
                }
            ],
            "isError": bool(args.get("fail")),
        }
        if not args.get("fail"):
            result["structuredContent"] = {"echo": args}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
