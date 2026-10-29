import contextlib
import io
import hashlib
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

    def test_malformed_item_fails_cleanly_and_preserves_session(self):
        code, _, err = self.call("run", "-C", str(self.work), "MALFORMED_ITEM")
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)
        self.assertIn("invalid item payload", err)
        job = self.records()[0]
        self.assertEqual(job["session"], "salvaged-session")
        self.assertEqual(job["error"], "invalid_provider_output")

    def test_nonzero_exit_is_preserved(self):
        self.assertEqual(self.call("run", "-C", str(self.work), "NONZERO")[0], 7)
        self.assertEqual(self.records()[0]["exit_code"], 7)

    def test_timeout_kills_descendants_and_preserves_timeout_with_partial_json(self):
        self.assertEqual(self.call("run", "--timeout", "2", "-C", str(self.work), "TIMEOUT")[0], 124)
        self.assertEqual(self.records()[0]["status"], "timed_out")
        self.assertEqual(self.records()[0]["session"], "timeout-session")
        pid = int((self.work / "child.pid").read_text())
        if os.name == "nt":
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            handle = kernel.OpenProcess(0x1000, False, pid)
            if handle:
                exit_code = ctypes.c_ulong()
                self.assertTrue(kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(exit_code)))
                kernel.CloseHandle(ctypes.c_void_p(handle))
                self.assertNotEqual(exit_code.value, 259)  # STILL_ACTIVE
        elif sys.platform.startswith("linux"):
            stat = Path(f"/proc/{pid}/stat")
            # A killed child can briefly remain a zombie awaiting init's reap.
            self.assertTrue(not stat.exists() or stat.read_text().split()[2] == "Z")
        else:
            status = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
            self.assertTrue(not status or status.startswith("Z"))

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

    def test_json_output_and_saved_results_and_event_tails(self):
        code, out, _ = self.call("run", "--json", "-C", str(self.work), "hello café")
        self.assertEqual(code, 0)
        result = json.loads(out)
        job = result["job"]["id"]
        self.assertEqual(result["job"]["status"], "succeeded")
        self.assertIn("café", result["result"])
        code, out, _ = self.call("result", job, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["result"], result["result"])
        code, out, _ = self.call("events", job, "--tail", "1", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["events"], [{"type": "turn.completed"}])
        code, out, _ = self.call("jobs", "--json", "--limit", "1")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)), 1)

    def test_failed_job_has_structured_error_and_result_exit_code(self):
        code, out, _ = self.call("run", "--json", "-C", str(self.work), "FAIL")
        self.assertEqual(code, 1)
        result = json.loads(out)
        self.assertEqual(result["job"]["error"], "provider_incomplete_or_failed")
        self.assertEqual(self.call("result", result["job"]["id"])[0], 1)

    def test_json_validation_errors_are_machine_readable(self):
        code, out, _ = self.call("run", "--to", "missing", "--json", "hello")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], "bridge_error")

    def test_json_launch_failure_still_identifies_job(self):
        self.cfg["targets"]["codex"]["command"] = ["cx-nonexistent-fixture"]
        self.save()
        code, out, _ = self.call("run", "--json", "-C", str(self.work), "hello")
        self.assertEqual(code, 1)
        result = json.loads(out)
        self.assertEqual(result["job"]["status"], "failed")
        self.assertEqual(result["job"]["error"], "FileNotFoundError")
        self.assertTrue(Path(result["directory"]).is_dir())

    def test_handoff_uses_result_provenance_and_defaults_to_read_only(self):
        self.assertEqual(self.call("run", "--to", "codex", "-w", "-C", str(self.work), "source-answer")[0], 0)
        original = self.records()[0]
        file = cli.find_job(self.root / "private jobs", original["id"])
        source_result = (file.parent / "result.md").read_text(encoding="utf-8")
        (file.parent / "runner.log").write_text("private-runner-context")
        code, out, _ = self.call("handoff", original["id"], "--to", "claude", "--json", "Review prior answer")
        self.assertEqual(code, 0)
        job = json.loads(out)["job"]
        self.assertEqual(job["source_job"], original["id"])
        self.assertEqual(job["source_result_sha256"], hashlib.sha256(source_result.encode()).hexdigest())
        self.assertEqual(job["target"], "claude")
        self.assertEqual(job["mode"], "read-only")
        self.assertEqual(job["cwd"], str(self.work.resolve()))
        self.assertIn("source-answer", out)
        self.assertNotIn("private-runner-context", out)
        code, out, _ = self.call("handoff", job["id"], "--to", "codex", "--json", "Reconcile the feedback")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["job"]["source_job"], job["id"])

    def test_handoff_rejects_failed_and_empty_source_results(self):
        self.call("run", "-C", str(self.work), "FAIL")
        job = self.records()[0]
        self.assertEqual(self.call("handoff", job["id"], "--to", "claude", "review")[0], 1)
        self.call("run", "-C", str(self.work), "valid")
        job = next(r for r in self.records() if r["status"] == "succeeded")
        file = cli.find_job(self.root / "private jobs", job["id"])
        (file.parent / "result.md").write_text("")
        self.assertEqual(self.call("handoff", job["id"], "--to", "claude", "review")[0], 1)

    def test_workspace_target_mapping_is_preserved_across_handoff_and_resume(self):
        other = self.root / "claude checkout"
        other.mkdir()
        self.cfg["workspaces"] = {"project": {"codex": str(self.work), "claude": str(other)}}
        self.save()
        code, out, _ = self.call("run", "--workspace", "project", "--json", "source")
        self.assertEqual(code, 0)
        source = json.loads(out)["job"]
        code, out, _ = self.call("handoff", source["id"], "--to", "claude", "--json", "review")
        self.assertEqual(code, 0)
        handoff = json.loads(out)["job"]
        self.assertEqual(handoff["cwd"], str(other.resolve()))
        self.assertEqual(handoff["workspace"], "project")
        self.cfg["workspaces"]["project"]["claude"] = str(self.work)
        self.save()
        code, out, _ = self.call("resume", handoff["id"], "--json", "follow-up")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["job"]["cwd"], str(other.resolve()))

    def test_relative_profile_paths_are_anchored_to_config(self):
        self.cfg["workspaces"] = {"project": {"*": self.work.name}}
        self.cfg["jobs_dir"] = "relative jobs"
        self.save()
        code, out, _ = self.call("run", "--workspace", "project", "--json", "hello")
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result["job"]["cwd"], str(self.work.resolve()))
        self.assertTrue(Path(result["directory"]).is_relative_to(self.root / "relative jobs"))

    def test_workspace_validation_and_missing_mapping(self):
        self.cfg["workspaces"] = {"partial": {"claude": str(self.work)}}
        self.save()
        self.assertEqual(self.call("run", "--workspace", "missing", "hello")[0], 1)
        self.assertEqual(self.call("run", "--workspace", "partial", "hello")[0], 1)
        self.cfg["workspaces"]["invalid"] = {"absent-target": str(self.work)}
        self.save()
        self.assertEqual(self.call("doctor")[0], 1)

    def test_target_environment_applies_without_leaking_config_values(self):
        self.cfg["targets"]["claude"]["env"] = {"BRIDGE_FIXTURE_VALUE": "fixture-environment-value"}
        self.save()
        code, out, _ = self.call("run", "--to", "claude", "-C", str(self.work), "ENV_CHECK")
        self.assertEqual(code, 0)
        self.assertIn("fixture-environment-value", out)
        code, out, _ = self.call("targets", "--json")
        self.assertEqual(code, 0)
        self.assertNotIn("fixture-environment-value", out)
        self.assertNotIn("BRIDGE_FIXTURE_VALUE", out)
        self.assertNotIn("fixture-environment-value", json.dumps(self.records()))

    def test_invalid_target_environment_is_rejected(self):
        for env in ([], {"invalid=name": "value"}, {"VALID": None}, {"VALID": "nul\0value"}):
            with self.subTest(env=env):
                self.cfg["targets"]["claude"]["env"] = env
                self.save()
                self.assertEqual(self.call("doctor")[0], 1)

    def test_profile_listing_and_relative_wrapper_are_config_anchored(self):
        self.cfg["workspaces"] = {"project": {"*": self.work.name}}
        self.cfg["targets"]["claude"]["command"] = ["./wrapper"]
        self.save()
        cfg = cli.load_config(self.config)
        self.assertEqual(cfg["targets"]["claude"]["command"][0], str((self.root / "wrapper").resolve()))
        code, out, _ = self.call("workspaces", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["paths"]["*"], str(self.work.resolve()))

    def test_corrupt_job_records_fail_without_traceback(self):
        self.call("run", "-C", str(self.work), "hello")
        file = cli.find_job(self.root / "private jobs", self.records()[0]["id"])
        file.write_text("[]")
        code, out, err = self.call("resume", file.parent.name, "--json", "continue")
        self.assertEqual(code, 1)
        self.assertIn("Invalid job record", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(json.loads(out)["error"]["code"], "bridge_error")

    def test_events_can_inspect_malformed_provider_output(self):
        self.call("run", "-C", str(self.work), "INVALID_JSON")
        job = self.records()[0]["id"]
        code, out, _ = self.call("events", job, "--json")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["events"][0]["invalid_json"])

    def test_queue_does_not_echo_provider_logs(self):
        queued = self.root / "queue.txt"
        with patch.dict(os.environ, {"FAKE_QUEUE_FILE": str(queued), "FAKE_QUEUE_SECRET": "1"}):
            code, out, err = self.call("say", "thread", "authorized fixture")
        self.assertEqual(code, 0)
        self.assertNotIn("fixture-private", out + err)

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

    def test_doctor_reports_timeout_without_echoing_private_command(self):
        with patch.object(cli.subprocess, "run", side_effect=subprocess.TimeoutExpired(["private-argv-value"], 15)):
            code, out, err = self.call("doctor", "--timeout", "0.1")
        self.assertEqual(code, 1)
        self.assertTrue(all(json.loads(line)["issues"]["version"] == "timeout" for line in out.splitlines()))
        self.assertNotIn("private-argv-value", out + err)

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
