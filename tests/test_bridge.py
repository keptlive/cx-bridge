import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cx_bridge import cli

FAKE = Path(__file__).with_name("fake_agent.py")


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bridge test ")
        self.root = Path(self.temp.name)
        self.work = self.root / "workspace with spaces"
        self.work.mkdir()
        self.config = self.root / "config.json"
        self.cfg = cli.defaults()
        self.cfg["jobs_dir"] = str(self.root / "private jobs")
        self.cfg["targets"] = {kind: {"kind": kind, "command": [sys.executable, str(FAKE), kind]}
                               for kind in ("codex", "claude")}
        self.save()

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        self.config.write_text(json.dumps(self.cfg), encoding="utf-8")

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--config", str(self.config), *args])
        return code, out.getvalue(), err.getvalue()

    def records(self):
        return [json.loads(p.read_text()) for p in sorted((self.root / "private jobs").glob("*/*/job.json"))]

    def test_both_directions_with_spaces_unicode_and_stdin(self):
        for target in ("claude", "codex"):
            with self.subTest(target=target), patch("sys.stdin", io.StringIO("Hello café\nsecond line")):
                code, out, _ = self.call("run", "--to", target, "-C", str(self.work), "-")
                self.assertEqual(code, 0)
                self.assertIn("café\nsecond line", out)
        self.assertEqual({r["kind"] for r in self.records()}, {"codex", "claude"})
        self.assertTrue(all(r["status"] == "succeeded" and r["mode"] == "read-only" for r in self.records()))

    def test_resume_preserves_target_workspace_mode_and_parent(self):
        for target in ("claude", "codex"):
            self.assertEqual(self.call("run", "--to", target, "-w", "-C", str(self.work), "original")[0], 0)
            original = next(r for r in self.records() if r["target"] == target and not r["parent"])
            self.assertEqual(self.call("resume", original["id"], "follow-up")[0], 0)
            resumed = next(r for r in self.records() if r["parent"] == original["id"])
            self.assertEqual(resumed["cwd"], str(self.work.resolve()))
            self.assertEqual(resumed["mode"], "workspace-write")
            self.assertEqual(resumed["target"], target)
            self.assertEqual(resumed["session"], target + "-session")

    def test_resume_rejects_changed_target_config(self):
        self.call("run", "-C", str(self.work), "original")
        job = self.records()[0]["id"]
        self.cfg["targets"]["codex"]["model"] = "changed"
        self.save()
        self.assertEqual(self.call("resume", job, "follow-up")[0], 1)
        self.assertEqual(len(self.records()), 1)

    def test_provider_reported_failure_overrides_zero_exit(self):
        for target in ("codex", "claude"):
            self.assertNotEqual(self.call("run", "--to", target, "-C", str(self.work), "FAIL")[0], 0)
        self.assertTrue(all(r["status"] == "failed" for r in self.records()))

    def test_permission_denials_fail(self):
        self.assertEqual(self.call("run", "--to", "claude", "-C", str(self.work), "DENY")[0], 1)

    def test_truncated_output_is_not_success(self):
        for target in ("codex", "claude"):
            self.assertEqual(self.call("run", "--to", target, "-C", str(self.work), "NO_TERMINAL")[0], 1)

    def test_invalid_json_has_failed_record(self):
        code, _, err = self.call("run", "-C", str(self.work), "INVALID_JSON")
        self.assertEqual(code, 1)
        self.assertIn("invalid JSONL", err)
        self.assertEqual(self.records()[0]["status"], "failed")

    def test_nonzero_exit_is_preserved(self):
        self.assertEqual(self.call("run", "-C", str(self.work), "NONZERO")[0], 7)
        self.assertEqual(self.records()[0]["exit_code"], 7)

    @unittest.skipIf(os.name == "nt", "POSIX process group assertion")
    def test_timeout_kills_descendants_and_preserves_timeout_with_partial_json(self):
        self.assertEqual(self.call("run", "--timeout", "0.5", "-C", str(self.work), "TIMEOUT")[0], 124)
        self.assertEqual(self.records()[0]["status"], "timed_out")
        pid = int((self.work / "child.pid").read_text())
        stat = Path(f"/proc/{pid}/stat")
        # A killed child can briefly remain a zombie awaiting init's reap.
        self.assertTrue(not stat.exists() or stat.read_text().split()[2] == "Z")

    def test_missing_binary_has_terminal_record(self):
        self.cfg["targets"]["codex"]["command"] = ["cx-nonexistent-fixture"]
        self.save()
        self.assertEqual(self.call("run", "-C", str(self.work), "hello")[0], 1)
        self.assertEqual(self.records()[0]["status"], "failed")

    def test_jobs_have_unique_ids_and_no_command_secrets(self):
        for _ in range(3):
            self.assertEqual(self.call("run", "-C", str(self.work), "hello")[0], 0)
        self.assertEqual(len({r["id"] for r in self.records()}), 3)
        self.assertTrue(all("command" not in r for r in self.records()))
        self.assertEqual(self.call("jobs")[0], 0)
        self.assertEqual(self.call("show", self.records()[0]["id"])[0], 0)

    def test_no_shell_prompt_injection(self):
        sentinel = self.work / "injected"
        prompt = f'hello; touch "{sentinel}"; $(touch "{sentinel}")'
        self.assertEqual(self.call("run", "-C", str(self.work), prompt)[0], 0)
        self.assertFalse(sentinel.exists())

    def test_review_without_focus_is_read_only(self):
        self.assertEqual(self.call("review", "-C", str(self.work))[0], 0)
        self.assertEqual(self.records()[0]["mode"], "read-only")

    def test_desktop_say_and_dispatch_unique_receipts(self):
        queued = self.root / "queue.txt"
        job = self.work / "JOB.md"
        job.write_text("Job fixture")
        (self.work / "DONE.txt").write_text("stale report")
        with patch.dict(os.environ, {"FAKE_QUEUE_FILE": str(queued), "FAKE_RECEIPT": "1"}):
            self.assertEqual(self.call("say", "thread", "authorized fixture message")[0], 0)
            self.assertEqual(queued.read_text(), "authorized fixture message")
            code, out, _ = self.call("dispatch", "thread", str(job), "--timeout", "2")
            self.assertEqual(code, 0)
            self.assertIn("Fixture complete", out)
            self.assertNotIn("stale report", out)
        self.assertEqual((self.work / "DONE.txt").read_text(), "stale report")

    def test_dispatch_timeout_does_not_accept_stale_done(self):
        queued = self.root / "queue.txt"
        job = self.work / "JOB.md"
        job.write_text("Job fixture")
        (self.work / "DONE.txt").write_text("stale report")
        with patch.dict(os.environ, {"FAKE_QUEUE_FILE": str(queued)}):
            code, out, err = self.call("dispatch", "thread", str(job), "--timeout", "0.05")
        self.assertEqual(code, 1)
        self.assertNotIn("stale report", out)
        self.assertIn("may still be running", err)

    def test_doctor_never_runs_inference(self):
        code, out, _ = self.call("doctor")
        self.assertEqual(code, 0)
        self.assertTrue(all(json.loads(line)["healthy"] for line in out.splitlines()))
        self.assertEqual(self.records(), [])

    def test_doctor_accepts_task_cli_without_optional_desktop_queue(self):
        with patch.dict(os.environ, {"FAKE_NO_QUEUE": "1"}):
            code, out, _ = self.call("doctor")
        self.assertEqual(code, 0)
        codex = next(json.loads(line) for line in out.splitlines() if json.loads(line)["kind"] == "codex")
        self.assertFalse(codex["checks"]["queue"])
        self.assertTrue(codex["healthy"])

    @unittest.skipIf(os.name == "nt", "WSL path conversion behavior")
    def test_windows_codex_rejects_linux_unc_workspace_without_mirroring(self):
        target = {"kind": "codex", "command": ["codex.exe"], "path_style": "windows"}
        with patch.object(cli.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "\\\\wsl.localhost\\Ubuntu\\home\\project\n")):
            with self.assertRaisesRegex(cli.BridgeError, "No automatic mirror"):
                cli.native_path(Path("/home/project"), target)
        with patch.object(cli.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "C:\\project with spaces\n")):
            self.assertEqual(cli.native_path(Path("/mnt/c/project with spaces"), target), "C:\\project with spaces")

    def test_init_does_not_overwrite(self):
        before = self.config.read_bytes()
        self.assertEqual(self.call("init")[0], 1)
        self.assertEqual(self.config.read_bytes(), before)

    def test_explicit_missing_config_is_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--config", str(self.root / "absent"), "doctor"]), 1)

    def test_invalid_config(self):
        for update in ({"timeout": -1}, {"timeout": "nan"}, {"targets": []}, {"default_target": "absent"}):
            with self.subTest(update=update):
                self.config.write_text(json.dumps({**self.cfg, **update}))
                self.assertEqual(self.call("doctor")[0], 1)

    def test_session_lock_prevents_simultaneous_resume(self):
        root = self.root / "locks"
        with cli.session_lock(root, "same"):
            with self.assertRaises(cli.BridgeError):
                with cli.session_lock(root, "same"):
                    self.fail("Concurrent lock admitted")
        with cli.session_lock(root, "same"):
            pass

    def test_resume_path_traversal_rejected(self):
        self.assertEqual(self.call("resume", "../other", "follow-up")[0], 1)

    def test_native_cli_permission_arguments_and_resume(self):
        target = {"kind": "codex", "command": ["codex"]}
        cmd = cli.build_command(target, self.work, "read-only", session="session")
        self.assertIn('sandbox_mode="read-only"', cmd)
        self.assertEqual(cmd[cmd.index("resume") + 1], "session")
        target = {"kind": "claude", "command": ["claude"]}
        cmd = cli.build_command(target, self.work, "read-only")
        tools = cmd[cmd.index("--tools") + 1]
        self.assertEqual(tools, "Read,Grep,Glob")
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        cmd = cli.build_command(target, self.work, "workspace-write")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read,Grep,Glob,Edit,Write")

    def test_interrupt_has_terminal_status_and_releases_lock(self):
        with patch.object(cli, "invoke", side_effect=KeyboardInterrupt):
            self.assertEqual(self.call("run", "-C", str(self.work), "hello")[0], 130)
        self.assertEqual(self.records()[0]["status"], "interrupted")
        self.assertEqual(list((self.root / "private jobs/.locks").glob("*.lock")), [])


if __name__ == "__main__":
    unittest.main()
