"""Built-in fallback for harnesses that fail to load MCP tools."""
import argparse
import json
import sys
from . import mcp_server

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tool", choices=list(mcp_server.HANDLERS))
    for name in ("profile", "url", "goal", "serial", "vendor"):
        parser.add_argument("--" + name)
    parser.add_argument("--port", type=int)
    parser.add_argument("--act", action="store_true", default=None)
    parser.add_argument("--json-file", help="Advanced arguments as JSON in a file; avoids shell quoting.")
    options = vars(parser.parse_args())
    tool = options.pop("tool")
    json_file = options.pop("json_file")
    arguments = {}
    if json_file:
        with open(json_file, encoding="utf-8") as source:
            arguments = json.load(source)
    arguments.update({k: v for k, v in options.items() if v is not None})
    schema = next(t["inputSchema"] for t in mcp_server.TOOLS if t["name"] == tool)
    missing = [name for name in schema.get("required", []) if name not in arguments]
    if missing:
        parser.error("missing arguments: " + ", ".join(missing))
    mcp_server.load_env()
    response = mcp_server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    })
    result = response["result"]
    for block in result["content"]:
        print(block.get("text", ""))
    return 1 if result.get("isError") else 0

if __name__ == "__main__":
    sys.exit(main())
