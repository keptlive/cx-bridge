"""Read-only task, message, and file activity from saved bridge records."""
from datetime import datetime
import json
from pathlib import Path, PureWindowsPath
import re
import shlex
import sys
import time

from .inbox import BridgeError, list_messages

MAX_EVENT_BYTES = 64 * 1024 * 1024
BRIDGE_TASK_MARKER = "TASK:\n"
READ_TOOLS = ("cat", "head", "tail", "nl", "sed")


def obj(value):
    return value if isinstance(value, dict) else {}


def array(value):
    return value if isinstance(value, list) else []


def string(value):
    return value if isinstance(value, str) and value else None


def command_read(command):
    """Recognize simple readers only. Never evaluate a provider command."""
    command = string(command)
    if not command:
        return None
    wrapper = re.fullmatch(r"""(?is)(?:"([^"]+)"|(\S+))\s+(-lc|-c|-Command)\s+(.+)""", command)
    if wrapper:
        shell = PureWindowsPath(wrapper[1] or wrapper[2]).stem.lower()
        if shell in ("bash", "sh", "zsh", "pwsh", "powershell"):
            inner = wrapper[4]
            if inner[0] in "\"'" and inner[-1] == inner[0]:
                command = inner[1:-1]
    if re.search(r"[\r\n;&|<>#$\x60*?\[\]]", command):
        return None
    if re.match(r"(?i)get-content\s", command):
        match = re.fullmatch(
            r"""(?i)get-content\s+-literalpath\s+(?:'([^']+)'|"([^"]+)"|([^\s'"]+))"""
            r"(?:\s+-raw|\s+-totalcount\s+\d+|\s+-tail\s+\d+)?\s*",
            command,
        )
        if match:
            return "Get-Content", next(path for path in match.groups() if path is not None)
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if not words:
        return None
    tool, *args = words
    if tool not in READ_TOOLS:
        return None
    if tool == "sed":
        if len(args) != 3 or args[0] != "-n" or not re.fullmatch(r"\d+(?:,\d+)?p", args[1]):
            return None
        args = args[2:]
    elif tool in ("head", "tail") and len(args) >= 2 and args[0] == "-n" and args[1].isdigit():
        args = args[2:]
    elif tool == "nl" and args[:1] == ["-ba"]:
        args = args[1:]
    ends_options = args[:1] == ["--"]
    if ends_options:
        args = args[1:]
    if len(args) == 1 and args[0] != "-" and (ends_options or not args[0].startswith("-")):
        return tool, args[0]
    return None


