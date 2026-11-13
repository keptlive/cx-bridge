# Connecting Other Agent Frameworks & CLIs

Beyond its native adapters for **Claude Code (`claude`)**, **Codex (`codex`)**,
and **Antigravity (`agy`)**, `cx-bridge` connects to **any external agent
framework or CLI** through three stdlib-only mechanisms:

1. **Python Framework SDK (`cx_bridge.connector`)** — for **LangGraph**,
   **CrewAI**, **AutoGen**, **PydanticAI**, **OpenAI Agents SDK**,
   **Smolagents**, **LlamaIndex**, or custom `async`/`sync` Python agents.
2. **Agent Client Protocol (`kind: "acp"`)** — for any standardized ACP stdio
   agent (`opencode acp`, `goose acp`, `gemini --experimental-acp`, Zed/JetBrains
   ACP servers, etc.).
3. **Declarative CLI Connector (`kind: "generic"`)** — for any JSON, JSONL, or
   plain-text CLI (`gemini`, `aider`, `openhands`, `llm`, `ollama`, or custom
   Node/Rust/Go binaries) plus `cx-bridge hook gemini` / `cx-bridge hook generic`
   and `cx-bridge mcp`.

---

## 1. Python Agent Frameworks (LangGraph, CrewAI, AutoGen, PydanticAI, ...)

### A. Expose your framework AS a `cx-bridge` target

Wrap any `def` or `async def` handler in `serve_framework_target` (5 lines,
zero third-party dependencies required by `cx-bridge`):

```python
# my_langgraph_runner.py
from cx_bridge import AgentRequest, AgentResponse, serve_framework_target


async def run_graph(req: AgentRequest) -> AgentResponse:
    # req.task: user task; req.prompt: full safety-preambled prompt
    # req.cwd, req.mode ("read-only" | "workspace-write"), req.model, req.session
    answer = await invoke_my_langgraph(req.task, thread_id=req.session, cwd=req.cwd)
    return AgentResponse(
        response=answer,
        session_id=req.session or "lg-thread-1",
        tool_calls=["retriever"],
    )


if __name__ == "__main__":
    raise SystemExit(
        serve_framework_target(run_graph, name="langgraph", version="0.1.0", supports_write=False)
    )
```

Register it in `~/.config/cx-bridge/config.json`:

```json
"langgraph": {
  "kind": "framework",
  "command": ["/path/to/venv/bin/python", "/path/to/my_langgraph_runner.py"]
}
```

What `serve_framework_target` handles automatically:
* Answers `--version` / `--help` so `cx-bridge doctor` passes without running inference.
* Redirects noisy `print()` / Rich / CrewAI / LangChain banners to `stderr`
  (`runner.log`) so `stdout` stays valid `bridge-jsonl`.
* Enforces `supports_write=False` (failing closed with `denied_actions` if `-w`
  is requested unless explicitly enabled).
* Preserves `session_id` across `cx-bridge resume` and supports `handoff` to and
  from `claude`, `codex`, `agy`, and `acp` targets.

### B. Call Claude, Codex, Antigravity & inline inbox FROM your framework

Import `BridgeConnector` to call peer agents directly or bind ready-made tool
functions into your framework's tool registry:

```python
from cx_bridge import BridgeConnector

bridge = BridgeConnector(identity="langgraph", allow_write=False)

# 1. Direct Python calls
res = bridge.run("Audit auth middleware", to="claude", cwd="/path/to/repo")
bridge.say("Finished graph pass; check edge case #2", to="claude", cwd="/path/to/repo")
incoming = bridge.inbox(claim=True)  # Claims unread notes from Claude/Codex/AGY

# 2. Native tool callables (typed + docstring-annotated)
# Works directly with LangGraph ToolNode, PydanticAI Agent(tools=...),
# OpenAI Agents SDK, AutoGen FunctionTool, Smolagents, and LlamaIndex:
tools = bridge.as_tools()
# -> [bridge_run, bridge_resume, bridge_handoff, bridge_say, bridge_inbox]
```

---

## 2. Agent Client Protocol Targets (`kind: "acp"`)

Any agent implementing the open [Agent Client Protocol](https://agentclientprotocol.com)
over stdio JSON-RPC 2.0 works natively with `run`, `review`, `resume`
(`session/load`), `handoff`, and `say --wake`:

```json
"opencode": {
  "kind": "acp",
  "command": ["opencode", "acp"]
},
"goose": {
  "kind": "acp",
  "command": ["goose", "acp"]
}
```

**Deterministic permission gating:** `cx-bridge` acts as the ACP client on
`session/request_permission`:
* Default (`read-only`): approves only `read`, `search`, and `think` tool kinds;
  rejects `edit`, `delete`, `move`, `execute`, `fetch`, and `other`.
* With `-w` (`workspace-write`): additionally approves `edit`, while still
  denying shell `execute` and destructive operations. Any denied action marks
  the job `failed`.
* If an ACP binary starts its server loop even on `--version`, set
  `"doctor_args": []` on that target so `doctor` checks executable resolution
  without hanging.

---

## 3. Declarative CLI Targets (`kind: "generic"`)

Wrap any standalone CLI using pure `argv` arrays (never shell strings) and safe
`{cwd}`, `{session}`, `{model}`, `{effort}`, `{mode}`, and `{thread}` token
substitution:

```json
"gemini-cli": {
  "kind": "generic",
  "command": ["gemini"],
  "input_format": "text",
  "output_format": "bridge-jsonl",
  "read_only_args": ["--output-format", "stream-json", "--approval-mode", "plan"],
  "write_args": ["--output-format", "stream-json", "--approval-mode", "auto_edit"],
  "model_args": ["--model", "{model}"],
  "resume_args": ["--resume", "{session}"]
},
"local-llm": {
  "kind": "generic",
  "command": ["ollama", "run", "qwen2.5-coder"],
  "input_format": "text",
  "output_format": "text",
  "doctor_args": ["--version"]
}
```

* **`input_format`:** `"bridge-json"` (structured JSON envelope on `stdin`) or
  `"text"` (raw prompt on `stdin`).
* **`output_format`:** `"bridge-jsonl"` (NDJSON events), `"json"` (single JSON
  object on `stdout`), or `"text"` (plain UTF-8 `stdout` + exit code `0`).
* **Fail-closed `-w`:** A `"text"`-input target refuses `-w` (`workspace-write`)
  unless `write_args` is explicitly configured.
* **Custom live delivery (`say_command`):** Optional `argv` array (with
  `{thread}` / `{cwd}`) receiving inline `say` messages over `stdin`.

---

## 4. Universal Live Hooks & MCP for Any Framework

* **Gemini CLI hooks (`~/.gemini/settings.json`):** run
  `cx-bridge hook gemini --to gemini-cli` on `BeforeAgent`, `AfterTool`, and
  `AfterAgent` (includes `stop_hook_active` loop protection).
* **Generic JSON hook (Cursor, Cline, OpenHands, custom loops):** run
  `cx-bridge hook generic --to <target>` (accepts optional
  `{"session_id": "...", "cwd": "...", "event": "..."}` on `stdin` and outputs
  `{"count": N, "messages": [...], "context": "..."}` when unread messages are
  claimed).
* **Any MCP-capable framework:** connect over stdio via
  `cx-bridge mcp --identity <framework-name>`.
