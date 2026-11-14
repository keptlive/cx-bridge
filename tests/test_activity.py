"""Activity board regressions using private deterministic job records."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

import test_bridge
from test_bridge import SRC_ROOT


def line(payload):
    return json.dumps(payload) + "\n"


def command(command, code=0):
    return {"type": "item.completed", "item": {
        "type": "command_execution", "command": command, "status": "completed",
        "exit_code": code, "aggregated_output": "PRIVATE TOOL OUTPUT",
    }}


class ActivityTests(test_bridge.BridgeTestCase):
    def fixture_job(self, kind, payload, *, job_id=None, status="succeeded", group="pilot"):
        job_id = job_id or f"fixture-{kind}"
        folder = Path(self.cfg["jobs_dir"]) / "2026-11-13" / job_id
        folder.mkdir(parents=True)
        record = {
            "id": job_id, "target": kind, "kind": kind, "target_signature": "fixture",
            "cwd": str(self.work), "mode": "read-only", "status": status, "group": group,
        }
        (folder / "job.json").write_text(json.dumps(record), encoding="utf-8")
        events_file = folder / "events.jsonl"
        events_file.write_text(payload, encoding="utf-8")
        return events_file, record

    def snapshot(self, *args):
        code, out, err = self.call("board", "--json", *args)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_group_filters_old_jobs_and_explicit_job_thread_messages(self):
        self.assertEqual(self.call("run", "--group", "pilot", "-m", "gpt-6.1-terra",
                                   "-n", "inbox-review", "-C", str(self.work), "PRIVATE PROMPT")[0], 0)
        job = self.records()[0]
        group = ("--group", "pilot")
        self.assertEqual(self.call("resume", job["id"], "Continue")[0], 0)
        self.assertEqual(self.call("handoff", job["id"], "--to", "claude", "Review")[0], 0)
        self.assertEqual(self.call("say", "--to", "codex", "--inbox-only", *group,
                                   "codex-session", "PRIVATE BODY")[0], 0)
        self.assertEqual(self.call("say", "--to", "codex", "--inbox-only", *group,
                                   "*", "Broadcast")[0], 0)
        self.fixture_job("codex", "", job_id="unrelated", group=None)
        snapshot = self.snapshot(*group)
        self.assertEqual(len(snapshot["tasks"]), 3)
        self.assertEqual(len(snapshot["messages"]), 2)
        self.assertNotIn("PRIVATE", json.dumps(snapshot))
        scoped = self.snapshot("--job", job["id"], "--show-messages", "--show-tasks")
        self.assertEqual(scoped["tasks"][0]["name"], "inbox-review")
        self.assertEqual(scoped["tasks"][0]["summary"], "PRIVATE PROMPT")
        self.assertEqual(scoped["tasks"][0]["model"], "gpt-6.1-terra")
        self.assertEqual([row["body"] for row in scoped["messages"]], ["PRIVATE BODY"])
        self.assertTrue(all(row["status"] == "pending" for row in self.snapshot(*group)["messages"]))

    def test_reads_require_success_and_do_not_expose_output(self):
        self.fixture_job("codex", "".join(map(line, [
            command("""/bin/bash -lc 'sed -n "1,4p" "a file.md"'"""),
            command("cat failed.txt", 1),
            command("rg -l pattern src"),
            command("echo cat guessed.txt"),
            command("cat one.txt && cat two.txt"),
            command(r""""C:\Program Files\pwsh.exe" -Command """
                    r'''"Get-Content -LiteralPath 'C:\repo\windows.txt' -Raw"'''),
        ])))
        self.fixture_job("claude", "".join(map(line, [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read", "id": call_id, "input": {"file_path": path}}
                for call_id, path in (("okay", "claude.txt"), ("error", "missing"), ("pending", "unread"))
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "okay", "content": "PRIVATE CLAUDE TEXT"},
                {"type": "tool_result", "tool_use_id": "error", "is_error": True},
            ]}},
        ])))
        self.fixture_job("agy", "".join(map(line, [
            {"event": "step_update", "step_update": {
                "step_type": "tool", "state": state, "tool_info": {
                    "name": "view_file", "parameters": {"AbsolutePath": path},
                    "error": error, "output": "PRIVATE AGY TEXT",
                },
            }}
            for state, path, error in (("ACTIVE", "pending.md", None), ("DONE", "agy.md", None),
                                       ("DONE", "error.md", {"type": "failed"}))
        ])))
        self.fixture_job("acp", "".join(map(line, [
            {"method": "session/update", "params": {"update": {
                "toolCallId": "read-one", **update
            }}}
            for update in (
                {"sessionUpdate": "tool_call", "kind": "read", "locations": [{"path": "acp.txt"}],
                 "status": "pending"},
                {"sessionUpdate": "tool_call_update", "status": "completed"},
            )
        ])))
        self.fixture_job("generic", line({"event": "file_read", "status": "completed", "path": "generic.md"}))
        snapshot = self.snapshot()
        tasks = {task["target"]: task for task in snapshot["tasks"]}
        self.assertEqual([r["path"] for r in tasks["codex"]["files_read"]], ["a file.md", r"C:\repo\windows.txt"])
        self.assertTrue(all(r["evidence"] == "inferred_from_command" for r in tasks["codex"]["files_read"]))
        for target, path in (("claude", "claude.txt"), ("agy", "agy.md"), ("acp", "acp.txt"), ("generic", "generic.md")):
            self.assertEqual([(r["path"], r["evidence"]) for r in tasks[target]["files_read"]],
                             [(path, "provider_reported")])
        self.assertNotIn("PRIVATE", json.dumps(snapshot))

    def test_follow_observes_a_running_job_partial_event_and_completion(self):
        events, record = self.fixture_job("codex", line({"type": "thread.started", "thread_id": "live"}),
                                          status="running")
        env = {**os.environ, "PYTHONPATH": SRC_ROOT}
        feed = self.root / "feed.jsonl"
        with feed.open("wb") as output, subprocess.Popen(
                [sys.executable, "-m", "cx_bridge", "--config", str(self.config),
                 "board", "--job", record["id"], "--follow", "--json"],
                stdout=output, env=env) as process:
            def wait_snapshot(predicate):
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    for row in feed.read_bytes().splitlines(keepends=True):
                        if row.endswith(b"\n"):
                            task = json.loads(row)["tasks"][0]
                            if predicate(task):
                                return task
                    time.sleep(0.05)
                self.fail("Timed out waiting for board update")
            try:
                self.assertEqual(wait_snapshot(lambda task: task["status"] == "running")["session"], "live")
                plan = line({"type": "item.updated", "item": {
                    "type": "todo_list", "items": [{"text": "Check imports", "completed": False}],
                }})
                started = line({"type": "item.started", "item": {
                    "type": "command_execution", "id": "shell-read",
                }})
                completed = command("cat README.md")
                completed["item"]["id"] = "shell-read"
                read = line(completed)
                with events.open("a", encoding="utf-8") as stream:
                    stream.write(plan + started + read[:12])
                working = wait_snapshot(lambda task: bool(task["plan"]))
                self.assertEqual(working["files_read"], [])
                self.assertEqual(working["active_tools"], ["shell command"])
                self.assertEqual(working["event_log"], "available")
                with events.open("a", encoding="utf-8") as stream:
                    stream.write(read[12:])
                self.assertEqual(wait_snapshot(lambda task: bool(task["files_read"]))["active_tools"], [])
                record["status"] = "succeeded"
                temporary = events.parent / "updated.json"
                temporary.write_text(json.dumps(record), encoding="utf-8")
                temporary.replace(events.parent / "job.json")
                self.assertEqual(wait_snapshot(lambda task: task["status"] == "succeeded")["files_read"][0]["path"],
                                 "README.md")
            finally:
                process.terminate()
                process.wait(timeout=5)

    def test_dispatch_receipt_and_failed_transport_are_visible(self):
        task_file = self.work / "JOB.md"
        task_file.write_text("Review only", encoding="utf-8")
        code, out, err = self.call("dispatch", "--to", "claude", "--no-wait", "--group", "pilot",
                                   "thread", str(task_file))
        self.assertEqual(code, 0, err)
        receipt = Path(out.strip().split("receipt: ", 1)[1])
        msg = self.snapshot("--group", "pilot")["messages"][0]
        self.assertEqual(msg["task_file"], str(task_file.resolve()))
        self.assertFalse(msg["receipt_ready"])
        receipt.write_text("PRIVATE RECEIPT BODY", encoding="utf-8")
        snapshot = self.snapshot("--group", "pilot")
        self.assertTrue(snapshot["messages"][0]["receipt_ready"])
        self.assertNotIn("PRIVATE", json.dumps(snapshot))
        with patch.dict(os.environ, {"FAKE_QUEUE_FILE": str(self.work / "missing" / "queue.json")}):
            code, _, _ = self.call("say", "--to", "codex", "thread", "Provider fixture transport fails")
        self.assertEqual(code, 1)
        msg = self.snapshot("--to", "codex")["messages"][0]
        self.assertEqual(msg["status"], "failed")
        self.assertIsNone(msg["delivered_at"])

    def test_mcp_board_is_read_only_and_omits_message_bodies(self):
        from cx_bridge import cli
        self.fixture_job("codex", line(command("cat README.md")))
        self.assertEqual(self.call("say", "--to", "codex", "--inbox-only", "--group", "pilot",
                                   "PRIVATE MESSAGE")[0], 0)
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "bridge_board", "arguments": {"group": "pilot"},
        }}
        output = io.BytesIO()
        self.assertEqual(cli.serve_mcp(cli, self.config, instream=io.BytesIO(line(request).encode()),
                                       outstream=output), 0)
        result = json.loads(output.getvalue())["result"]
        self.assertFalse(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["tasks"][0]["files_read"][0]["path"], "README.md")
        self.assertNotIn("body", payload["messages"][0])
        self.assertEqual(payload["messages"][0]["status"], "pending")