class JobActivity:
    def __init__(self, kind):
        self.kind = kind
        self.session = None
        self.plan = []
        self.reads = {}
        self.active_tools = {}
        self.claude_calls = {}
        self.acp_calls = {}

    def read(self, path, tool, evidence):
        if string(path) and path not in self.reads:
            self.reads[path] = {"path": path, "tool": tool, "evidence": evidence}

    def update(self, event):
        event_type = event.get("type")
        if self.kind == "codex":
            if event_type == "thread.started":
                self.session = string(event.get("thread_id")) or self.session
            item = obj(event.get("item"))
            if event_type in ("item.started", "item.updated", "item.completed") and item.get("type") == "todo_list":
                self.plan = [
                    {"text": todo["text"], "completed": todo.get("completed") is True}
                    for todo in array(item.get("items")) if string(obj(todo).get("text"))
                ]
            item_id = string(item.get("id"))
            tool_label = {
                "command_execution": "shell command",
                "mcp_tool_call": string(item.get("tool")) or "MCP tool",
                "web_search": "web search",
                "file_change": "file change",
            }.get(item.get("type") if isinstance(item.get("type"), str) else None)
            if item_id and tool_label:
                if event_type == "item.started":
                    self.active_tools[item_id] = tool_label
                elif event_type == "item.completed":
                    self.active_tools.pop(item_id, None)
            if (event_type == "item.completed" and item.get("type") == "command_execution"
                    and item.get("status") == "completed" and item.get("exit_code") == 0):
                read = command_read(item.get("command"))
                if read:
                    tool, path = read
                    self.read(path, tool, "inferred_from_command")
        elif self.kind == "claude":
            self.session = string(event.get("session_id")) or self.session
            if event_type not in ("assistant", "user"):
                return
            for raw in array(obj(event.get("message")).get("content")):
                block = obj(raw)
                if event_type == "assistant" and block.get("type") == "tool_use" and block.get("name") == "Read":
                    call_id = string(block.get("id"))
                    path = string(obj(block.get("input")).get("file_path"))
                    if call_id and path:
                        self.claude_calls[call_id] = path
                        self.active_tools[call_id] = "Read"
                if event_type == "user" and block.get("type") == "tool_result":
                    call_id = string(block.get("tool_use_id"))
                    path = self.claude_calls.pop(call_id, None)
                    self.active_tools.pop(call_id, None)
                    if not block.get("is_error"):
                        self.read(path, "Read", "provider_reported")
        elif self.kind == "agy":
            step = obj(event.get("step_update"))
            self.session = (string(event.get("conversation_id")) or string(step.get("conversation_id"))
                            or string(obj(event.get("init")).get("conversation_id")) or self.session)
            tool = obj(step.get("tool_info"))
            name = string(tool.get("name")) or string(step.get("tool_name"))
            step_id = step.get("step_index")
            if type(step_id) is int and step.get("step_type") == "tool":
                if step.get("state") == "ACTIVE" and name:
                    self.active_tools[step_id] = name
                elif step.get("state") in ("DONE", "ERROR"):
                    self.active_tools.pop(step_id, None)
            if (step.get("state") == "DONE" and step.get("step_type") == "tool"
                    and name == "view_file" and not tool.get("error")):
                self.read(obj(tool.get("parameters")).get("AbsolutePath"), "view_file", "provider_reported")
        elif self.kind == "acp":
            if event.get("id") == 2:
                self.session = string(obj(event.get("result")).get("sessionId")) or self.session
            if event.get("method") != "session/update":
                return
            params = obj(event.get("params"))
            self.session = string(params.get("sessionId")) or self.session
            update = obj(params.get("update"))
            call_id = string(update.get("toolCallId"))
            if not call_id or update.get("sessionUpdate") not in ("tool_call", "tool_call_update"):
                return
            call = self.acp_calls.setdefault(call_id, {})
            call.update({key: update[key] for key in ("kind", "locations", "status") if key in update})
            if call.get("status") == "in_progress":
                self.active_tools[call_id] = string(call.get("kind")) or "ACP tool"
            else:
                self.active_tools.pop(call_id, None)
            if call.get("status") == "completed" and call.get("kind") == "read":
                for location in array(call.get("locations")):
                    self.read(obj(location).get("path"), "ACP read", "provider_reported")
        else:
            self.session = next((event[key] for key in ("session_id", "sessionId", "conversation_id", "thread_id")
                                 if string(event.get(key))), self.session)
            if (event.get("event") or event_type) == "file_read" and event.get("status") == "completed":
                self.read(event.get("path"), "file_read", "provider_reported")


def activity_for(file, job, cache):
    event_file = file.parent / "events.jsonl"
    try:
        stat = event_file.stat()
    except FileNotFoundError:
        return JobActivity(job["kind"]), "waiting" if job["status"] == "running" else "unavailable"
    if job["kind"] == "generic" and job.get("event_format") in ("text", "json"):
        return JobActivity(job["kind"]), "unavailable"
    signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns, job["status"])
    previous = cache.get(file)
    if previous and previous[0] == signature:
        return previous[1], previous[2]
    activity = JobActivity(job["kind"])
    coverage = "available"
    offset = 0
    if previous:
        old_inode, old_size, old_mtime, old_status = previous[0]
        appended = old_size < stat.st_size
        unchanged = old_size == stat.st_size and old_mtime == stat.st_mtime_ns
        if old_inode == stat.st_ino and old_status == "running" and (appended or unchanged):
            _, activity, coverage, offset = previous
    if stat.st_size > MAX_EVENT_BYTES:
        coverage = "event_log_too_large"
        activity = JobActivity(job["kind"])
    else:
        with event_file.open("rb") as stream:
            stream.seek(offset)
            while offset < stat.st_size:
                line = stream.readline(stat.st_size - offset)
                if not line or (not line.endswith(b"\n") and job["status"] == "running"):
                    break
                offset += len(line)
                try:
                    event = json.loads(line)
                except (UnicodeError, ValueError, RecursionError):
                    if line.strip():
                        coverage = "partial"
                    continue
                if isinstance(event, dict):
                    activity.update(event)
                else:
                    coverage = "partial"
    cache[file] = (signature, activity, coverage, offset)
    return activity, coverage


def task_summary(file):
    try:
        with (file.parent / "prompt.txt").open(encoding="utf-8", errors="replace") as stream:
            prompt = stream.read(4096)
    except OSError:
        return None
    _, marker, body = prompt.partition(BRIDGE_TASK_MARKER)
    if marker:
        first_line = body.lstrip().split("\n", 1)[0]
        if first_line:
            return first_line[:200] + ("…" if len(first_line) > 200 else "")
    return None


