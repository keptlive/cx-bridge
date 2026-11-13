"""Zero-dependency JSON-RPC 2.0 stdio MCP server for cx-bridge."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import sys

from . import __version__
from .inbox import (
    BridgeError,
    ack_message,
    format_provenance,
    list_messages,
    validate_identifier,
)

MAX_FRAME_BYTES = 4 * 1024 * 1024
SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")


def _tool_schemas():
    return [
        {
            "name": "bridge_targets",
            "description": "List configured cx-bridge targets and workspace mappings (never exposes secret env values).",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "bridge_run",
            "description": "Run an authorized scoped task inline on claude, codex, or agy and return its job record and result.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Configured target name (e.g. claude, codex, agy)."},
                    "prompt": {"type": "string", "description": "Scoped task prompt."},
                    "cwd": {"type": "string", "description": "Working directory path (mutually exclusive with workspace)."},
                    "workspace": {"type": "string", "description": "Named workspace profile."},
                    "write": {"type": "boolean", "description": "Request workspace-write mode (requires --allow-write on server)."},
                    "model": {"type": "string"},
                    "effort": {"type": "string"},
                    "name": {"type": "string"},
                    "timeout": {"type": "number"},
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
        },
        {
            "name": "bridge_resume",
            "description": "Continue a saved cx-bridge job's session inline on its original target.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job": {"type": "string", "description": "Saved cx-bridge job ID."},
                    "prompt": {"type": "string", "description": "Follow-up prompt."},
                    "model": {"type": "string"},
                    "effort": {"type": "string"},
                    "name": {"type": "string"},
                    "timeout": {"type": "number"},
                },
                "required": ["job", "prompt"],
                "additionalProperties": False,
            },
        },
        {
            "name": "bridge_handoff",
            "description": "Hand off a succeeded cx-bridge job's result to another target (claude, codex, or agy).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job": {"type": "string", "description": "Succeeded source job ID."},
                    "target": {"type": "string", "description": "Destination target name."},
                    "prompt": {"type": "string", "description": "Scoped instruction for the destination target."},
                    "cwd": {"type": "string"},
                    "workspace": {"type": "string"},
                    "write": {"type": "boolean"},
                    "model": {"type": "string"},
                    "effort": {"type": "string"},
                    "name": {"type": "string"},
                    "timeout": {"type": "number"},
                },
                "required": ["job", "target", "prompt"],
                "additionalProperties": False,
            },
        },
        {
            "name": "bridge_say",
            "description": "Send an authorized inline peer message to claude, codex, or agy (live session or durable inbox).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Destination target name (e.g. claude, codex, agy)."},
                    "message": {"type": "string", "description": "Inline message text (untrusted peer provenance is attached automatically)."},
                    "thread": {"type": "string", "description": "Target thread/session ID, 'ide', or '*' for active session."},
                    "cwd": {"type": "string", "description": "Optional workspace scope for active session matching."},
                    "sender": {"type": "string", "description": "Sender label when server --identity is not pinned."},
                    "wake": {"type": "boolean", "description": "Wake or run a synchronous turn on the target session."},
                    "inbox_only": {"type": "boolean", "description": "Store in durable inbox without live CLI transport."},
                    "write": {"type": "boolean", "description": "Allow write/agent mode when waking or targeting IDE chat (requires --allow-write)."},
                },
                "required": ["target", "message"],
                "additionalProperties": False,
            },
        },
        {
            "name": "bridge_inbox",
            "description": "List, atomically claim, or acknowledge inline messages in the cx-bridge durable inbox.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "claim", "ack"], "description": "Inbox operation (default: claim)."},
                    "target": {"type": "string", "description": "Filter by target name."},
                    "thread": {"type": "string", "description": "Filter by thread/session identifier."},
                    "session_id": {"type": "string", "description": "Current live session ID when claiming."},
                    "cwd": {"type": "string", "description": "Current workspace directory."},
                    "message_id": {"type": "string", "description": "Message ID prefix for action='ack'."},
                    "include_all": {"type": "boolean", "description": "Include delivered/acked messages when action='list'."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "bridge_jobs",
            "description": "List recent cx-bridge jobs or read a specific saved job and result.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job": {"type": "string", "description": "Specific job ID to inspect (omit to list recent jobs)."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "additionalProperties": False,
            },
        },
    ]


def _read_frame(instream):
    while True:
        line = instream.readline(MAX_FRAME_BYTES + 1)
        if not line:
            return None, None
        if len(line) > MAX_FRAME_BYTES:
            raise BridgeError("MCP input frame exceeds 4 MiB limit")
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(b"content-length:"):
            try:
                length = int(stripped.split(b":", 1)[1].strip())
            except ValueError as exc:
                raise BridgeError("Invalid Content-Length header") from exc
            if not (0 <= length <= MAX_FRAME_BYTES):
                raise BridgeError("Content-Length out of bounds")
            while True:
                header = instream.readline(4096)
                if not header or header in (b"\r\n", b"\n"):
                    break
            body = instream.read(length)
            if len(body) != length:
                return None, None
            return body, "content-length"
        return stripped, "ndjson"


def _write_frame(outstream, payload, mode):
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if mode == "content-length":
        outstream.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii") + encoded)
    else:
        outstream.write(encoded + b"\n")
    outstream.flush()


def _capture_cli_json(cli_mod, cfg, namespace):
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        code = cli_mod.run_job(namespace, cfg)
    raw = out_buf.getvalue().strip()
    parsed = json.loads(raw) if raw else {"exit_code": code}
    return code, parsed


def _execute_tool(cli_mod, config_file, allow_write, identity, name, arguments):
    if not isinstance(arguments, dict):
        raise BridgeError("Tool arguments must be an object")
    cfg = cli_mod.load_config(config_file)
    root = cli_mod.jobs_root(cfg)

    if name == "bridge_targets":
        return False, {
            "targets": cli_mod.summarize_targets(cfg),
            "workspaces": [{"name": k, "paths": v} for k, v in cfg["workspaces"].items()],
        }

    if name in ("bridge_run", "bridge_handoff"):
        want_write = bool(arguments.get("write", False))
        if want_write and not allow_write:
            raise BridgeError("MCP server is read-only; start `cx-bridge mcp --allow-write` to allow workspace-write tasks")
        if arguments.get("cwd") and arguments.get("workspace"):
            raise BridgeError("Specify either cwd or workspace, not both")
        cmd_name = "run" if name == "bridge_run" else "handoff"
        ns = argparse.Namespace(
            command=cmd_name,
            job=arguments.get("job"),
            target=arguments.get("target"),
            cwd=arguments.get("cwd"),
            workspace=arguments.get("workspace"),
            write=want_write,
            base=None,
            model=arguments.get("model"),
            effort=arguments.get("effort"),
            name=arguments.get("name") or cmd_name,
            timeout=arguments.get("timeout"),
            json=True,
            prompt=arguments.get("prompt", ""),
        )
        code, payload = _capture_cli_json(cli_mod, cfg, ns)
        return code != 0, payload

    if name == "bridge_resume":
        job_file = cli_mod.find_job(root, arguments.get("job", ""))
        parent = cli_mod.read_record(job_file)
        if parent["mode"] == "workspace-write" and not allow_write:
            raise BridgeError("MCP server is read-only; start `cx-bridge mcp --allow-write` to resume workspace-write jobs")
        ns = argparse.Namespace(
            command="resume",
            job=arguments["job"],
            target=None,
            model=arguments.get("model"),
            effort=arguments.get("effort"),
            name=arguments.get("name") or "resume",
            timeout=arguments.get("timeout"),
            json=True,
            prompt=arguments.get("prompt", ""),
        )
        code, payload = _capture_cli_json(cli_mod, cfg, ns)
        return code != 0, payload

    if name == "bridge_say":
        want_write = bool(arguments.get("write", False))
        if want_write and not allow_write:
            raise BridgeError("MCP server is read-only; start `cx-bridge mcp --allow-write` to send write-capable messages")
        sender = identity or arguments.get("sender") or "mcp-peer"
        validate_identifier(sender, "sender")
        thread = arguments.get("thread")
        ns = argparse.Namespace(
            command="say",
            target=arguments["target"],
            thread=thread if thread is not None else "*",
            message=arguments["message"],
            sender=sender,
            cwd=arguments.get("cwd"),
            wake=bool(arguments.get("wake", False)),
            inbox_only=bool(arguments.get("inbox_only", False)),
            write=want_write,
            timeout=None,
            json=True,
        )
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            code = cli_mod.deliver_say(ns, cfg)
        raw = out_buf.getvalue().strip()
        payload = json.loads(raw) if raw else {"ok": code == 0}
        return code != 0, payload

    if name == "bridge_inbox":
        action = arguments.get("action") or "claim"
        if action == "ack":
            msg_id = arguments.get("message_id")
            if not msg_id:
                raise BridgeError("message_id is required when action is 'ack'")
            acked = ack_message(root, msg_id)
            return False, {"acked": acked}
        if action not in ("list", "claim"):
            raise BridgeError("action must be 'list', 'claim', or 'ack'")
        target = arguments.get("target") or (identity if identity in cfg["targets"] else None)
        limit = int(arguments.get("limit") or 25)
        if not (1 <= limit <= 100):
            raise BridgeError("limit must be between 1 and 100")
        msgs = list_messages(
            root,
            target=target,
            thread=arguments.get("thread"),
            session_id=arguments.get("session_id"),
            cwd=arguments.get("cwd"),
            include_all=bool(arguments.get("include_all", False)) if action == "list" else False,
            claim=(action == "claim"),
            claimed_by=identity or arguments.get("session_id") or "mcp-inbox",
            limit=limit,
        )
        return False, {
            "count": len(msgs),
            "messages": [{**m, "framed": format_provenance(m)} for m in msgs],
        }

    if name == "bridge_jobs":
        if arguments.get("job"):
            file = cli_mod.find_job(root, arguments["job"])
            job = cli_mod.read_record(file)
            result_path = file.parent / "result.md"
            result = result_path.read_text(encoding="utf-8") if result_path.is_file() else ""
            return False, {"job": job, "result": result}
        limit = int(arguments.get("limit") or 20)
        if not (1 <= limit <= 100):
            raise BridgeError("limit must be between 1 and 100")
        rows = []
        for file in sorted(root.glob("*/*/job.json"))[-limit:]:
            try:
                rows.append(cli_mod.read_record(file))
            except BridgeError:
                continue
        return False, {"jobs": rows}

    raise BridgeError(f"Unknown MCP tool: {name}")


def serve_mcp(cli_mod, config_file, *, allow_write=False, identity=None, instream=None, outstream=None):
    if identity is not None:
        validate_identifier(identity, "identity")
    config_file = Path(config_file).expanduser()
    # Validate config upfront before entering the JSON-RPC loop.
    cli_mod.load_config(config_file)
    inp = instream if instream is not None else sys.stdin.buffer
    out = outstream if outstream is not None else sys.stdout.buffer

    while True:
        raw, mode = _read_frame(inp)
        if raw is None:
            return 0
        try:
            req = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            _write_frame(out, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, mode or "ndjson")
            continue
        if not isinstance(req, dict):
            _write_frame(out, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}, mode)
            continue
        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") if isinstance(req.get("params"), dict) else {}
        # JSON-RPC notifications omit "id" and receive no response.
        if "id" not in req:
            continue
        if method == "initialize":
            requested = params.get("protocolVersion")
            negotiated = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
            _write_frame(
                out,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": negotiated,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "cx-bridge", "version": __version__},
                    },
                },
                mode,
            )
        elif method == "ping":
            _write_frame(out, {"jsonrpc": "2.0", "id": req_id, "result": {}}, mode)
        elif method == "tools/list":
            _write_frame(out, {"jsonrpc": "2.0", "id": req_id, "result": {"tools": _tool_schemas()}}, mode)
        elif method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})
            try:
                is_err, result_obj = _execute_tool(cli_mod, config_file, allow_write, identity, tool_name, tool_args)
                _write_frame(
                    out,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": json.dumps(result_obj, ensure_ascii=False)}],
                            "isError": bool(is_err),
                        },
                    },
                    mode,
                )
            except (BridgeError, OSError, ValueError, TypeError, argparse.ArgumentTypeError) as exc:
                _write_frame(
                    out,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": json.dumps({"error": str(exc)}, ensure_ascii=False)}],
                            "isError": True,
                        },
                    },
                    mode,
                )
        else:
            _write_frame(
                out,
                {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}},
                mode,
            )
