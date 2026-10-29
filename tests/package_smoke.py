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
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    with tempfile.TemporaryDirectory(prefix="cx-package-smoke-") as temp:
        root = Path(temp)
        venv = root / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True, env=env)
        bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        executable = bin_dir / ("cx-bridge.exe" if os.name == "nt" else "cx-bridge")
        subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
                       check=True, capture_output=True, env=env, cwd=root)
        version = subprocess.run([str(executable), "--version"], check=True, capture_output=True,
                                 text=True, env=env, cwd=root).stdout.strip()
        if version != "0.2.0":
            raise RuntimeError("Unexpected installed package version")
        config = {"version": 1, "default_target": "codex", "jobs_dir": str(root / "jobs"),
                  "targets": {kind: {"kind": kind, "command": [str(python), str(fixture), kind]}
                              for kind in ("codex", "claude")}, "workspaces": {"project": {}}}
        for kind in ("codex", "claude"):
            workspace = root / (kind + " workspace")
            workspace.mkdir()
            config["workspaces"]["project"][kind] = str(workspace)
        file = root / "config.json"
        file.write_text(json.dumps(config), encoding="utf-8")

        def call(*args, expected=0):
            response = subprocess.run([str(executable), "--config", str(file), *args], cwd=root, env=env,
                                      capture_output=True, encoding="utf-8", timeout=20)
            if response.returncode != expected:
                raise RuntimeError(f"Package command failed: {args[0]}, exit {response.returncode}")
            return json.loads(response.stdout)

        original = call("run", "--to", "codex", "--workspace", "project", "--json", "Explain the fixture")
        handoff = call("handoff", original["job"]["id"], "--to", "claude", "--json", "Review the result")
        if handoff["job"]["source_job"] != original["job"]["id"] or handoff["job"]["cwd"] != config["workspaces"]["project"]["claude"]:
            raise RuntimeError("Handoff lost provenance or workspace mapping")
        followup = call("resume", handoff["job"]["id"], "-e", "high", "--json", "Check again")
        if followup["job"]["parent"] != handoff["job"]["id"]:
            raise RuntimeError("Continuation lost its parent")
        call("result", followup["job"]["id"], "--json")
        call("events", followup["job"]["id"], "--tail", "1", "--json")
        if len(call("jobs", "--json")) != 3:
            raise RuntimeError("Unexpected saved job count")
        failed = call("run", "--workspace", "project", "--json", "MALFORMED_ITEM", expected=1)
        if failed["job"]["status"] != "failed" or failed["job"]["session"] != "salvaged-session":
            raise RuntimeError("Malformed output was not handled safely")
        print(json.dumps({"platform": sys.platform, "package_version": version, "wheel_install": "passed",
                          "console_run_handoff_resume_inspection_failure": "passed"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
