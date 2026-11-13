"""Install a built wheel into a clean venv and test its console entry point.

Uses only the deterministic fixture; never starts a model or sends a message.
Usage: python tests/package_smoke.py path/to/cx_agent_bridge-VERSION-py3-none-any.whl
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    wheel = Path(sys.argv[1]).resolve(strict=True)
    fixture = Path(__file__).with_name("fake_agent.py").resolve(strict=True)
    acp_fixture = Path(__file__).with_name("fake_acp_agent.py").resolve(strict=True)
    fw_fixture = Path(__file__).with_name("fake_framework_agent.py").resolve(strict=True)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    with tempfile.TemporaryDirectory(prefix="cx-package-smoke-") as temp:
        root = Path(temp)
        venv = root / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True, env=env)
        bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        executable = bin_dir / ("cx-bridge.exe" if os.name == "nt" else "cx-bridge")
        subprocess.run(
            [str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
            check=True,
            capture_output=True,
            env=env,
            cwd=root,
        )
        version = subprocess.run(
            [str(executable), "--version"], check=True, capture_output=True, text=True, env=env, cwd=root
        ).stdout.strip()
        if version != "0.3.0":
            raise RuntimeError(f"Unexpected installed package version: {version}")
        config = {
            "version": 1,
            "default_target": "codex",
            "jobs_dir": str(root / "jobs"),
            "targets": {
                **{
                    kind: {"kind": kind, "command": [str(python), str(fixture), kind]}
                    for kind in ("codex", "claude", "agy")
                },
                "acp-agent": {"kind": "acp", "command": [str(python), str(acp_fixture)]},
                "langgraph": {"kind": "framework", "command": [str(python), str(fw_fixture)]},
            },
            "workspaces": {"project": {"*": str(root / "shared workspace")}},
        }
        (root / "shared workspace").mkdir()
        for kind in ("codex", "claude", "agy"):
            workspace = root / (kind + " workspace")
            workspace.mkdir()
            config["workspaces"]["project"][kind] = str(workspace)
        file = root / "config.json"
        file.write_text(json.dumps(config), encoding="utf-8")

        def call(*args, expected=0, stdin_text=None):
            response = subprocess.run(
                [str(executable), "--config", str(file), *args],
                cwd=root,
                env=env,
                input=stdin_text,
                capture_output=True,
                encoding="utf-8",
                timeout=20,
            )
            if response.returncode != expected:
                raise RuntimeError(f"Package command failed: {args[0]}, exit {response.returncode}: {response.stderr}")
            return json.loads(response.stdout)

        original = call("run", "--to", "codex", "--workspace", "project", "--json", "Explain the fixture")
        handoff_claude = call("handoff", original["job"]["id"], "--to", "claude", "--json", "Review the result")
        if (
            handoff_claude["job"]["source_job"] != original["job"]["id"]
            or handoff_claude["job"]["cwd"] != config["workspaces"]["project"]["claude"]
        ):
            raise RuntimeError("Handoff lost provenance or workspace mapping")
        handoff_agy = call("handoff", handoff_claude["job"]["id"], "--to", "agy", "--json", "Cross-check in AGY")
        if handoff_agy["job"]["cwd"] != config["workspaces"]["project"]["agy"]:
            raise RuntimeError("AGY handoff lost workspace mapping")
        followup = call("resume", handoff_agy["job"]["id"], "-e", "high", "--json", "Check again")
        if followup["job"]["parent"] != handoff_agy["job"]["id"]:
            raise RuntimeError("Continuation lost its parent")
        call("result", followup["job"]["id"], "--json")
        call("events", followup["job"]["id"], "--tail", "1", "--json")
        if len(call("jobs", "--json")) != 4:
            raise RuntimeError("Unexpected saved job count")

        # Verify ACP and Python framework SDK targets from the installed wheel.
        acp_res = call("run", "--to", "acp-agent", "--workspace", "project", "--json", "ALLOW_READ wheel-acp")
        fw_res = call("handoff", acp_res["job"]["id"], "--to", "langgraph", "--json", "Check ACP output in LangGraph")
        if fw_res["job"]["status"] != "succeeded" or "wheel-acp" not in fw_res["result"]:
            raise RuntimeError("ACP -> Python framework handoff failed")

        # Verify inline message + Claude hook roundtrip via installed console binary.
        call("say", "--to", "claude", "--from", "agy", "--json", "Inline smoke note")
        hook_out = call(
            "hook",
            "claude",
            stdin_text=json.dumps({"session_id": "smoke-s1", "cwd": str(root), "hook_event_name": "PostToolUse"}),
        )
        if "Inline smoke note" not in hook_out["hookSpecificOutput"]["additionalContext"]:
            raise RuntimeError("Hook did not deliver inline smoke note")

        failed = call("run", "--workspace", "project", "--json", "MALFORMED_ITEM", expected=1)
        if failed["job"]["status"] != "failed" or failed["job"]["session"] != "salvaged-session":
            raise RuntimeError("Malformed output was not handled safely")
        print(
            json.dumps(
                {
                    "platform": sys.platform,
                    "package_version": version,
                    "wheel_install": "passed",
                    "console_3way_handoff_resume_hook_failure": "passed",
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
