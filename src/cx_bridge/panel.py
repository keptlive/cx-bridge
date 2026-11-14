"""Loopback-only control panel for a local CX Bridge installation."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import sys
import threading
from urllib.parse import parse_qs, urlsplit

from .activity import board
from .connector_setup import MAX_PACKAGE, install_agent, read_package, scan, setup_steps
from .inbox import BridgeError

ASSETS = Path(__file__).with_name("panel_assets")
ROUTES = {"/": "index.html", "/panel.css": "panel.css", "/panel.js": "panel.js"}
MAX_REQUEST = MAX_PACKAGE * 6 + 1024


class Panel:
    def __init__(self, cli, config, cwd):
        self.cli, self.config, self.cwd = cli, config, cwd
        self.root = cli.jobs_root(config)
        self.activity_cache = {}
        self.activity_lock = threading.Lock()
        self.scan_lock = threading.Lock()
        self.token = secrets.token_urlsafe(32)

    def path_for(self, workspace, target):
        if not workspace:
            return self.cwd
        paths = self.config["workspaces"].get(workspace)
        if paths is None:
            raise BridgeError("Unknown workspace")
        path = paths.get(target, paths.get("*"))
        return Path(path).resolve() if path and Path(path).is_dir() else None

    def scan_workspace(self, workspace, target=None):
        config = self.config
        if target is not None:
            if target not in config["targets"] or config["targets"][target]["kind"] not in ("codex", "claude"):
                raise BridgeError("Select a Codex or Claude Code profile")
            config = {**config, "targets": {target: config["targets"][target]}}
        paths = {name: self.path_for(workspace, name) for name in config["targets"]}
        if not self.scan_lock.acquire(blocking=False):
            raise BridgeError("A connector scan is already running. Try again when it finishes.")
        try:
            return scan(self.cli, config, paths)
        finally:
            self.scan_lock.release()

    def activity(self, query):
        def first(key):
            return query.get(key, [None])[0]

        job = self.cli.find_job(self.root, first("job")) if first("job") else None
        with self.activity_lock:
            return board(
                self.root, self.cli.read_record, job_file=job, target=first("target"),
                group=first("group"), limit=50, show_tasks=first("briefs") == "1",
                show_messages=first("messages") == "1", cache=self.activity_cache,
            )

    def get(self, route, query):
        if route == "/api/settings":
            return {"targets": self.cli.summarize_targets(self.config),
                    "workspaces": list(self.config["workspaces"]), "cwd": str(self.cwd)}
        if route == "/api/activity":
            return self.activity(query)
        return None

    def post(self, route, payload):
        if route not in ("/api/connectors", "/api/agents"):
            return None
        workspace = payload.get("workspace", "")
        if not isinstance(workspace, str):
            raise BridgeError("Select a workspace")
        if route == "/api/connectors":
            package = read_package(payload["package"]) if "package" in payload else None
            report = self.scan_workspace(workspace)
            requested = package["connectors"] if package else []
            return {"profiles": report,
                    "steps": setup_steps(report, requested) if not package or requested else [],
                    "agent": {k: package[k] for k in ("name", "description", "instructions", "connectors")} if package else None}
        package = read_package(payload.get("package"))
        target = payload.get("target")
        if not isinstance(target, str):
            raise BridgeError("Select a Codex or Claude Code profile")
        cwd = self.path_for(workspace, target)
        if cwd is None:
            raise BridgeError("The selected profile has no directory in this workspace")
        profile = self.scan_workspace(workspace, target)[0]
        path = install_agent(package, profile, cwd)
        return {"path": path, "workspace": str(cwd), "provider": profile["provider"]}


class PanelHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        return

    def respond(self, status, body, mime):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, value):
        self.respond(status, json.dumps(value, ensure_ascii=True).encode(), "application/json; charset=utf-8")

    def handle_request(self, post=False):
        self.connection.settimeout(10)
        panel = self.server.panel
        host = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != host or self.headers.get("Origin") not in (None, f"http://{host}"):
            self.send_json(403, {"error": "Open the panel at its local address"})
            return
        try:
            url = urlsplit(self.path)
            if not post and url.path in ROUTES:
                asset = ROUTES[url.path]
                data = (ASSETS / asset).read_bytes()
                if url.path == "/":
                    data = data.replace(b"__PANEL_TOKEN__", panel.token.encode())
                mime = mimetypes.guess_type(asset)[0] or "text/plain"
                self.respond(200, data, mime + "; charset=utf-8")
                return
            token = self.headers.get("X-CX-Panel", "")
            if not secrets.compare_digest(token.encode(), panel.token.encode()):
                self.send_json(403, {"error": "Reload the control panel"})
                return
            if post:
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= MAX_REQUEST or self.headers.get_content_type() != "application/json":
                    raise BridgeError("Send a JSON request within the size limit")
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise BridgeError("Expected a JSON object")
                result = panel.post(url.path, payload)
            else:
                result = panel.get(url.path, parse_qs(url.query, max_num_fields=8))
            self.send_json(404 if result is None else 200, result if result is not None else {"error": "Page not found"})
        except BridgeError as exc:
            self.send_json(400, {"error": str(exc)})
        except (ValueError, TypeError, RecursionError):
            self.send_json(400, {"error": "Invalid panel request"})
        except (ConnectionError, TimeoutError):
            return
        except OSError:
            self.send_json(500, {"error": "Could not read or write the local bridge files"})

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request(post=True)


def run_panel(cli, cfg, args):
    cwd = Path(args.cwd or Path.cwd()).expanduser().resolve()
    if not cwd.is_dir():
        raise BridgeError("Panel workspace does not exist")
    with ThreadingHTTPServer(("127.0.0.1", args.port), PanelHandler) as server:
        server.panel = Panel(cli, cfg, cwd)
        print(f"CX Bridge control panel: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("Panel closed", file=sys.stderr)
    return 0
