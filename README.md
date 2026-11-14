# cx-bridge

A small, zero-dependency bridge and stdio MCP server across **Claude Code,
Codex, Antigravity (`agy`), ACP agents, and external agent frameworks**
(LangGraph, CrewAI, AutoGen, PydanticAI, OpenCode, Goose, Gemini CLI, and custom
runners), using their installed CLIs or Python handlers and existing sign-ins.
Send a scoped task to any agent, read its result, continue its saved session,
hand off verified outputs, and exchange inline messages across live sessions.
Python 3.10+, zero runtime dependencies.

```sh
cx-bridge run --to codex -C ./project "Explain how authentication works"
cx-bridge run --to claude -C ./project "Review the authentication flow"
cx-bridge run --to agy -C ./project "Audit edge cases in session handling"
cx-bridge resume JOB_ID "Check the edge case we just discussed"
cx-bridge handoff JOB_ID --to agy "Cross-check this answer for mistakes"

# Inline peer messaging across Codex, Antigravity, and Claude Code
cx-bridge say --to claude --from codex -C ./project "Check parser.py line 142"
cx-bridge say --to claude --from agy --wake SESSION_ID "Do you agree on the fix?"
cx-bridge say --to agy --from claude ide "Inspect the open editor tab"
cx-bridge say --to codex --from agy THREAD_ID "Re-run the unit suite"
```

Any agent can invoke these commands through its terminal tool—or call typed
tools via `cx-bridge mcp`—when the user has authorized delegation.

## Install

Install Codex, Claude Code, and/or Antigravity (`agy`) and sign in using their
own tools first.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/cx-bridge init
.venv/bin/cx-bridge doctor
```

On Windows, use `.venv\Scripts\python.exe` and
`.venv\Scripts\cx-bridge.exe`. For a normal package installation use
`python -m pip install .` in an appropriate virtual environment. The installed
command is `cx-bridge`; it never replaces an existing `cx` command.

For development without installing:

```sh
PYTHONPATH=src python3 -m cx_bridge --help
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

To verify the installed console entry point from a clean environment, build a
wheel with `python -m pip wheel --no-deps --wheel-dir dist .`, then run
`python tests/package_smoke.py dist/cx_agent_bridge-0.3.0-py3-none-any.whl`.
The smoke test uses deterministic fixtures, so it consumes no model usage.

## Configure different setups

Default configuration: `~/.config/cx-bridge/config.json`. Override it with
`CX_BRIDGE_CONFIG` or `cx-bridge --config PATH ...`. `init` refuses to overwrite
an existing file. The bridge reads only this user-selected configuration, never
an automatically discovered configuration in an untrusted project.

```json
{
  "version": 1,
  "default_target": "codex",
  "timeout": 600,
  "jobs_dir": "~/.local/state/cx-bridge/jobs",
  "targets": {
    "codex": {"kind": "codex", "command": ["codex"]},
    "claude": {"kind": "claude", "command": ["claude"]},
    "agy": {"kind": "agy", "command": ["agy"]}
  }
}
```

Targets are names (`kind` is `"codex"`, `"claude"`, `"agy"` / `"antigravity"`,
`"acp"` for Agent Client Protocol servers, or `"generic"` / `"framework"` /
`"custom"` for Python frameworks and declarative CLIs), so you can define
multiple profiles such as `codex-work`, `claude-review`, `agy-argon`,
`opencode`, or `langgraph`. Remove any target you do not have installed. See
[docs/connectors.md](docs/connectors.md) for **LangGraph, CrewAI, AutoGen,
PydanticAI, ACP (`opencode acp`, `goose acp`), Gemini CLI, and custom CLI**
connectors (`BridgeConnector` + `serve_framework_target`).

Optional target properties:

* `model` / `effort`: defaults overridden per run with `-m` and `-e`.
* `path_style`: `"native"` (default), `"windows"` (translates WSL `/mnt/<drive>`
  paths via `wslpath -w` and rejects `\\wsl.localhost` UNC workspaces without
  silent mirroring), or `"wsl"` (when running on Windows against a WSL binary
  such as Linux `claude`, translating `C:\...` and `\\wsl.localhost\...` paths
  and launching via `wsl.exe [-d wsl_distro] --cd ... --exec` with no shell).
* `wsl_distro`: optional WSL distribution name (e.g. `"Ubuntu"`) when
  `path_style` is `"wsl"`.
* `bin_dir`: optional parent directory (such as `.../OpenAI/Codex/bin`) to
  automatically select the newest `<hash>/codex.exe` across app updates. Even
  without `bin_dir`, rotated `<hex-hash>/codex.exe` paths automatically fall
  back to the newest sibling build.
* `chat_command`: optional argv array for Antigravity IDE live window delivery
  (`chat -r -m ask|agent -` over `stdin`, never `argv`).
* `home`: session index directory used by Codex `threads`.
* `env`: environment variable overrides inherited only by that target (forwarded
  via `WSLENV` `/u` when `path_style` is `"wsl"`), never echoed by `targets` or
  `doctor`, and never persisted in `job.json`.

