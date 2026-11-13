"""Universal connectors for external agent frameworks and ACP/generic CLIs."""
import argparse
import asyncio
import contextlib
from dataclasses import dataclass, field
import inspect
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid

from . import __version__
from .inbox import (
    BridgeError,
    ack_message,
    format_provenance,
    list_messages,
    validate_identifier,
)

MAX_BRIDGE_JSON_BYTES = 16 * 1024 * 1024
MAX_ACP_LINE_BYTES = 8 * 1024 * 1024
MAX_EVENTS_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class AgentRequest:
    """Structured request passed to Python agent frameworks via serve_framework_target."""

    prompt: str
    task: str
    cwd: str
    mode: str
    model: str | None = None
    effort: str | None = None
    session: str | None = None


@dataclass
class AgentResponse:
    """Structured response returned by a Python agent framework handler."""

    response: str
    session_id: str | None = None
    status: str = "SUCCESS"
    error: str | None = None
    denied_actions: list[str] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)


def expand_template_args(args_template, values):
    """Safely replace fixed {key} tokens inside argv items without str.format()."""
    expanded = []
    for raw in args_template:
        item = raw
        for key in ("cwd", "session", "model", "effort", "mode", "thread"):
            token = "{" + key + "}"
            if token in item:
                replacement = values.get(key)
                if replacement is None:
                    raise BridgeError(f"Missing value for template placeholder {token}")
                item = item.replace(token, str(replacement))
        expanded.append(item)
    return expanded


def serve_framework_target(
    handler,
    *,
    name="framework-agent",
    version="0.1.0",
    supports_write=False,
    argv=None,
    instream=None,
    outstream=None,
    errstream=None,
):
    """Host any sync or async Python agent framework as a cx-bridge target.

    Redirects stray print()/logging output from noisy frameworks (e.g. CrewAI,
    LangGraph, AutoGen) to stderr (`runner.log`) so stdout stays pure JSONL.
    """
    validate_identifier(name, "framework name")
    args = list(sys.argv[1:] if argv is None else argv)
    inp = instream if instream is not None else sys.stdin.buffer
    out = outstream if outstream is not None else sys.stdout.buffer
    err = errstream if errstream is not None else sys.stderr

    def emit(obj):
        out.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        out.flush()

    if "--version" in args or "--help" in args:
        out.write(f"{name} {version} --bridge-json\n".encode("utf-8"))
        out.flush()
        return 0

    raw = inp.read(MAX_BRIDGE_JSON_BYTES + 1)
    if len(raw) > MAX_BRIDGE_JSON_BYTES:
        emit({"event": "result", "status": "ERROR", "response": "", "error": "payload_too_large", "denied_actions": []})
        return 1
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        emit({"event": "result", "status": "ERROR", "response": "", "error": "invalid_bridge_json", "denied_actions": []})
        return 1
    if not isinstance(payload, dict) or not isinstance(payload.get("prompt"), str):
        emit({"event": "result", "status": "ERROR", "response": "", "error": "missing_prompt", "denied_actions": []})
        return 1

    mode = payload.get("mode") or "read-only"
    if mode not in ("read-only", "workspace-write"):
        emit({"event": "result", "status": "ERROR", "response": "", "error": "invalid_mode", "denied_actions": []})
        return 1

    incoming_session = payload.get("session")
    if incoming_session is not None and (
        not isinstance(incoming_session, str) or not incoming_session or incoming_session.startswith("-")
    ):
        emit({"event": "result", "status": "ERROR", "response": "", "error": "invalid_session", "denied_actions": []})
        return 1
    session_id = incoming_session or f"{name}-{uuid.uuid4().hex[:12]}"

    emit({"event": "init", "session_id": session_id, "framework": name, "mode": mode})

    if mode == "workspace-write" and not supports_write:
        emit(
            {
                "event": "result",
                "session_id": session_id,
                "status": "ERROR",
                "response": "",
                "error": "workspace_write_not_supported",
                "denied_actions": ["workspace-write"],
            }
        )
        return 1

    prompt_text = payload["prompt"]
    task_text = prompt_text.split("TASK:\n", 1)[1] if "TASK:\n" in prompt_text else prompt_text
    req = AgentRequest(
        prompt=prompt_text,
        task=task_text,
        cwd=str(payload.get("cwd") or os.getcwd()),
        mode=mode,
        model=payload.get("model") if isinstance(payload.get("model"), str) else None,
        effort=payload.get("effort") if isinstance(payload.get("effort"), str) else None,
        session=incoming_session,
    )

    try:
        with contextlib.redirect_stdout(err):
            outcome = handler(req)
            if inspect.isawaitable(outcome):
                outcome = asyncio.run(outcome)
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            raise
        print(f"{name} handler error: {type(exc).__name__}", file=err)
        emit(
            {
                "event": "result",
                "session_id": session_id,
                "status": "ERROR",
                "response": "",
                "error": type(exc).__name__,
                "denied_actions": [],
            }
        )
        return 1

    if isinstance(outcome, AgentResponse):
        resp = outcome
        if not resp.session_id:
            resp.session_id = session_id
    elif isinstance(outcome, dict):
        resp = AgentResponse(
            response=str(outcome.get("response", outcome.get("result", ""))),
            session_id=str(outcome.get("session_id") or session_id),
            status=str(outcome.get("status", "SUCCESS")),
            error=str(outcome["error"]) if outcome.get("error") else None,
            denied_actions=[str(x) for x in outcome.get("denied_actions", [])],
            tool_calls=[str(x) for x in outcome.get("tool_calls", [])],
        )
    else:
        resp = AgentResponse(response="" if outcome is None else str(outcome), session_id=session_id)

    if resp.tool_calls:
        emit({"event": "tool_use", "session_id": resp.session_id, "tools": list(resp.tool_calls)})

    failed = resp.status != "SUCCESS" or bool(resp.error) or bool(resp.denied_actions)
    emit(
        {
            "event": "result",
            "session_id": resp.session_id,
            "status": "ERROR" if failed else "SUCCESS",
            "response": resp.response,
            "error": resp.error or "",
            "denied_actions": list(resp.denied_actions),
        }
    )
    return 1 if failed else 0


