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
FAKE_ACP = Path(__file__).with_name("fake_acp_agent.py")
FAKE_FRAMEWORK = Path(__file__).with_name("fake_framework_agent.py")
SRC_ROOT = str(Path(cli.__file__).resolve().parents[1])


class BridgeTestCase(unittest.TestCase):
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


class BridgeTests(BridgeTestCase):
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

    def test_three_way_antigravity_run_resume_and_all_six_handoff_directions(self):
        self.cfg["targets"]["agy"] = {"kind": "antigravity", "command": [sys.executable, str(FAKE), "agy"]}
        self.save()
        cfg = cli.load_config(self.config)
        self.assertEqual(cfg["targets"]["agy"]["kind"], "agy")
        cmd_ro = cli.build_command(cfg["targets"]["agy"], self.work, "read-only", session="conv-1")
        self.assertEqual(cmd_ro[cmd_ro.index("--mode") + 1], "plan")
        self.assertIn("--sandbox", cmd_ro)
        self.assertEqual(cmd_ro[cmd_ro.index("--conversation") + 1], "conv-1")
        cmd_rw = cli.build_command(cfg["targets"]["agy"], self.work, "workspace-write")
        self.assertEqual(cmd_rw[cmd_rw.index("--mode") + 1], "accept-edits")

        code, out, _ = self.call("doctor")
        self.assertEqual(code, 0)
        self.assertEqual({json.loads(line)["kind"] for line in out.splitlines()}, {"codex", "claude", "agy"})
        self.assertTrue(all(json.loads(line)["healthy"] for line in out.splitlines()))

        fresh_ids = {}
        for target in ("codex", "claude", "agy"):
            with patch("sys.stdin", io.StringIO(f"seed-{target} café")):
                code, out, _ = self.call("run", "--to", target, "-w", "-C", str(self.work), "--json", "-")
            self.assertEqual(code, 0)
            payload = json.loads(out)
            fresh_ids[target] = payload["job"]["id"]
            self.assertEqual(payload["job"]["session"], f"{target}-session")
            code, r_out, _ = self.call("resume", fresh_ids[target], "--json", f"followup-{target}")
            self.assertEqual(code, 0)
            resumed = json.loads(r_out)["job"]
            self.assertEqual(resumed["parent"], fresh_ids[target])
            self.assertEqual(resumed["session"], f"{target}-session")
            self.assertEqual(resumed["mode"], "workspace-write")

        pairs = [
            ("codex", "claude"),
            ("claude", "codex"),
            ("codex", "agy"),
            ("agy", "codex"),
            ("claude", "agy"),
            ("agy", "claude"),
        ]
        for src, dst in pairs:
            with self.subTest(src=src, dst=dst):
                code, out, _ = self.call("handoff", fresh_ids[src], "--to", dst, "--json", f"check {src}->{dst}")
                self.assertEqual(code, 0)
                job = json.loads(out)["job"]
                self.assertEqual(job["source_job"], fresh_ids[src])
                self.assertEqual(job["target"], dst)
                self.assertEqual(job["mode"], "read-only")
                self.assertIn(f"seed-{src}", out)

    def test_antigravity_failure_denied_actions_malformed_and_timeout_salvage(self):
        self.cfg["targets"]["agy"] = {"kind": "agy", "command": [sys.executable, str(FAKE), "agy"]}
        self.save()
        for prompt in ("FAIL", "DENY", "NO_TERMINAL"):
            with self.subTest(prompt=prompt):
                code, out, _ = self.call("run", "--to", "agy", "--json", "-C", str(self.work), prompt)
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(out)["job"]["status"], "failed")

        code, out, err = self.call("run", "--to", "agy", "--json", "-C", str(self.work), "MALFORMED_ITEM")
        self.assertEqual(code, 1)
        self.assertIn("invalid step_update payload", err)
        malformed = json.loads(out)["job"]
        self.assertEqual(malformed["session"], "salvaged-session")
        self.assertEqual(malformed["error"], "invalid_provider_output")

        code, out, _ = self.call("run", "--to", "agy", "--timeout", "2", "--json", "-C", str(self.work), "TIMEOUT")
        self.assertEqual(code, 124)
        timed = json.loads(out)
        self.assertEqual(timed["job"]["status"], "timed_out")
        self.assertEqual(timed["job"]["session"], "timeout-session")
        self.assertEqual(timed["result"], "partial-agy")

    def test_symmetric_path_styles_wsl_and_windows_antigravity(self):
        self.assertEqual(cli.to_wsl_path(r"C:\Users\Matt\project with spaces"), "/mnt/c/Users/Matt/project with spaces")
        self.assertEqual(
            cli.to_wsl_path(r"\\wsl.localhost\Ubuntu\home\matt\src\cx-bridge", "Ubuntu"),
            "/home/matt/src/cx-bridge",
        )
        with self.assertRaisesRegex(cli.BridgeError, "expects 'Debian'"):
            cli.to_wsl_path(r"\\wsl.localhost\Ubuntu\home\matt\src\cx-bridge", "Debian")
        with self.assertRaisesRegex(cli.BridgeError, "UNC shares"):
            cli.to_wsl_path(r"\\fileserver\share\repo")

        wsl_target = {
            "kind": "claude",
            "command": ["/home/user/.local/bin/claude"],
            "path_style": "wsl",
            "wsl_distro": "Ubuntu",
            "env": {"BRIDGE_FIXTURE_VALUE": "secret"},
        }
        with patch.object(cli.os, "name", "nt"):
            wrapped, popen_cwd = cli.wrap_host_command(
                ["/home/user/.local/bin/claude", "-p"], wsl_target, r"C:\Users\Matt\repo"
            )
            self.assertIsNone(popen_cwd)
            self.assertEqual(
                wrapped[:6],
                ["wsl.exe", "-d", "Ubuntu", "--cd", "/mnt/c/Users/Matt/repo", "--exec"],
            )
            env = cli.provider_env(wsl_target)
            self.assertIn("BRIDGE_FIXTURE_VALUE/u", env["WSLENV"])
            win_agy = {"kind": "agy", "command": ["agy.exe"], "path_style": "windows"}
            with self.assertRaisesRegex(cli.BridgeError, "No automatic mirror"):
                cli.native_path(r"\\wsl.localhost\Ubuntu\home\matt\repo", win_agy)

    def test_codex_bin_dir_and_rotated_hash_executable_resolution(self):
        bin_root = self.root / "Codex" / "bin"
        older = bin_root / "1111111111111111"
        newer = bin_root / "2222222222222222"
        older.mkdir(parents=True)
        newer.mkdir(parents=True)
        old_exe = older / "codex.exe"
        new_exe = newer / "codex.exe"
        old_exe.write_text("old")
        new_exe.write_text("new")
        os.utime(old_exe, (1000, 1000))
        os.utime(new_exe, (2000, 2000))

        via_bindir = {"kind": "codex", "command": ["codex.exe"], "bin_dir": str(bin_root)}
        self.assertEqual(cli.resolve_command(via_bindir)[0], str(new_exe))

        stale_hash = {"kind": "codex", "command": [str(bin_root / "deadbeefcafebabe" / "codex.exe")]}
        self.assertEqual(cli.resolve_command(stale_hash)[0], str(new_exe))

    def test_inline_messaging_to_claude_from_codex_and_agy_with_live_hooks_and_loop_guard(self):
        other_work = self.root / "other workspace"
        other_work.mkdir()
        self.assertEqual(self.call("say", "--to", "claude", "--from", "codex", "First inline note from Codex")[0], 0)
        self.assertEqual(
            self.call("say", "--to", "claude", "--from", "agy", "-C", str(self.work), "Scoped inline note from AGY")[0],
            0,
        )

        # Hook in a different workspace only receives unscoped messages, leaving scoped message pending.
        hook_other = json.dumps(
            {"session_id": "claude-other", "cwd": str(other_work), "hook_event_name": "PostToolUse"}
        ).encode("utf-8")
        with patch("sys.stdin", io.TextIOWrapper(io.BytesIO(hook_other), encoding="utf-8")):
            code, out, _ = self.call("hook", "claude")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn("First inline note from Codex", payload["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("Scoped inline note from AGY", payload["hookSpecificOutput"]["additionalContext"])

        # Hook in matching workspace claims the workspace-scoped AGY message once and records heartbeat.
        hook_main = json.dumps(
            {"session_id": "claude-live-1", "cwd": str(self.work), "hook_event_name": "UserPromptSubmit"}
        ).encode("utf-8")
        with patch("sys.stdin", io.TextIOWrapper(io.BytesIO(hook_main), encoding="utf-8")):
            code, out, _ = self.call("hook", "claude")
        self.assertEqual(code, 0)
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Scoped inline note from AGY", ctx)
        self.assertIn("Untrusted peer message via cx-bridge", ctx)

        # Re-running hook does not re-deliver already claimed messages.
        with patch("sys.stdin", io.TextIOWrapper(io.BytesIO(hook_main), encoding="utf-8")):
            code, out, _ = self.call("hook", "claude")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

        # Active Claude session is discoverable via `threads --to claude`.
        code, t_out, _ = self.call("threads", "--to", "claude", "--json")
        self.assertEqual(code, 0)
        thread_ids = {row["id"] for row in json.loads(t_out)}
        self.assertIn("claude-live-1", thread_ids)
        self.assertIn("claude-bg-session", thread_ids)

        # Stop hook blocks once to wake idle-bound Claude, then refuses runaway stop_hook_active chains.
        self.assertEqual(
            self.call("say", "--to", "claude", "--from", "codex", "claude-live-1", "Wake before stopping")[0], 0
        )
        stop_first = json.dumps(
            {"session_id": "claude-live-1", "cwd": str(self.work), "hook_event_name": "Stop", "stop_hook_active": False}
        ).encode("utf-8")
        with patch("sys.stdin", io.TextIOWrapper(io.BytesIO(stop_first), encoding="utf-8")):
            code, out, _ = self.call("hook", "claude")
        self.assertEqual(code, 0)
        stop_resp = json.loads(out)
        self.assertEqual(stop_resp["decision"], "block")
        self.assertIn("Wake before stopping", stop_resp["reason"])

        self.assertEqual(
            self.call("say", "--to", "claude", "--from", "agy", "claude-live-1", "Second rapid ping")[0], 0
        )
        stop_chained = json.dumps(
            {"session_id": "claude-live-1", "cwd": str(self.work), "hook_event_name": "Stop", "stop_hook_active": True}
        ).encode("utf-8")
        with patch("sys.stdin", io.TextIOWrapper(io.BytesIO(stop_chained), encoding="utf-8")):
            code, out, _ = self.call("hook", "claude")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        # Remains safely pending for next normal prompt/claim.
        code, inb_out, _ = self.call("inbox", "--to", "claude", "--claim", "--json")
        self.assertEqual(code, 0)
        claimed = json.loads(inb_out)
        self.assertEqual(len(claimed), 1)
        self.assertIn("Second rapid ping", claimed[0]["body"])

    def test_say_wake_and_antigravity_ide_chat_and_multi_target_dispatch(self):
        chat_capture = self.root / "agy_chat.txt"
        self.cfg["targets"]["agy"] = {
            "kind": "agy",
            "command": [sys.executable, str(FAKE), "agy"],
            "chat_command": [sys.executable, str(FAKE), "agy"],
        }
        self.save()

        # Synchronous inline wake on Claude and AGY external session IDs.
        for target, ext_sid in (("claude", "ext-claude-42"), ("agy", "ext-agy-99")):
            code, out, _ = self.call(
                "say", "--to", target, "--from", "codex", "--wake", "-C", str(self.work), "--json", ext_sid, "Ping inline"
            )
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(payload["job"]["session"], ext_sid)
            self.assertIn("Ping inline", payload["result"])

        # Antigravity IDE live window delivery via stdin (ask by default, agent with -w).
        with patch.dict(os.environ, {"FAKE_CHAT_FILE": str(chat_capture), "FAKE_QUEUE_SECRET": "1"}):
            code, out, err = self.call("say", "--to", "agy", "--from", "claude", "ide", "Check open editor")
            self.assertEqual(code, 0)
            self.assertNotIn("fixture-private", out + err)
            self.assertTrue(chat_capture.read_text(encoding="utf-8").startswith("ask:"))
            self.assertIn("Check open editor", chat_capture.read_text(encoding="utf-8"))

        # Multi-target dispatch with unique DONE.<uuid>.txt receipts for AGY IDE and Claude wake.
        job_file = self.work / "ARGON-JOB.md"
        job_file.write_text("Scoped job")
        (self.work / "DONE.txt").write_text("stale")
        with patch.dict(os.environ, {"FAKE_CHAT_FILE": str(chat_capture), "FAKE_RECEIPT": "1"}):
            code, out, _ = self.call("dispatch", "--to", "agy", "ide", str(job_file), "--timeout", "2")
            self.assertEqual(code, 0)
            self.assertIn("AGY IDE fixture complete", out)
            code, out, _ = self.call("dispatch", "--to", "claude", "--wake", "active", str(job_file), "--timeout", "2")
            self.assertEqual(code, 0)
            self.assertIn("claude wake fixture complete", out)
        self.assertEqual((self.work / "DONE.txt").read_text(), "stale")

    def test_inbox_cli_claim_ack_and_input_bounds(self):
        code, out, _ = self.call("say", "--to", "codex", "--inbox-only", "--from", "claude", "--json", "t-1", "Queued note")
        self.assertEqual(code, 0)
        msg_id = json.loads(out)["id"]
        code, listed, _ = self.call("inbox", "--to", "codex", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(listed)), 1)
        code, claimed, _ = self.call("inbox", "--to", "codex", "--claim", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(claimed)[0]["status"], "delivered")
        self.assertEqual(self.call("inbox", "ack", msg_id[:16])[0], 0)
        code, all_msgs, _ = self.call("inbox", "--to", "codex", "--all", "--json")
        self.assertEqual(json.loads(all_msgs)[0]["status"], "acked")

        # Reject flag-like thread names (even after `--`) and oversized messages.
        self.assertEqual(self.call("say", "--to", "claude", "--", "-bad-thread", "hello")[0], 1)
        self.assertEqual(self.call("say", "--to", "claude", "ok-thread", "x" * (65 * 1024))[0], 1)

    def test_mcp_stdio_server_ndjson_and_content_length_and_write_guard(self):
        self.cfg["targets"]["agy"] = {"kind": "agy", "command": [sys.executable, str(FAKE), "agy"]}
        self.save()
        # Seed a workspace-write job outside MCP to verify read-only MCP cannot resume it.
        code, seed_out, _ = self.call("run", "--to", "claude", "-w", "-C", str(self.work), "--json", "seed-write-job")
        self.assertEqual(code, 0)
        write_job_id = json.loads(seed_out)["job"]["id"]

        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "bridge_say",
                    "arguments": {"target": "claude", "sender": "spoofed-claude", "message": "Hello from AGY MCP"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "bridge_inbox", "arguments": {"action": "claim", "target": "claude"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "bridge_run", "arguments": {"target": "codex", "cwd": str(self.work), "prompt": "mcp-task"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "bridge_run", "arguments": {"target": "codex", "cwd": str(self.work), "write": True, "prompt": "blocked"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "bridge_resume", "arguments": {"job": write_job_id, "prompt": "blocked-write-resume"}},
            },
        ]
        ndjson_in = io.BytesIO("".join(json.dumps(r) + "\n" for r in requests).encode("utf-8"))
        ndjson_out = io.BytesIO()
        self.assertEqual(
            cli.serve_mcp(cli, self.config, allow_write=False, identity="agy", instream=ndjson_in, outstream=ndjson_out),
            0,
        )
        responses = {json.loads(line)["id"]: json.loads(line) for line in ndjson_out.getvalue().decode("utf-8").splitlines()}
        self.assertEqual(responses[1]["result"]["protocolVersion"], "2025-03-26")
        tool_names = {t["name"] for t in responses[2]["result"]["tools"]}
        self.assertIn("bridge_say", tool_names)
        self.assertIn("bridge_inbox", tool_names)
        say_res = json.loads(responses[3]["result"]["content"][0]["text"])
        self.assertEqual(say_res["sender"], "agy")  # Server --identity overrides spoofed sender
        claimed_res = json.loads(responses[4]["result"]["content"][0]["text"])
        self.assertEqual(claimed_res["count"], 1)
        self.assertIn('"from": "agy"', claimed_res["messages"][0]["framed"])
        run_res = json.loads(responses[5]["result"]["content"][0]["text"])
        self.assertEqual(run_res["job"]["status"], "succeeded")
        self.assertTrue(responses[6]["result"]["isError"])
        self.assertTrue(responses[7]["result"]["isError"])

        # Also verify Content-Length framing roundtrip.
        ping_body = json.dumps({"jsonrpc": "2.0", "id": 99, "method": "ping"}).encode("utf-8")
        cl_in = io.BytesIO(f"Content-Length: {len(ping_body)}\r\n\r\n".encode("ascii") + ping_body)
        cl_out = io.BytesIO()
        self.assertEqual(cli.serve_mcp(cli, self.config, instream=cl_in, outstream=cl_out), 0)
        raw_cl = cl_out.getvalue()
        self.assertTrue(raw_cl.startswith(b"Content-Length:"))
        self.assertEqual(json.loads(raw_cl.split(b"\r\n\r\n", 1)[1].decode("utf-8"))["id"], 99)

    def test_acp_connector_run_resume_handoff_permissions_and_timeout(self):
        self.cfg["targets"]["opencode-acp"] = {"kind": "acp", "command": [sys.executable, str(FAKE_ACP)]}
        self.cfg["targets"]["agy"] = {"kind": "agy", "command": [sys.executable, str(FAKE), "agy"]}
        self.save()
        self.assertEqual(self.call("doctor")[0], 0)

        # Read-only allows `read` permission requests, rejects `edit` and `execute`.
        code, out, _ = self.call("run", "--to", "opencode-acp", "-m", "gemini-flash", "-C", str(self.work), "--json", "ALLOW_READ ok")
        self.assertEqual(code, 0)
        fresh = json.loads(out)
        self.assertEqual(fresh["job"]["session"], "acp-fresh-session")
        self.assertIn("[gemini-flash] acp-answer", fresh["result"])

        self.assertEqual(self.call("run", "--to", "opencode-acp", "-C", str(self.work), "REQ_EDIT denied")[0], 1)
        self.assertEqual(self.call("run", "--to", "opencode-acp", "-C", str(self.work), "REQ_EXEC denied")[0], 1)

        # Workspace-write (-w) approves `edit`, while still denying `execute`.
        code, rw_out, _ = self.call("run", "--to", "opencode-acp", "-w", "-C", str(self.work), "--json", "REQ_EDIT allowed")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(rw_out)["job"]["mode"], "workspace-write")
        self.assertEqual(self.call("run", "--to", "opencode-acp", "-w", "-C", str(self.work), "REQ_EXEC still-denied")[0], 1)
        self.assertEqual(self.call("run", "--to", "opencode-acp", "-C", str(self.work), "ACP_ERROR")[0], 1)

        # Resume (`session/load`) and bidirectional handoffs with Claude and Antigravity.
        code, res_out, _ = self.call("resume", fresh["job"]["id"], "--json", "continue-acp")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(res_out)["job"]["session"], "acp-fresh-session")
        code, ho_out, _ = self.call("handoff", fresh["job"]["id"], "--to", "agy", "--json", "verify acp")
        self.assertEqual(code, 0)
        self.assertIn("ALLOW_READ ok", ho_out)

        # Timeout kills ACP child tree and salvages sessionId + partial streamed chunk.
        code, t_out, _ = self.call("run", "--to", "opencode-acp", "--timeout", "2", "-C", str(self.work), "--json", "TIMEOUT")
        self.assertEqual(code, 124)
        timed = json.loads(t_out)
        self.assertEqual(timed["job"]["status"], "timed_out")
        self.assertEqual(timed["job"]["session"], "acp-fresh-session")
        self.assertEqual(timed["result"], "partial-acp-chunk")

    def test_python_framework_target_and_sdk_bridge_connector_and_tools(self):
        from cx_bridge import BridgeConnector

        self.cfg["targets"]["agy"] = {"kind": "agy", "command": [sys.executable, str(FAKE), "agy"]}
        self.cfg["targets"]["langgraph"] = {
            "kind": "framework",
            "command": [sys.executable, str(FAKE_FRAMEWORK)],
            "env": {"PYTHONPATH": SRC_ROOT},
        }
        self.cfg["targets"]["langgraph-rw"] = {
            "kind": "framework",
            "command": [sys.executable, str(FAKE_FRAMEWORK), "--writable"],
            "env": {"PYTHONPATH": SRC_ROOT},
        }
        self.save()
        self.assertEqual(self.call("doctor")[0], 0)

        # Read-only run isolates noisy framework stdout banner into runner.log.
        code, out, err = self.call("run", "--to", "langgraph", "-C", str(self.work), "--json", "graph-task")
        self.assertEqual(code, 0)
        res = json.loads(out)
        self.assertEqual(res["result"], "framework(read-only):graph-task")
        self.assertNotIn("NOISY_FRAMEWORK_STDOUT", out + err)
        self.assertEqual(res["job"]["session"], "langgraph-session-1")

        # supports_write=False blocks -w, whereas supports_write=True allows -w; handler exceptions never leak details.
        self.assertEqual(self.call("run", "--to", "langgraph", "-w", "-C", str(self.work), "blocked-write")[0], 1)
        self.assertEqual(self.call("run", "--to", "langgraph-rw", "-w", "-C", str(self.work), "allowed-write")[0], 0)
        code, fail_out, fail_err = self.call("run", "--to", "langgraph", "-C", str(self.work), "--json", "RAISE_ERROR")
        self.assertEqual(code, 1)
        self.assertNotIn("private-framework-exception-detail", fail_out + fail_err)

        # In-process BridgeConnector + .as_tools() across LangGraph, Claude, Codex, and Antigravity.
        conn = BridgeConnector(self.config, identity="langgraph")
        self.assertTrue(any(t["name"] == "langgraph" for t in conn.targets()))
        with self.assertRaises(cli.BridgeError):
            conn.run("denied-write", to="codex", cwd=str(self.work), write=True)

        tools = {fn.__name__: fn for fn in conn.as_tools()}
        self.assertEqual(
            set(tools.keys()),
            {"bridge_run", "bridge_resume", "bridge_handoff", "bridge_say", "bridge_inbox"},
        )
        agy_run = json.loads(tools["bridge_run"]("check from langgraph", target="agy", cwd=str(self.work)))
        self.assertEqual(agy_run["job"]["status"], "succeeded")
        ho = json.loads(tools["bridge_handoff"](agy_run["job"]["id"], "claude", "claude review"))
        self.assertEqual(ho["job"]["target"], "claude")
        resumed = json.loads(tools["bridge_resume"](ho["job"]["id"], "claude follow-up"))
        self.assertEqual(resumed["job"]["parent"], ho["job"]["id"])

        # Bidirectional inline messaging between Claude/AGY and LangGraph.
        say_to_claude = json.loads(tools["bridge_say"]("claude", "Inline ping from LangGraph"))
        self.assertEqual(say_to_claude["sender"], "langgraph")
        self.assertEqual(self.call("say", "--to", "langgraph", "--from", "agy", "Reply from AGY to LangGraph")[0], 0)
        claimed = json.loads(tools["bridge_inbox"](claim=True))
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["sender"], "agy")
        self.assertEqual(conn.ack(claimed[0]["id"])["status"], "acked")

    def test_declarative_generic_json_and_text_cli_and_hooks_and_flag_guards(self):
        say_capture = self.root / "custom_say.json"
        self.cfg["targets"]["gemini-cli"] = {
            "kind": "generic",
            "command": [sys.executable, str(FAKE), "generic_json"],
            "input_format": "text",
            "output_format": "json",
            "cwd_args": ["--cwd", "{cwd}"],
            "read_only_args": ["--approval", "plan"],
            "model_args": ["--model", "{model}"],
            "resume_args": ["--resume", "{session}"],
            "say_command": [sys.executable, str(FAKE), "custom_say", "--thread", "{thread}"],
        }
        self.cfg["targets"]["plain-cli"] = {
            "kind": "custom",
            "command": [sys.executable, str(FAKE), "generic_text"],
            "input_format": "text",
            "output_format": "text",
        }
        self.save()

        # JSON CLI run + resume + fail-closed -w when write_args is omitted.
        code, out, _ = self.call("run", "--to", "gemini-cli", "-m", "pro", "-C", str(self.work), "--json", "json-hello")
        self.assertEqual(code, 0)
        jres = json.loads(out)
        self.assertEqual(jres["job"]["session"], "generic-json-session")
        self.assertIn("--approval|plan|--model|pro", jres["result"])
        self.assertEqual(self.call("resume", jres["job"]["id"], "json-followup")[0], 0)
        self.assertEqual(self.call("run", "--to", "gemini-cli", "-w", "-C", str(self.work), "no-write-args")[0], 1)

        # Plain-text CLI run + handoff to Claude, and nonzero exit failure.
        code, tout, _ = self.call("run", "--to", "plain-cli", "-C", str(self.work), "--json", "text-hello")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(tout)["result"], "generic-text:text-hello")
        self.assertEqual(self.call("run", "--to", "plain-cli", "-C", str(self.work), "FAIL")[0], 3)

        # Custom say_command delivery over stdin.
        with patch.dict(os.environ, {"FAKE_CUSTOM_SAY_FILE": str(say_capture)}):
            self.assertEqual(self.call("say", "--to", "gemini-cli", "--from", "codex", "chan-7", "Hello custom CLI")[0], 0)
        captured = json.loads(say_capture.read_text(encoding="utf-8"))
        self.assertEqual(captured["args"], ["--thread", "chan-7"])
        self.assertIn("Hello custom CLI", captured["msg"])

        # Argument-injection guard on -m and -e across targets.
        self.assertEqual(self.call("run", "--to", "codex", "--model=-injected", "-C", str(self.work), "hi")[0], 1)
        self.assertEqual(self.call("run", "--to", "codex", "--effort=-injected", "-C", str(self.work), "hi")[0], 1)

        # Gemini hook (`BeforeAgent` + `AfterAgent` loop guard) and `hook generic`.
        self.assertEqual(self.call("say", "--to", "gemini-cli", "--inbox-only", "--from", "claude", "Gemini inline 1")[0], 0)
        gem_in = json.dumps({"session_id": "gem-1", "cwd": str(self.work), "hook_event_name": "BeforeAgent"}).encode("utf-8")
        with patch("sys.stdin", io.TextIOWrapper(io.BytesIO(gem_in), encoding="utf-8")):
            code, gout, _ = self.call("hook", "gemini", "--to", "gemini-cli")
        self.assertEqual(code, 0)
        self.assertIn("Gemini inline 1", json.loads(gout)["hookSpecificOutput"]["additionalContext"])

        self.assertEqual(self.call("say", "--to", "plain-cli", "--from", "agy", "Generic hook msg")[0], 0)
        with patch("sys.stdin", io.TextIOWrapper(io.BytesIO(b""), encoding="utf-8")):
            code, gen_out, _ = self.call("hook", "generic", "--to", "plain-cli")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(gen_out)["count"], 1)

    def test_cross_os_inbox_cwd_matching(self):
        from cx_bridge.cli import to_wsl_path
        from cx_bridge.inbox import _cwd_matches

        self.assertEqual(
            to_wsl_path(r"\\?\UNC\wsl.localhost\Ubuntu\home\matt\src\cx-bridge", "Ubuntu"),
            "/home/matt/src/cx-bridge",
        )
        self.assertEqual(to_wsl_path(r"\\?\C:\Users\Matt\projects", "Ubuntu"), "/mnt/c/Users/Matt/projects")
        self.assertTrue(_cwd_matches(r"C:\Users\Matt\projects\repo", "/mnt/c/Users/Matt/projects/repo", True))
        self.assertTrue(_cwd_matches(r"C:\Users\Matt\projects\repo", "/mnt/c/Users/Matt/projects/repo/sub", True))
        self.assertFalse(_cwd_matches(r"C:\Users\Matt\projects\repo", "/mnt/c/Users/Matt/projects/repo-other", True))
        with patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}):
            self.assertTrue(
                _cwd_matches(
                    r"\\wsl.localhost\Ubuntu\home\matt\src\cx-bridge",
                    "/home/matt/src/cx-bridge",
                    True,
                )
            )
            if os.name != "nt":
                self.assertFalse(
                    _cwd_matches(
                        r"\\wsl.localhost\Debian\home\matt\src\cx-bridge",
                        "/home/matt/src/cx-bridge",
                        True,
                    )
                )


if __name__ == "__main__":
    unittest.main()