def board(root, read_record, *, job_file=None, target=None, group=None, limit=25,
          show_messages=False, show_tasks=False, cache=None):
    cache = {} if cache is None else cache
    jobs = []
    files = [job_file] if job_file else root.glob("*/*/job.json")
    recent = []
    for file in files:
        try:
            job = read_record(file)
        except (BridgeError, OSError):
            if job_file:
                raise
            continue
        if (target and job["target"] != target) or (group and job.get("group") != group):
            continue
        if job["status"] == "running":
            jobs.append((file, job))
        else:
            recent.append((file, job))
    for entries in (jobs, recent):
        entries.sort(key=lambda entry: string(entry[1].get("started_at")) or str(entry[0]), reverse=True)
    jobs = (jobs + recent)[:limit]
    tasks = []
    for file, job in jobs:
        activity, coverage = activity_for(file, job, cache)
        task = {key: job.get(key) for key in (
            "id", "name", "target", "kind", "status", "mode", "model", "effort", "cwd",
            "workspace", "group", "parent", "source_job", "started_at", "finished_at", "error",
        )}
        task["session"] = job.get("session") or activity.session
        task["plan"] = activity.plan
        task["active_tools"] = list(activity.active_tools.values()) if job["status"] == "running" else []
        task["files_read"] = list(activity.reads.values())
        task["event_log"] = coverage
        if show_tasks:
            task["summary"] = task_summary(file)
        tasks.append(task)
    visible = {file for file, _ in jobs}
    for old in cache.keys() - visible:
        del cache[old]
    task = tasks[0] if job_file and tasks else None
    messages = list_messages(
        root, target=target or (task["target"] if task else None), group=group,
        threads={task["id"], task["session"]} - {None} if task else None,
        include_all=True, limit=limit,
    ) if not job_file or task else []
    rows = []
    for message in messages:
        row = {key: message.get(key) for key in (
            "id", "sender", "target", "thread", "group", "delivery", "status",
            "created_at", "delivered_at", "acked_at", "task_file", "receipt",
        )}
        if row["receipt"]:
            try:
                ready = Path(row["receipt"]).stat().st_size > 0
            except OSError:
                ready = False
            row["receipt_ready"] = ready
        if show_messages:
            row["body"] = message["body"]
        rows.append(row)
    return {"tasks": tasks, "messages": rows}


def display(value):
    # Avoid terminal control characters in provider data and peer text.
    return "".join(char if char.isprintable() else ascii(char)[1:-1] for char in str(value))


def print_board(snapshot):
    print("Tasks")
    if not snapshot["tasks"]:
        print("  No matching jobs")
    for task in snapshot["tasks"]:
        label = task["name"] or task["id"]
        print(display(f"  [{task['status']}] {label}  {task['target']}  {task['model'] or 'default model'}"))
        print(display(f"    Job {task['id']}  Session {task['session'] or 'unavailable'}"))
        print(display(f"    Workspace {task['cwd']}"))
        if task.get("summary"):
            print(display(f"    Brief {task['summary']}"))
        for tool in task["active_tools"]:
            print(display(f"    In progress {tool}"))
        for step in task["plan"]:
            print(display(f"    [{'x' if step['completed'] else ' '}] {step['text']}"))
        for read in task["files_read"]:
            evidence = "inferred" if read["evidence"] == "inferred_from_command" else "reported"
            print(display(f"    Read ({evidence}, {read['tool']}) {read['path']}"))
        if not task["files_read"]:
            print("    No file reads recorded.")
        if task["event_log"] != "available":
            print(display(f"    Event log {task['event_log']}"))
    print("Messages")
    if not snapshot["messages"]:
        print("  No matching messages")
    for msg in snapshot["messages"]:
        print(display(f"  {msg['created_at']}  [{msg['status']}] {msg['sender'] or 'unspecified'}"
                      f" -> {msg['target']} ({msg['thread']})  {msg['delivery']}  {msg['id']}"))
        if msg["task_file"]:
            if msg["receipt_ready"]:
                status = "receipt available"
            elif msg["status"] == "failed":
                status = "send failed"
            else:
                status = "awaiting receipt"
            print(display(f"    Task {msg['task_file']}  ({status})"))
        for line in msg.get("body", "").splitlines():
            print(f"    {display(line)}")


def run_board(args, root, read_record, find_job):
    job_file = find_job(root, args.job) if args.job else None
    cache = {}
    previous = None
    while True:
        snapshot = board(root, read_record, job_file=job_file, target=args.target, group=args.group,
                         limit=args.limit, show_messages=args.show_messages,
                         show_tasks=args.show_tasks, cache=cache)
        if snapshot != previous:
            if args.json:
                print(json.dumps(snapshot, ensure_ascii=True), flush=True)
            else:
                if args.follow:
                    print(f"\n{datetime.now().astimezone().isoformat(timespec='seconds')}")
                print_board(snapshot)
                sys.stdout.flush()
            previous = snapshot
        if not args.follow:
            return 0
        time.sleep(1)