def invoke_acp(command, cwd, native_cwd, prompt, mode, model, session, directory, timeout, terminate_fn, env=None):
    """Drive an Agent Client Protocol (ACP) agent over stdio JSON-RPC 2.0."""
    deadline = time.monotonic() + timeout
    written_bytes = 0
    with (directory / "events.jsonl").open("wb") as events, (directory / "runner.log").open("wb") as log:
        kwargs = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log,
            **kwargs,
        )
        line_queue = queue.Queue()

        def _reader():
            try:
                while True:
                    line = process.stdout.readline(MAX_ACP_LINE_BYTES + 1)
                    if not line:
                        line_queue.put((None, "eof"))
                        return
                    if len(line) > MAX_ACP_LINE_BYTES:
                        line_queue.put((None, "oversize"))
                        return
                    line_queue.put((line, None))
            except OSError:
                line_queue.put((None, "eof"))

        threading.Thread(target=_reader, daemon=True).start()

        def record_event(raw_bytes):
            nonlocal written_bytes
            written_bytes += len(raw_bytes)
            if written_bytes > MAX_EVENTS_BYTES:
                raise BridgeError("Event output exceeds the 64 MiB parsing limit; inspect the private job files")
            events.write(raw_bytes)
            events.flush()

        def send_rpc(frame):
            encoded = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
            process.stdin.write(encoded)
            process.stdin.flush()

        def handle_agent_request(frame):
            req_id = frame.get("id")
            method = frame.get("method")
            params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
            if method == "session/request_permission":
                tool_call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
                tool_kind = str(tool_call.get("kind") or "other")
                allowed_kinds = ("read", "search", "think") if mode == "read-only" else ("read", "search", "think", "edit")
                permit = tool_kind in allowed_kinds
                options = params.get("options") if isinstance(params.get("options"), list) else []
                wanted = ("allow_once", "allow_always") if permit else ("reject_once", "reject_always")
                chosen = next(
                    (
                        opt.get("optionId")
                        for opt in options
                        if isinstance(opt, dict) and opt.get("kind") in wanted and isinstance(opt.get("optionId"), str)
                    ),
                    None,
                )
                if not permit:
                    record_event((json.dumps({"acp_event": "permission_denied", "tool_kind": tool_kind}) + "\n").encode("utf-8"))
                if chosen:
                    send_rpc({"jsonrpc": "2.0", "id": req_id, "result": {"outcome": {"outcome": "selected", "optionId": chosen}}})
                else:
                    send_rpc({"jsonrpc": "2.0", "id": req_id, "result": {"outcome": {"outcome": "cancelled"}}})
            else:
                record_event((json.dumps({"acp_event": "permission_denied", "method": str(method)}) + "\n").encode("utf-8"))
                send_rpc({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Capability disabled"}})

        def await_response(expected_id, allow_method_not_found=False):
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    terminate_fn(process)
                    return None, "timed_out"
                try:
                    raw_line, signal_kind = line_queue.get(timeout=remaining)
                except queue.Empty:
                    terminate_fn(process)
                    return None, "timed_out"
                if signal_kind == "oversize":
                    terminate_fn(process)
                    raise BridgeError("ACP provider emitted a frame exceeding 8 MiB")
                if signal_kind == "eof":
                    return None, "eof"
                if not raw_line.strip():
                    continue
                record_event(raw_line if raw_line.endswith(b"\n") else raw_line + b"\n")
                try:
                    frame = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    terminate_fn(process)
                    raise BridgeError("Provider produced invalid JSONL; inspect events.jsonl") from exc
                if not isinstance(frame, dict):
                    terminate_fn(process)
                    raise BridgeError("Provider emitted a non-object event")
                if "method" in frame and "id" in frame:
                    handle_agent_request(frame)
                    continue
                if frame.get("id") == expected_id:
                    if allow_method_not_found and isinstance(frame.get("error"), dict) and frame["error"].get("code") == -32601:
                        return frame, "ignored_optional"
                    return frame, "ok"

        try:
            send_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": 1,
                        "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                        "clientInfo": {"name": "cx-bridge", "version": __version__},
                    },
                }
            )
            init_resp, status = await_response(1)
            if status == "timed_out":
                return 124, "timed_out"
            if status != "ok" or "error" in init_resp:
                terminate_fn(process)
                return process.returncode or 1, "finished"

            if session:
                send_rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "session/load",
                        "params": {"sessionId": session, "cwd": native_cwd, "mcpServers": []},
                    }
                )
            else:
                send_rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "session/new",
                        "params": {"cwd": native_cwd, "mcpServers": []},
                    }
                )
            sess_resp, status = await_response(2)
            if status == "timed_out":
                return 124, "timed_out"
            if status != "ok" or "error" in sess_resp:
                terminate_fn(process)
                return process.returncode or 1, "finished"
            res_obj = sess_resp.get("result") if isinstance(sess_resp.get("result"), dict) else {}
            active_session = res_obj.get("sessionId") or session
            if not isinstance(active_session, str) or not active_session or active_session.startswith("-"):
                terminate_fn(process)
                raise BridgeError("ACP provider did not return a valid sessionId")

            if model:
                send_rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "session/set_model",
                        "params": {"sessionId": active_session, "modelId": model},
                    }
                )
                _, model_status = await_response(3, allow_method_not_found=True)
                if model_status == "timed_out":
                    return 124, "timed_out"

            send_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "session/prompt",
                    "params": {"sessionId": active_session, "prompt": [{"type": "text", "text": prompt}]},
                }
            )
            prompt_resp, status = await_response(4)
            if status == "timed_out":
                return 124, "timed_out"
            try:
                process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                terminate_fn(process)
                return (0 if status == "ok" and "error" not in (prompt_resp or {}) else 1), "finished"
            if status != "ok":
                return process.returncode or 1, "finished"
            return (0 if process.returncode in (0, None) and "error" not in prompt_resp else (process.returncode or 1)), "finished"
        except BaseException:
            terminate_fn(process)
            raise


