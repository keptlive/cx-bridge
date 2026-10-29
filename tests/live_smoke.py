"""Opt-in live provider tests. Uses real account usage; never runs in unittest.

Run with --live --directory PATH visible to both providers. All raw outputs
remain in the newly created private test directory, outside the source tree.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from cx_bridge import cli


def tool_types(response):
    """Audit saved protocol events; don't print raw model/tool payloads."""
    kinds = set()
    with (Path(response["directory"]) / "events.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            event = json.loads(line)
            if event.get("type") in ("item.started", "item.completed"):
                kind = event.get("item", {}).get("type")
                if kind in ("command_execution", "mcp_tool_call", "web_search", "file_change"):
                    kinds.add(kind)
            if event.get("type") == "assistant":
                for part in event.get("message", {}).get("content", []):
                    if part.get("type") == "tool_use":
                        kinds.add(part.get("name", "tool_use"))
    return sorted(kinds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=cli.config_path())
    parser.add_argument("--timeout", type=cli.positive, default=240)
    args = parser.parse_args()
    if os.name != "nt":
        os.umask(0o077)
    root = args.directory.expanduser().resolve() / ("live-" + uuid.uuid4().hex[:12])
    root.mkdir(parents=True, mode=0o700)
    tokens = {kind: uuid.uuid4().hex for kind in ("codex", "claude")}
    for kind, token in tokens.items():
        (root / (kind + "-fixture.txt")).write_text(token + "\n", encoding="utf-8")
    record = {"started_at": datetime.now(timezone.utc).isoformat(), "steps": [], "live": True}
    base = [sys.executable, "-m", "cx_bridge", "--config", str(args.config.expanduser().resolve())]

    def invoke(label, argv, expected, session=None):
        started = time.monotonic()
        stdout = root / (label + ".stdout.json")
        stderr = root / (label + ".stderr.log")
        options = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        command = base + argv + ["--timeout", str(args.timeout), "--json"]
        # Prompts have already been supplied as positional arguments; argparse
        # supports options after the required positional prompt.
        with stdout.open("wb") as out, stderr.open("wb") as err:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err, **options)
            try:
                process.wait(timeout=args.timeout + 30)
            except subprocess.TimeoutExpired:
                cli.terminate(process)
        try:
            response = json.loads(stdout.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            response = {}
        job = response.get("job", {})
        passed = process.returncode == 0 and job.get("status") == "succeeded" and response.get("result", "").strip() == expected and bool(job.get("session"))
        tools = tool_types(response) if job.get("status") == "succeeded" else []
        passed = passed and job.get("mode") == "read-only" and job.get("cwd") == str(root)
        # A resume that secretly rereads the file cannot prove conversation memory.
        passed = passed and (bool(tools) if label.endswith("-fresh") else not tools)
        if session:
            passed = passed and job.get("session") == session
        item = {"step": label, "passed": passed, "exit_code": process.returncode,
                "job_id": job.get("id"), "session": job.get("session"),
                "status": job.get("status"), "tool_types": tools,
                "elapsed_seconds": round(time.monotonic() - started, 2)}
        record["steps"].append(item)
        print(json.dumps(item), flush=True)
        return response if passed else None

    def fresh(kind):
        return invoke(kind + "-fresh", ["run", "--to", kind, "-C", str(root),
                      f"This is an authorized connector smoke test. Read {kind}-fixture.txt in the current workspace. "
                      "Remember its full token for later turns. Reply exactly FRESH:<token> using the file contents, "
                      "with no markdown or additional text. Do not modify any file or use unrelated services."], "FRESH:" + tokens[kind])

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {kind: pool.submit(fresh, kind) for kind in tokens}
        originals = {kind: future.result() for kind, future in futures.items()}
    for kind, original in originals.items():
        if original:
            invoke(kind + "-resume", ["resume", original["job"]["id"],
                   "Without opening any files or using tools, recall the token from the earlier turn. "
                   "Reply exactly RESUMED:<token> with no additional text."],
                   "RESUMED:" + tokens[kind], session=original["job"]["session"])
    for source, destination in (("codex", "claude"), ("claude", "codex")):
        original = originals[source]
        if not original:
            continue
        label = source + "-to-" + destination
        handoff = invoke(label, ["handoff", original["job"]["id"], "--to", destination,
                         "This is an authorized connector handoff smoke test. Use only the prior agent output "
                         "provided as reference. Extract and remember its token. Do not read files or use tools. "
                         "Reply exactly HANDOFF:<token> with no markdown or additional text."], "HANDOFF:" + tokens[source])
        if handoff:
            fingerprint = hashlib.sha256(original["result"].encode("utf-8")).hexdigest()
            if (handoff["job"]["session"] == original["job"]["session"]
                or handoff["job"].get("source_job") != original["job"]["id"]
                or handoff["job"].get("source_result_sha256") != fingerprint):
                record["steps"][-1]["passed"] = False
                continue
            invoke(label + "-resume", ["resume", handoff["job"]["id"],
                   "Without tools or opening files, recall the token you received in the previous handoff. "
                   "Reply exactly AGAIN:<token> with no additional text."],
                   "AGAIN:" + tokens[source], session=handoff["job"]["session"])
    record["fixture_files_unchanged"] = all((root / (kind + "-fixture.txt")).read_text(encoding="utf-8").strip() == token
                                            for kind, token in tokens.items())
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    record["passed"] = len(record["steps"]) == 8 and all(step["passed"] for step in record["steps"]) and record["fixture_files_unchanged"]
    cli.write_json(root / "receipt.json", record)
    print(json.dumps({"passed": record["passed"], "receipt": str(root / "receipt.json"), "steps": len(record["steps"])}), flush=True)
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
