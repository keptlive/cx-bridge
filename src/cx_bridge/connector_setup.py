"""Discover provider connections and prepare project-scoped shared agents."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit

from . import __version__
from .inbox import BridgeError, validate_identifier

# Known public endpoints. Claude-managed proxy URLs are never exported.
SERVICES = {
    "github": ("GitHub", "https://api.githubcopilot.com/mcp/readonly", ("github", "github-mcp-server")),
    "linear": ("Linear", "https://mcp.linear.app/mcp/readonly", ("linear", "linear-server")),
    "openai-docs": ("OpenAI Docs", "https://developers.openai.com/mcp", ("openai-docs", "openaideveloperdocs")),
    "google-drive": ("Google Drive", None, ("google-drive", "gdrive")),
    "google-calendar": ("Google Calendar", None, ("google-calendar",)),
    "gmail": ("Gmail", None, ("gmail",)),
}
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
CLAUDE_ROW = re.compile(r"^(.+?): (.*) - (✔ Connected|! Needs authentication|✘ Failed to connect|"
                        r"⏸ Pending approval|⏸ Rejected|Rejected|Disabled)(?:$| [—-])")
MAX_PACKAGE = 96 * 1024


def slug(value):
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def service_id(name, url=None):
    key = slug(name.removeprefix("claude.ai "))
    try:
        host = urlsplit(url).hostname if url else None
    except ValueError:
        host = None
    for service, (label, endpoint, aliases) in SERVICES.items():
        if key in (*aliases, service, slug(label)) or (endpoint and host == urlsplit(endpoint).hostname):
            return service
    return key



class CodexDiscovery:
    """Read the CLI's JSON-RPC inventory without launching an agent turn."""

    def __init__(self, command, cwd, env, stop):
        self.command, self.cwd, self.env, self.stop = command, cwd, env, stop
        self.responses = Queue()
        self.counter = 0

    def __enter__(self):
        options = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
        }
        self.process = subprocess.Popen(
            self.command, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **options,
        )
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()
        try:
            self.request("initialize", {"clientInfo": {"name": "cx-bridge", "version": __version__}})
            self.send({"method": "initialized"})
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        if self.process.poll() is None:
            self.stop(self.process)
        self.process.wait(timeout=5)
        self.process.stdin.close()
        self.reader.join(timeout=5)
        self.process.stdout.close()

    def read(self):
        try:
            for line in self.process.stdout:
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(row, dict) and "id" in row:
                    self.responses.put(row)
        finally:
            self.responses.put(None)

    def send(self, payload):
        self.process.stdin.write(json.dumps(payload).encode() + b"\n")
        self.process.stdin.flush()

    def request(self, method, params):
        self.counter += 1
        self.send({"id": self.counter, "method": method, "params": params})
        deadline = time.monotonic() + 15
        while True:
            try:
                response = self.responses.get(timeout=max(0, deadline - time.monotonic()))
            except Empty as exc:
                raise BridgeError("Codex connector scan timed out") from exc
            if response is None:
                raise BridgeError("Codex closed its connector scan")
            if response.get("id") == self.counter:
                return response.get("result")


