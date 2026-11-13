"""Deterministic Agent Client Protocol (ACP) stdio fixture; never contacts a model."""
import json
from pathlib import Path
import subprocess
import sys
import time

args = sys.argv[1:]
if "--version" in args or "--help" in args:
    print("fake-acp 1.0")
    raise SystemExit(0)


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


active_session = "acp-session-1"
model_id = None

while True:
    line = sys.stdin.readline()
    if not line:
        break
    if not line.strip():
        continue
    frame = json.loads(line)
    req_id = frame.get("id")
    method = frame.get("method")
    params = frame.get("params") or {}

    if method == "initialize":
        send({"jsonrpc": "2.0", "id": req_id, "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}})
    elif method == "session/new":
        active_session = "acp-fresh-session"
        send({"jsonrpc": "2.0", "id": req_id, "result": {"sessionId": active_session}})
    elif method == "session/load":
        active_session = params.get("sessionId") or "acp-loaded-session"
        send({"jsonrpc": "2.0", "id": req_id, "result": {"sessionId": active_session}})
    elif method == "session/set_model":
        model_id = params.get("modelId")
        send({"jsonrpc": "2.0", "id": req_id, "result": {}})
    elif method == "session/prompt":
        blocks = params.get("prompt") or []
        text = "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        task = text.split("TASK:\n", 1)[-1]

        if "TIMEOUT" in task:
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            Path("acp_child.pid").write_text(str(child.pid), encoding="utf-8")
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": active_session,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "partial-acp-chunk"},
                        },
                    },
                }
            )
            time.sleep(60)

        if "ACP_ERROR" in task:
            send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": "fixture acp error"}})
            continue

        for token, perm_kind in (("ALLOW_READ", "read"), ("REQ_EDIT", "edit"), ("REQ_EXEC", "execute")):
            if token in task:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": "perm-1",
                        "method": "session/request_permission",
                        "params": {
                            "sessionId": active_session,
                            "toolCall": {"toolCallId": "tc-1", "kind": perm_kind, "title": token},
                            "options": [
                                {"optionId": "allow", "kind": "allow_once", "name": "Allow"},
                                {"optionId": "reject", "kind": "reject_once", "name": "Reject"},
                            ],
                        },
                    }
                )
                perm_reply = json.loads(sys.stdin.readline())
                outcome = perm_reply.get("result", {}).get("outcome", {})
                if outcome.get("optionId") != "allow":
                    send({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "cancelled"}})
                    break
        else:
            prefix = f"[{model_id}] " if model_id else ""
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": active_session,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": prefix + "acp-answer\n" + task},
                        },
                    },
                }
            )
            send({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})
        # Simulate persistent ACP server staying open until client closes stdin or terminates.
        time.sleep(0.05)
