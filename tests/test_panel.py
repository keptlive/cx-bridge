"""Connector inventory, private panel endpoints, and native agent installation."""
import json
from pathlib import Path
import sys
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

from cx_bridge import cli
from cx_bridge.panel import Panel, PanelHandler
import test_bridge


class PanelTests(unittest.TestCase):
    setUp = test_bridge.BridgeTests.setUp
    tearDown = test_bridge.BridgeTests.tearDown
    save = test_bridge.BridgeTests.save

    def panel(self):
        fixture = str(Path(__file__).with_name("fake_connector_cli.py"))
        for name in ("codex", "claude"):
            self.cfg["targets"][name]["command"] = [sys.executable, fixture, name]
        return Panel(cli, self.cfg, self.work)

    def package(self, *connectors, instructions="Use the connected service."):
        return json.dumps({"version": 1, "name": "reader", "description": "Read linked material",
                           "instructions": instructions, "connectors": list(connectors)})

    def test_scan_same_service_with_different_names_and_separate_states(self):
        result = self.panel().post("/api/connectors", {})
        rows = {(row["provider"], row["service"]): row for row in result["steps"]}
        self.assertEqual(rows["codex", "openai-docs"]["name"], "docs-work")
        self.assertIsNone(rows["codex", "openai-docs"]["command"])
        self.assertEqual(rows["codex", "linear"]["command"], "codex mcp login linear")
        self.assertEqual(rows["claude", "linear"]["command"], "claude mcp login linear-work")
        self.assertEqual(rows["claude", "gmail"]["status"], "connected")
        self.assertEqual(rows["codex", "gmail"]["command"], "/apps")
        self.assertEqual(rows["codex", "google-drive"]["source"], "Codex app")
        self.assertNotIn("FIXTURE", json.dumps(result))

    def test_install_uses_detected_connections_and_preserves_existing_agents(self):
        panel = self.panel()
        self.assertRaisesRegex(cli.BridgeError, "Complete connector setup", panel.post, "/api/agents",
                               {"package": self.package("linear"), "target": "codex"})
        body = {"package": self.package("openai-docs"), "target": "codex"}
        installed = self.work / panel.post("/api/agents", body)["path"]
        self.assertEqual(installed, self.work / ".codex/agents/reader.toml")
        self.assertIn('Use these connections from the parent session: docs-work.',
                      installed.read_text(encoding="utf-8"))
        self.assertEqual(panel.post("/api/agents", body)["path"], str(Path(".codex/agents/reader.toml")))
        with self.assertRaisesRegex(cli.BridgeError, "already exists"):
            panel.post("/api/agents", {**body, "package": self.package("openai-docs", instructions="Changed.")})
        package = self.package("gmail")
        installed = self.work / panel.post("/api/agents", {"package": package, "target": "claude"})["path"]
        self.assertIn('mcpServers: ["claude.ai Gmail"]', installed.read_text(encoding="utf-8"))
        unsafe = json.loads(package)
        unsafe["connectors"] = [{"command": "example"}]
        with self.assertRaisesRegex(cli.BridgeError, "Invalid connector ID"):
            panel.post("/api/connectors", {"package": json.dumps(unsafe)})

    def test_http_requires_panel_token_and_rejects_foreign_origins(self):
        panel = self.panel()
        with ThreadingHTTPServer(("127.0.0.1", 0), PanelHandler) as server:
            server.panel = panel
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                with self.assertRaises(HTTPError) as denied:
                    urlopen(url + "/api/activity", timeout=5)
                self.assertEqual(denied.exception.code, 403)
                headers = {"X-CX-Panel": panel.token, "Origin": "https://external.invalid"}
                with self.assertRaises(HTTPError) as denied:
                    urlopen(Request(url + "/api/activity", headers=headers), timeout=5)
                self.assertEqual(denied.exception.code, 403)
                del headers["Origin"]
                with urlopen(Request(url + "/api/activity", headers=headers), timeout=5) as response:
                    self.assertEqual(json.load(response), {"tasks": [], "messages": []})
            finally:
                server.shutdown()
                thread.join(timeout=5)