def get_codex(cli, target, cwd):
    base = cli.resolve_command(target)
    env = cli.provider_env(target)
    config_cmd, process_cwd = cli.wrap_host_command(base + ["mcp", "list", "--json"], target, cwd)
    configured = subprocess.run(config_cmd, cwd=process_cwd, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=18, check=True)
    entries = {row["name"]: row for row in json.loads(configured.stdout)}
    cmd, process_cwd = cli.wrap_host_command(base + ["app-server"], target, cwd)
    rows = []
    incomplete = False
    try:
        with CodexDiscovery(cmd, process_cwd, env, cli.terminate) as rpc:
            apps = rpc.request("app/installed", {})
            if not isinstance(apps, dict):
                incomplete = True
            for app in (apps or {}).get("apps", []):
                name = app.get("runtimeName")
                if isinstance(name, str) and name:
                    rows.append({"name": name, "service": service_id(name), "source": "Codex app",
                                 "status": "connected" if app.get("callable") else "unavailable"})
            cursor = None
            seen_cursors = set()
            while True:
                page = rpc.request("mcpServerStatus/list", {"detail": "toolsAndAuthOnly", "cursor": cursor})
                if not isinstance(page, dict):
                    incomplete = True
                    break
                for row in page.get("data", []):
                    name = row["name"]
                    entry = entries.pop(name, {})
                    url = (entry.get("transport") or {}).get("url")
                    status = "unavailable"
                    if row.get("authStatus") == "notLoggedIn":
                        status = "sign_in"
                    elif row.get("serverCapabilities") is not None and not row.get("toolsError"):
                        status = "connected" if row.get("tools") else "unavailable"
                    rows.append({"name": name, "service": service_id(name, url), "source": "MCP",
                                 "status": status})
                next_cursor = page.get("nextCursor")
                if not next_cursor:
                    break
                if next_cursor in seen_cursors:
                    incomplete = True
                    break
                seen_cursors.add(next_cursor)
                cursor = next_cursor
    except (BridgeError, OSError, subprocess.SubprocessError):
        incomplete = True
    for name, row in entries.items():
        url = (row.get("transport") or {}).get("url")
        status = "disabled" if row.get("enabled") is False else "unverified"
        if status == "unverified" and row.get("auth_status") in ("not_logged_in", "notLoggedIn"):
            status = "sign_in"
        rows.append({"name": name, "service": service_id(name, url), "source": "MCP", "status": status})
    return rows, incomplete


def get_claude(cli, target, cwd):
    cmd, process_cwd = cli.wrap_host_command(cli.resolve_command(target) + ["mcp", "list"], target, cwd)
    result = subprocess.run(cmd, cwd=process_cwd, env=cli.provider_env(target), stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=25, check=True)
    states = {"✔ Connected": "connected", "! Needs authentication": "sign_in",
              "✘ Failed to connect": "unavailable", "⏸ Pending approval": "approval",
              "⏸ Rejected": "disabled", "Rejected": "disabled", "Disabled": "disabled"}
    rows = []
    output = result.stdout.decode("utf-8", errors="replace")
    for line in output.splitlines():
        match = CLAUDE_ROW.match(line)
        if match:
            name, endpoint, state = match.groups()
            hosted = name.startswith("claude.ai ")
            if SAFE_NAME.fullmatch(name) or (hosted and re.fullmatch(r"claude\.ai [\w .()&-]{1,80}", name)):
                url = endpoint if endpoint.startswith(("http://", "https://")) else None
                rows.append({"name": name, "service": service_id(name, url), "source": "Claude connector" if hosted else "MCP",
                             "status": states[state]})
    return rows, not rows and "No MCP servers configured" not in output


def scan(cli, cfg, workspaces):
    def scan_target(name, target):
        provider = target["kind"]
        cwd = workspaces.get(name)
        rows, incomplete = [], False
        try:
            if cwd is None:
                raise BridgeError("Workspace unavailable")
            rows, incomplete = (get_codex if provider == "codex" else get_claude)(cli, target, cwd)
        except (BridgeError, OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            incomplete = True
        style = target.get("path_style", "native")
        terminal = "Windows" if style == "windows" or os.name == "nt" else "local"
        if style == "wsl":
            terminal = target.get("wsl_distro") or "WSL"
        return {"target": name, "provider": provider, "terminal": terminal,
                "incomplete": incomplete, "connectors": rows}

    targets = [(name, target) for name, target in cfg["targets"].items() if target["kind"] in ("codex", "claude")]
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(targets)))) as pool:
        futures = [pool.submit(scan_target, name, target) for name, target in targets]
        return [future.result() for future in futures]