class BridgeConnector:
    """In-process Python connector for LangGraph, CrewAI, AutoGen, PydanticAI, etc."""

    def __init__(self, config=None, *, identity="framework", allow_write=False):
        validate_identifier(identity, "identity")
        from . import cli as cli_mod

        self._cli = cli_mod
        self.config_path = Path(config).expanduser() if config is not None else cli_mod.config_path()
        self.identity = identity
        self.allow_write = bool(allow_write)

    def _cfg(self):
        return self._cli.load_config(self.config_path)

    def targets(self) -> list[dict]:
        """List configured cx-bridge targets without exposing secret env values."""
        return self._cli.summarize_targets(self._cfg())

    def run(
        self,
        prompt: str,
        *,
        to: str | None = None,
        cwd: str | None = None,
        workspace: str | None = None,
        write: bool = False,
        model: str | None = None,
        effort: str | None = None,
        name: str = "connector-run",
        timeout: float | None = None,
    ) -> dict:
        """Run a scoped task on a configured target and return its structured job and result."""
        if write and not self.allow_write:
            raise BridgeError("BridgeConnector is read-only; initialize with allow_write=True for write tasks")
        if cwd and workspace:
            raise BridgeError("Specify either cwd or workspace, not both")
        cfg = self._cfg()
        ns = argparse.Namespace(
            command="run",
            job=None,
            target=to,
            cwd=cwd,
            workspace=workspace,
            write=bool(write),
            base=None,
            model=model,
            effort=effort,
            name=name,
            timeout=timeout,
            json=True,
            prompt=prompt,
        )
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            code = self._cli.run_job(ns, cfg)
        payload = json.loads(out_buf.getvalue())
        payload["exit_code"] = code
        return payload

    def resume(
        self,
        job: str,
        prompt: str,
        *,
        model: str | None = None,
        effort: str | None = None,
        name: str = "connector-resume",
        timeout: float | None = None,
    ) -> dict:
        """Continue a saved cx-bridge job session on its original target."""
        cfg = self._cfg()
        root = self._cli.jobs_root(cfg)
        parent = self._cli.read_record(self._cli.find_job(root, job))
        if parent["mode"] == "workspace-write" and not self.allow_write:
            raise BridgeError("BridgeConnector is read-only; initialize with allow_write=True to resume write jobs")
        ns = argparse.Namespace(
            command="resume",
            job=job,
            target=None,
            model=model,
            effort=effort,
            name=name,
            timeout=timeout,
            json=True,
            prompt=prompt,
        )
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            code = self._cli.run_job(ns, cfg)
        payload = json.loads(out_buf.getvalue())
        payload["exit_code"] = code
        return payload

    def handoff(
        self,
        job: str,
        prompt: str,
        *,
        to: str,
        cwd: str | None = None,
        workspace: str | None = None,
        write: bool = False,
        model: str | None = None,
        effort: str | None = None,
        name: str = "connector-handoff",
        timeout: float | None = None,
    ) -> dict:
        """Hand off a succeeded cx-bridge job's result to another target."""
        if write and not self.allow_write:
            raise BridgeError("BridgeConnector is read-only; initialize with allow_write=True for write handoffs")
        if cwd and workspace:
            raise BridgeError("Specify either cwd or workspace, not both")
        cfg = self._cfg()
        ns = argparse.Namespace(
            command="handoff",
            job=job,
            target=to,
            cwd=cwd,
            workspace=workspace,
            write=bool(write),
            base=None,
            model=model,
            effort=effort,
            name=name,
            timeout=timeout,
            json=True,
            prompt=prompt,
        )
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            code = self._cli.run_job(ns, cfg)
        payload = json.loads(out_buf.getvalue())
        payload["exit_code"] = code
        return payload

    def say(
        self,
        message: str,
        *,
        to: str,
        thread: str = "*",
        cwd: str | None = None,
        wake: bool = False,
        inbox_only: bool = False,
        write: bool = False,
        timeout: float | None = None,
    ) -> dict:
        """Send an inline message to Claude Code, Codex, Antigravity, or any configured framework target."""
        if write and not self.allow_write:
            raise BridgeError("BridgeConnector is read-only; initialize with allow_write=True for write messages")
        cfg = self._cfg()
        ns = argparse.Namespace(
            command="say",
            target=to,
            thread=thread,
            message=message,
            sender=self.identity,
            cwd=cwd,
            wake=bool(wake),
            inbox_only=bool(inbox_only),
            write=bool(write),
            timeout=timeout,
            json=True,
        )
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            code = self._cli.deliver_say(ns, cfg)
        raw = out_buf.getvalue().strip()
        payload = json.loads(raw) if raw else {"ok": code == 0}
        payload["exit_code"] = code
        return payload

    def inbox(
        self,
        *,
        target: str | None = None,
        thread: str | None = None,
        session_id: str | None = None,
        cwd: str | None = None,
        claim: bool = True,
        include_all: bool = False,
        limit: int = 25,
    ) -> list[dict]:
        """Claim or inspect inline messages addressed to this agent framework."""
        cfg = self._cfg()
        chosen = target or (self.identity if self.identity in cfg["targets"] else None)
        msgs = list_messages(
            self._cli.jobs_root(cfg),
            target=chosen,
            thread=thread,
            session_id=session_id,
            cwd=cwd,
            include_all=include_all if not claim else False,
            claim=claim,
            claimed_by=self.identity,
            limit=limit,
        )
        return [{**m, "framed": format_provenance(m)} for m in msgs]

    def ack(self, message_id: str) -> dict:
        """Acknowledge a processed inline inbox message."""
        cfg = self._cfg()
        return ack_message(self._cli.jobs_root(cfg), message_id)

    def as_tools(self) -> list:
        """Return typed, docstring-documented tool callables for LangGraph, PydanticAI, CrewAI, AutoGen, etc."""

        def bridge_run(prompt: str, target: str = "", cwd: str = "") -> str:
            """Run an authorized read-only task on another agent (claude, codex, agy, or custom target) and return JSON."""
            return json.dumps(self.run(prompt, to=target or None, cwd=cwd or None), ensure_ascii=False)

        def bridge_resume(job_id: str, prompt: str) -> str:
            """Continue a saved cx-bridge job session and return JSON."""
            return json.dumps(self.resume(job_id, prompt), ensure_ascii=False)

        def bridge_handoff(job_id: str, target: str, prompt: str) -> str:
            """Hand off a succeeded cx-bridge job's result to another agent target and return JSON."""
            return json.dumps(self.handoff(job_id, prompt, to=target), ensure_ascii=False)

        def bridge_say(target: str, message: str, thread: str = "*", wake: bool = False) -> str:
            """Send an inline message to claude, codex, agy, or another configured agent target."""
            return json.dumps(self.say(message, to=target, thread=thread, wake=wake), ensure_ascii=False)

        def bridge_inbox(claim: bool = True) -> str:
            """Check and claim unread inline peer messages addressed to this agent."""
            return json.dumps(self.inbox(claim=claim), ensure_ascii=False)

        return [bridge_run, bridge_resume, bridge_handoff, bridge_say, bridge_inbox]
