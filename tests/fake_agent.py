"""Deterministic provider fixture; never contacts a model."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

kind = sys.argv[1]
args = sys.argv[2:]
if "--help" in args or "--version" in args:
    if "queue" in args and os.environ.get("FAKE_NO_QUEUE"):
        raise SystemExit(2)
    print("fake 1.0 --output-format --strict-mcp-config --setting-sources --tools --permission-mode")
    raise SystemExit(0)
if "queue" in args:
    if os.environ.get("FAKE_QUEUE_SECRET"):
        print("fixture-private-stdout")
        print("fixture-private-stderr", file=sys.stderr)
    message = args[args.index("--message") + 1]
    Path(os.environ["FAKE_QUEUE_FILE"]).write_text(message, encoding="utf-8")
    if "write a report to " in message and os.environ.get("FAKE_RECEIPT"):
        receipt = message.split("write a report to ")[1].split(". Include")[0]
        Path(receipt).write_text("Fixture complete", encoding="utf-8")
    raise SystemExit(0)
prompt = sys.stdin.buffer.read().decode("utf-8")
if "TIMEOUT" in prompt:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path("child.pid").write_text(str(child.pid))
    print(json.dumps({"type": "thread.started", "thread_id": "timeout-session"}) if kind == "codex"
          else json.dumps({"type": "system", "session_id": "timeout-session"}), flush=True)
    print('{"type":', flush=True)
    time.sleep(60)
if "INVALID_JSON" in prompt:
    print("not json")
    raise SystemExit(0)
if "NONZERO" in prompt:
    print(json.dumps({"type": "error", "message": "fixture failure"}))
    raise SystemExit(7)
if "MALFORMED_ITEM" in prompt:
    print(json.dumps({"type": "thread.started", "thread_id": "salvaged-session"}))
    print(json.dumps({"type": "item.completed", "item": None}))
    raise SystemExit(0)
if "ENV_CHECK" in prompt:
    prompt = os.environ.get("BRIDGE_FIXTURE_VALUE", "env-unset")

def emit(event):
    print(json.dumps(event), flush=True)

if kind == "codex":
    emit({"type": "thread.started", "thread_id": "codex-session"})
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "answer\n" + prompt.split("TASK:\n")[-1]}})
    if "NO_TERMINAL" not in prompt:
        emit({"type": "turn.failed" if "FAIL" in prompt else "turn.completed"})
else:
    emit({"type": "system", "subtype": "init", "session_id": "claude-session"})
    if "NO_TERMINAL" not in prompt:
        emit({"type": "result", "session_id": "claude-session", "subtype": "success",
              "is_error": "FAIL" in prompt, "permission_denials": ["Edit"] if "DENY" in prompt else [],
              "result": "answer\n" + prompt.split("TASK:\n")[-1]})