def setup_steps(report, requested=()):
    service_ids = set(requested)
    if not requested:
        service_ids.update(SERVICES)
        service_ids.update(row["service"] for profile in report for row in profile["connectors"])
    results = []
    for service in sorted(service_ids):
        label, url, _ = SERVICES.get(service, (service, None, ()))
        for profile in report:
            matches = [row for row in profile["connectors"] if row["service"] == service]
            connected = [row for row in matches if row["status"] == "connected"]
            actionable = [row for row in matches if row["status"] in ("sign_in", "approval")]
            row = (connected or actionable or matches or [None])[0]
            provider = profile["provider"]
            state = row["status"] if row else ("unverified" if profile["incomplete"] else "missing")
            name = row["name"] if row else service
            client = "Codex" if provider == "codex" else "Claude Code"
            terminal = profile["terminal"]
            command_detail = (f"Run in a {terminal} terminal for {client}, then scan again." if terminal != "local"
                              else f"Run in a terminal for {client}, then scan again.")
            command = None
            if state == "sign_in" and SAFE_NAME.fullmatch(name):
                exe = "codex" if provider == "codex" else "claude"
                command = f"{exe} mcp login {name}"
                detail = command_detail
            elif state == "missing" and url and SAFE_NAME.fullmatch(service):
                command = (f"codex mcp add {service} --url {url}" if provider == "codex"
                           else f"claude mcp add --transport http --scope user {service} {url}")
                detail = command_detail
            elif state in ("approval", "sign_in", "disabled", "unavailable", "unverified") or not row:
                command = "/apps" if provider == "codex" and (not row or row["source"] == "Codex app") else "/mcp"
                detail = f"Enter this in the {client} composer to check access, then scan again."
            else:
                detail = f"Using {name} in {client}."
            results.append({"service": service, "label": label, "target": profile["target"], "provider": provider,
                            "status": state, "name": name, "source": row["source"] if row else None,
                            "command": command, "detail": detail})
    return results


def read_package(raw):
    if not isinstance(raw, str) or len(raw.encode("utf-8", errors="surrogatepass")) > MAX_PACKAGE:
        raise BridgeError("Choose a shared agent JSON file under 96 KiB")
    try:
        package = json.loads(raw)
    except ValueError as exc:
        raise BridgeError("Shared agent must be valid JSON") from exc
    keys = {"version", "name", "description", "instructions", "connectors"}
    if not isinstance(package, dict) or set(package) != keys or type(package.get("version")) is not int or package["version"] != 1:
        raise BridgeError("Shared agent needs version, name, description, instructions, and connectors")
    if not isinstance(package["name"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", package["name"]):
        raise BridgeError("Agent name must begin with a lowercase letter and contain only lowercase letters, numbers, or hyphens")
    for key, limit in (("description", 512), ("instructions", 64 * 1024)):
        value = package[key]
        if not isinstance(value, str) or not value.strip() or len(value) > limit or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]", value):
            raise BridgeError(f"Invalid agent {key}")
    connectors = package["connectors"]
    if not isinstance(connectors, list) or len(connectors) > 20:
        raise BridgeError("Connectors must be an array of up to 20 connector IDs")
    for name in connectors:
        validate_identifier(name, "connector ID")
        if name != slug(name):
            raise BridgeError("Connector IDs use lowercase letters, numbers, and hyphens")
    package["connectors"] = list(dict.fromkeys(connectors))
    return package


def install_agent(package, profile, workspace):
    """Write only the selected workspace's native agent definition."""
    provider = profile["provider"]
    required = setup_steps([profile], package["connectors"]) if package["connectors"] else []
    if any(row["status"] != "connected" for row in required):
        raise BridgeError("Complete connector setup for this client before installing the agent")
    workspace = Path(workspace).resolve()
    directory = workspace / (".codex" if provider == "codex" else ".claude") / "agents"
    if directory.resolve() != directory:
        raise BridgeError("The agent directory must be inside the workspace and cannot be a symlink")
    directory.mkdir(parents=True, exist_ok=True)
    ext = ".toml" if provider == "codex" else ".md"
    path = directory / (package["name"] + ext)
    def quote(value):
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    instructions = package["instructions"]
    if provider == "codex":
        if required:
            instructions += "\n\nUse these connections from the parent session: " + ", ".join(
                row["name"] for row in required
            ) + "."
        content = (f"name = {quote(package['name'])}\ndescription = {quote(package['description'])}\n"
                   f"developer_instructions = {quote(instructions)}\n")
    else:
        frontmatter = f"name: {quote(package['name'])}\ndescription: {quote(package['description'])}\n"
        if required:
            frontmatter += "mcpServers: [" + ", ".join(quote(row["name"]) for row in required) + "]\n"
        content = "---\n" + frontmatter + "---\n\n" + package["instructions"] + "\n"
    payload = content.encode("utf-8")
    try:
        with path.open("xb") as stream:
            stream.write(payload)
    except FileExistsError as exc:
        if path.is_symlink() or not path.is_file() or path.stat().st_size != len(payload) or path.read_bytes() != payload:
            raise BridgeError("An agent with this name already exists in the workspace") from exc
    return str(path.relative_to(workspace))
