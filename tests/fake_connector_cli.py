"""MCP discovery fixture. No inference or network."""
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

kind, *args = sys.argv[1:]
if args == ["mcp", "list", "--json"]:
    print(json.dumps([
        {"name": "docs-work", "enabled": True, "transport": {
            "url": "https://developers.openai.com/mcp", "http_headers": {"Authorization": "FIXTURE TOKEN"},
        }},
        {"name": "linear", "enabled": True, "transport": {"url": "https://mcp.linear.app/mcp/readonly"}},
    ]))
elif args == ["mcp", "list"]:
    print("claude.ai Gmail: https://proxy.invalid/mcp?secret=FIXTURE - ✔ Connected")
    print("linear-work: https://mcp.linear.app/mcp/readonly - ! Needs authentication")
    print("private: command FIXTURE TOKEN - ✘ Failed to connect — FIXTURE TOKEN")
elif args == ["app-server"]:
    for line in sys.stdin:
        request = json.loads(line)
        outputs = {
            "initialize": {},
            "app/installed": {"apps": [{"runtimeName": "Google Drive", "callable": True}]},
            "mcpServerStatus/list": {"data": [
                {"name": "docs-work", "authStatus": "unsupported", "serverCapabilities": {},
                 "tools": {"search": {"description": "FIXTURE TOKEN"}}},
                {"name": "linear", "authStatus": "notLoggedIn", "toolsError": "FIXTURE TOKEN", "tools": {}},
            ]},
        }
        if "id" in request:
            print(json.dumps({"id": request["id"], "result": outputs[request["method"]]}), flush=True)
else:
    sys.exit(2)