Named workspaces can map a logical project to a shared path or per-target paths:

```json
"workspaces": {
  "project": {
    "codex": "/mnt/c/projects/example",
    "agy": "/mnt/c/projects/example",
    "claude": "/home/example/src/project"
  },
  "shared": {"*": "../project"}
}
```

See [setup examples](docs/setups.md) for cross-OS WSL/Windows, Claude Code live
hooks, and stdio MCP setups.

## Commands

| Command | Purpose |
| --- | --- |
| `run [--to TARGET] [-C DIR] [-w] [-m MODEL] [-e EFFORT] [-n NAME] PROMPT` | Run a task, defaulting to read-only |
| `review [--to TARGET] [-C DIR] [--base REF] [FOCUS]` | Request a correctness review |
| `resume JOB_ID PROMPT` | Continue the same provider session and workspace |
| `handoff JOB_ID --to TARGET PROMPT` | Give another target a successful job's result as reference context |
| `say [--to TARGET] [--from SENDER] [-C DIR] [--wake] [THREAD] MESSAGE` | Send an inline message to Codex, Claude, or Antigravity |
| `dispatch [--to TARGET] [--from SENDER] [--wake] [--no-wait] THREAD JOB.md` | Dispatch a scoped job file and await a unique `DONE.<uuid>.txt` receipt |
| `inbox [list\|ack] [--to TARGET] [--claim] [--all] [--json]` | Inspect, atomically claim, or acknowledge inline inbox messages |
| `hook claude [--to TARGET]` | Inject pending inline messages into live interactive Claude Code sessions |
| `mcp [--allow-write] [--identity NAME]` | Serve typed `cx-bridge` tools over stdio JSON-RPC 2.0 (MCP) |
| `threads [QUERY] [--to TARGET] [--all] [--json]` | List known sessions/threads across Codex, Claude, and Antigravity |
| `board [--group NAME] [--job ID] [--follow]` | Inspect jobs, peer messages, dispatch receipts, and observed file reads |
| `jobs` / `show JOB_ID` | Inspect recent jobs or structured status |
| `result JOB_ID` / `events JOB_ID --tail 25` | Read the saved result or recent raw provider events |
| `targets` / `workspaces` | List configured target summaries or workspace mappings |
| `doctor` | Check executables and CLI capabilities without inference |

Use `-` as `PROMPT` or `MESSAGE` to read stdin. `--json` emits structured JSON
on stdout while keeping diagnostics on stderr.

## Local control panel

Run `cx-bridge panel -C /path/to/project` and open the loopback URL it prints.
Use the same CX Bridge config as your workers. The **Activity** tab follows jobs,
plans, current tools, observed file reads, peer messages, and dispatch status.
Turn on task briefs or message bodies when you need to see their contents.

**Connectors** checks the configured Codex and Claude Code profiles in the
selected workspace. Existing connections appear as ready. For missing access,
the panel shows the command or in-client step and where to use it. Run that
step in the corresponding profile and scan again. CX Bridge uses each client's
existing authentication and keeps Claude-hosted connections in Claude.

To install a shared agent, choose a JSON file like this one, select a client,
and click **Install agent** once its connections are ready. The panel shows the
agent's instructions for review and rechecks its connections before writing a
native project agent file. Start a new client session in that workspace to
use it. An existing agent with the same name is preserved.

```json
{
  "version": 1,
  "name": "docs-reader",
  "description": "Find relevant OpenAI documentation.",
  "instructions": "Use the connected documentation service and cite the relevant pages.",
  "connectors": ["openai-docs"]
}
```

Built-in setup guides cover GitHub, Linear, and OpenAI Docs. The panel also
recognizes existing Gmail, Google Calendar, and Google Drive connections and
shows other discovered connectors by name. Connector IDs in shared files
contain lowercase letters, numbers, and hyphens. CX Bridge does not copy
credentials, install connector executables from shared files, or override
client and workspace access policies.

## Activity board

Run the board in a separate terminal while workers are running. Use the same
bridge config as the workers so the board reads their job and inbox records.

```sh
cx-bridge board --follow
cx-bridge board --group terra-pilot --follow --show-tasks --show-messages
cx-bridge board --job JOB_ID --json
```

Pass `--group terra-pilot` to related `run`, `say`, and `dispatch` calls.
`resume` and `handoff` inherit the original job's group unless overridden.
Use `-n` to give a job a readable assignment name. The board prioritizes
running jobs, shows model and session when known, and displays live Codex
checklists and supported tool activity when present. Dispatch messages show
the job file and whether a receipt appeared. Use `--show-tasks` for a short
preview of each job brief. Message bodies are opt-in with
`--show-messages`. The board never claims or acknowledges an inbox message.
With `--json --follow`, each changed snapshot is a separate JSON line.
The read-only MCP tool `bridge_board` exposes the same snapshots without
message bodies or task briefs.

