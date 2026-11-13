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
    if "chat" in args and os.environ.get("FAKE_NO_CHAT"):
        raise SystemExit(2)
    help_line = (
        "fake 1.0 --output-format --strict-mcp-config --setting-sources --tools "
        "--permission-mode --input-format --mode --sandbox --conversation --add-dir"
    )
    print(help_line, file=sys.stderr if kind == "agy" else sys.stdout)
    raise SystemExit(0)
if "agents" in args and "--json" in args:
    print(json.dumps([{"session_id": "claude-bg-session", "name": "fixture-bg", "updated_at": "2026-11-13T00:00:00Z"}]))
    raise SystemExit(0)
if kind == "custom_say":
    msg = sys.stdin.buffer.read().decode("utf-8")
    Path(os.environ["FAKE_CUSTOM_SAY_FILE"]).write_text(json.dumps({"args": args, "msg": msg}), encoding="utf-8")
    raise SystemExit(0)
if "chat" in args:
    if os.environ.get("FAKE_QUEUE_SECRET"):
        print("fixture-private-stdout")
        print("fixture-private-stderr", file=sys.stderr)
    chat_mode = args[args.index("-m") + 1] if "-m" in args else "ask"
    message = sys.stdin.buffer.read().decode("utf-8") if "-" in args else args[-1]
    if "FAKE_CHAT_FILE" in os.environ:
        Path(os.environ["FAKE_CHAT_FILE"]).write_text(f"{chat_mode}:{message}", encoding="utf-8")
    if "write a report to " in message and os.environ.get("FAKE_RECEIPT"):
        receipt = message.split("write a report to ")[1].split(". Include")[0]
        Path(receipt).write_text("AGY IDE fixture complete", encoding="utf-8")
    raise SystemExit(0)
if "queue" in args:
    if os.environ.get("FAKE_QUEUE_SECRET"):
        print("fixture-private-stdout")
        print("fixture-private-stderr", file=sys.stderr)
    message = args[args.index("--message") + 1]
    if "FAKE_QUEUE_ARGS_FILE" in os.environ:
        Path(os.environ["FAKE_QUEUE_ARGS_FILE"]).write_text(json.dumps(args), encoding="utf-8")
    Path(os.environ["FAKE_QUEUE_FILE"]).write_text(message, encoding="utf-8")
    if "write a report to " in message and os.environ.get("FAKE_RECEIPT"):
        receipt = message.split("write a report to ")[1].split(". Include")[0]
        Path(receipt).write_text("Fixture complete", encoding="utf-8")
    raise SystemExit(0)

prompt = sys.stdin.buffer.read().decode("utf-8")
if "write a report to " in prompt and os.environ.get("FAKE_RECEIPT"):
    receipt = prompt.split("write a report to ")[1].split(". Include")[0]
    Path(receipt).write_text(f"{kind} wake fixture complete", encoding="utf-8")
if "TIMEOUT" in prompt:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path("child.pid").write_text(str(child.pid))
    if kind == "codex":
        print(json.dumps({"type": "thread.started", "thread_id": "timeout-session"}), flush=True)
    elif kind == "agy":
        print(json.dumps({"event": "init", "conversation_id": "timeout-session", "init": {"conversation_id": "timeout-session"}}), flush=True)
        print(json.dumps({"event": "step_update", "step_update": {"conversation_id": "timeout-session", "step_type": "agent_response", "text_delta": "partial-agy"}}), flush=True)
    else:
        print(json.dumps({"type": "system", "session_id": "timeout-session"}), flush=True)
    print('{"type":', flush=True)
    time.sleep(60)
if "INVALID_JSON" in prompt:
    print("not json")
    raise SystemExit(0)
if "NONZERO" in prompt:
    print(json.dumps({"type": "error", "message": "fixture failure"}))
    raise SystemExit(7)
if "MALFORMED_ITEM" in prompt:
    if kind == "agy":
        print(json.dumps({"event": "init", "conversation_id": "salvaged-session", "init": {"conversation_id": "salvaged-session"}}))
        print(json.dumps({"event": "step_update", "step_update": None}))
    else:
        print(json.dumps({"type": "thread.started", "thread_id": "salvaged-session"}))
        print(json.dumps({"type": "item.completed", "item": None}))
    raise SystemExit(0)
if "ENV_CHECK" in prompt:
    prompt = os.environ.get("BRIDGE_FIXTURE_VALUE", "env-unset")


def emit(event):
    print(json.dumps(event), flush=True)


session_override = None
if "--conversation" in args:
    session_override = args[args.index("--conversation") + 1]
elif "--resume" in args:
    session_override = args[args.index("--resume") + 1]
elif "resume" in args:
    session_override = args[args.index("resume") + 1]

if kind == "codex":
    sid = session_override or "codex-session"
    emit({"type": "thread.started", "thread_id": sid})
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "answer\n" + prompt.split("TASK:\n")[-1]}})
    if "NO_TERMINAL" not in prompt:
        emit({"type": "turn.failed" if "FAIL" in prompt else "turn.completed"})
elif kind == "agy":
    sid = session_override or "agy-session"
    answer_text = "answer\n" + prompt.split("TASK:\n")[-1]
    emit({"event": "init", "conversation_id": sid, "init": {"conversation_id": sid, "permission_mode": args[args.index("--mode") + 1] if "--mode" in args else "plan"}})
    emit({"event": "step_update", "step_update": {"conversation_id": sid, "step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": answer_text}})
    if "NO_TERMINAL" not in prompt:
        emit({
            "event": "result",
            "result": {
                "conversation_id": sid,
                "status": "ERROR" if "FAIL" in prompt else "SUCCESS",
                "error": "fixture error" if "FAIL" in prompt else "",
                "denied_actions": [{"tool": "run_command"}] if "DENY" in prompt else [],
                "response": answer_text,
            },
        })
elif kind == "generic_json":
    sid = session_override or "generic-json-session"
    print(
        json.dumps(
            {
                "session_id": sid,
                "status": "error" if "FAIL" in prompt else "ok",
                "response": "generic-json:" + "|".join(args) + ":" + prompt.split("TASK:\n")[-1],
            }
        ),
        flush=True,
    )
elif kind == "generic_text":
    if "FAIL" in prompt:
        raise SystemExit(3)
    print("generic-text:" + prompt.split("TASK:\n")[-1], flush=True)
else:
    sid = session_override or "claude-session"
    emit({"type": "system", "subtype": "init", "session_id": sid})
    if "NO_TERMINAL" not in prompt:
        emit({
            "type": "result",
            "session_id": sid,
            "subtype": "success",
            "is_error": "FAIL" in prompt,
            "permission_denials": ["Edit"] if "DENY" in prompt else [],
            "result": "answer\n" + prompt.split("TASK:\n")[-1],
        })