The snapshot has two arrays, `tasks` and `messages`. A task includes its
job ID, optional group, target, model, session, status, `plan`, `active_tools`,
and `files_read`. Each read has `path`, `tool`, and `evidence`
(`provider_reported` or `inferred_from_command`). The `event_log` field
indicates whether the log was available, partial, missing, or too large.
A message includes its ID, sender, target, thread, delivery method, status,
and timestamps. Dispatches also include their task and receipt paths and
`receipt_ready`. A delivered live message was accepted by its transport.
A delivered inbox message was claimed. An acknowledged message has status
`acked`. IDs are stable across snapshots.

File reads are derived from existing job event logs. Claude Code `Read`,
Antigravity `view_file`, and ACP read calls with file locations are shown
when the provider reports a successful completion. For Codex, the board
labels simple successful commands such as `cat README.md` and
`Get-Content -LiteralPath 'README.md' -Raw` as inferred reads. Generic JSONL
connectors can emit `{"event":"file_read","status":"completed","path":"README.md"}`.
The board shows paths only. Reads inside scripts, pipelines, searches, or IDE
sessions whose events are outside the bridge logs may be absent.

## Inline messaging across all three agents

`cx-bridge` unifies inline messaging in every direction without unauthenticated
network listeners or shell string interpolation:

1. **To Claude Code (from Codex or Antigravity):**
   * **Live interactive terminal:** Wire `cx-bridge hook claude` into
     `UserPromptSubmit`, `PostToolUse`, and `Stop` in `~/.claude/settings.json`
     (see [docs/setups.md](docs/setups.md)). Messages queued via
     `cx-bridge say --to claude --from codex ...` or MCP `bridge_say` are
     atomically claimed once in FIFO order and injected inline via
     `additionalContext` (or wake an idle-bound session once on `Stop`, with
     `stop_hook_active` loop protection so agents cannot enter unattended
     ping-pong loops).
   * **Synchronous inline turn (`--wake`):** `cx-bridge say --to claude --wake SESSION_ID "message"`
     resumes the saved job or Claude session immediately and prints the reply.
   * **Direct inbox / MCP:** Any Claude session can also call `cx-bridge inbox --to claude --claim`
     or MCP `bridge_inbox`.
2. **To Antigravity (from Claude or Codex):**
   * **Live IDE window (`ide`):** With `chat_command` configured,
     `cx-bridge say --to agy --from claude ide "message"` streams the message
     over `stdin` to the active Antigravity IDE chat (`ask` mode by default;
     `-w` selects `agent` mode), and `dispatch --to agy ide JOB.md` dispatches a
     scoped job awaiting a unique `DONE.<uuid>.txt` receipt.
   * **Headless conversation (`--wake`) or inbox:** Resume any `--conversation`
     UUID synchronously via `agy --input-format text --output-format stream-json`,
     or queue into the durable inbox for `cx-bridge inbox` / MCP `bridge_inbox`.
3. **To Codex (from Claude or Antigravity):**
   * Queues directly via `codex queue [--resume if --wake] --thread THREAD --message ...`
     (with optional `--inbox-only` durable inbox delivery) and records an audit
     entry in `<jobs_dir>/inbox/`.

Every peer message delivered through hooks, `--from`, `inbox --claim`, or MCP is
wrapped in an explicit JSON provenance envelope marking it as **untrusted peer
reference data** (with sender, timestamp, and SHA-256 digest), never user
authorization.

## Permissions and execution

* **Codex:** `read-only` sandbox by default; `-w` chooses `workspace-write`,
  with `-a never` noninteractive approval denial.
* **Antigravity (`agy`):** `--mode plan --sandbox` by default (read-only
  research/planning); `-w` chooses `--mode accept-edits --sandbox` (auto-approve
  file edits while refusing unapproved commands in headless mode). Any
  `denied_actions` or non-`SUCCESS` status marks the job `failed`.
* **Claude Code:** Receives only `Read,Grep,Glob` by default (`Edit,Write` added
  with `-w`), `--permission-mode dontAsk`, empty MCP config, and hooks disabled
  so headless tasks never inherit shell tools or steal interactive inbox items.
* **MCP Server (`cx-bridge mcp`):** Read-only by default—rejects `write: true`
  and refuses to resume `workspace-write` parent jobs unless explicitly started
  with `--allow-write`. `--identity` pins non-spoofable sender attribution.

## Debugging and live validation

Every `run`, `review`, `resume`, and `handoff` creates a private `0700` job
directory containing `job.json`, `prompt.txt`, `events.jsonl`, `result.md`, and
`runner.log` (never echoed automatically). Durable inline messages live in
`<jobs_dir>/inbox/` with `0600` atomic writes and lock files under `.locks/`.

`doctor` performs `--version` and `--help` capability checks only—never model
inference. The opt-in live checker `python tests/live_smoke.py --live --directory PATH`
validates real authenticated runs, session continuation memory, and directional
handoffs across configured providers (`--providers codex,claude,agy`).
