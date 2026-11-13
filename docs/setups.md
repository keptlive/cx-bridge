# Setup examples

## Native Linux / macOS / Windows

Put the provider executables on PATH, run `cx-bridge init`, and use the default
configuration. Remove any target whose CLI you have not installed. All targets
must see the workspace passed with `-C` (or map per-target paths under
`workspaces`). On Windows a command can be an absolute `.exe` path. The bridge
never invokes PowerShell or `cmd.exe` to interpret command arrays.

## WSL with Windows Codex + Windows Antigravity + Linux Claude Code

Set the Codex and Antigravity targets' `command` arrays to their mounted WSL
paths (or set `bin_dir` on Codex so automatic `<hash>/codex.exe` version updates
are picked up without editing config), set `path_style` to `windows`, and set
`home` on Codex to the mounted Windows `.codex` directory when using `threads`.
Optionally configure `chat_command` on the `agy` target to deliver inline IDE
window messages via stdin (`chat -r -m ask|agent -`).

Start from [examples/wsl-windows.json](../examples/wsl-windows.json) and replace
every placeholder with your actual paths.

The bridge converts `/mnt/<drive>/...` workspace and dispatch paths with
`wslpath -w`. A Linux-only workspace would become a `\\wsl.localhost` UNC path,
which Windows targets reject with an explanation rather than silently mirroring
files. Use a shared `/mnt/<drive>/...` directory, a native WSL provider, or an
explicitly managed `workspaces` mapping.

## Running on Windows with WSL Claude Code + Native Windows Codex & Antigravity

When invoking `cx-bridge` directly from Windows (for example from Antigravity
IDE or Windows Codex), set `path_style` to `"wsl"` and `wsl_distro` on the
Claude target:

```json
{
  "version": 1,
  "default_target": "claude",
  "jobs_dir": "//wsl.localhost/Ubuntu/home/REPLACE_USER/.local/state/cx-bridge/jobs",
  "targets": {
    "claude": {
      "kind": "claude",
      "command": ["/home/REPLACE_USER/.local/bin/claude"],
      "path_style": "wsl",
      "wsl_distro": "Ubuntu"
    },
    "codex": {
      "kind": "codex",
      "bin_dir": "C:/Users/REPLACE_USER/AppData/Local/OpenAI/Codex/bin",
      "command": ["codex.exe"]
    },
    "agy": {
      "kind": "agy",
      "command": ["C:/Users/REPLACE_USER/AppData/Local/agy/bin/agy.exe"],
      "chat_command": [
        "C:/Users/REPLACE_USER/AppData/Local/Programs/Antigravity IDE/Antigravity IDE.exe",
        "C:/Users/REPLACE_USER/AppData/Local/Programs/Antigravity IDE/resources/app/out/cli.js"
      ],
      "env": {"ELECTRON_RUN_AS_NODE": "1"}
    }
  }
}
```

With `path_style: "wsl"`, Windows `C:\...` and `\\wsl.localhost\Ubuntu\...`
workspaces translate automatically and launch via `wsl.exe -d <distro> --cd ... --exec`
with zero shell interpolation. Notice also that invoking `Antigravity IDE.exe` +
`cli.js` directly with `ELECTRON_RUN_AS_NODE=1` avoids `.cmd` batch wrappers on
Windows.

## Live inline delivery into interactive Claude Code sessions

Interactive Claude Code terminals can receive inline messages from Codex and
Antigravity as they arrive by wiring `cx-bridge hook claude` in
`~/.claude/settings.json` using Claude Code's shell-free exec form:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "*",
        "hooks": [{"type": "command", "command": "cx-bridge", "args": ["hook", "claude"], "timeout": 5}]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [{"type": "command", "command": "cx-bridge", "args": ["hook", "claude"], "timeout": 5}]
      }
    ],
    "Stop": [
      {
        "matcher": "*",
        "hooks": [{"type": "command", "command": "cx-bridge", "args": ["hook", "claude"], "timeout": 5}]
      }
    ]
  }
}
```

* During `UserPromptSubmit` and `PostToolUse`, unread inbox messages for that
  Claude session or workspace are claimed atomically once and injected via
  `hookSpecificOutput.additionalContext` with explicit untrusted-peer provenance.
* On `Stop`, an unread message blocks idle once (`{"decision": "block", ...}`)
  so Claude responds inline immediately, while `stop_hook_active` loop-fencing
  prevents unattended autonomous ping-pong chains.
* Headless `cx-bridge run --to claude` jobs run with `--settings '{"disableAllHooks":true}'`
  so background sub-runs never steal messages meant for your interactive session.

## Stdio MCP server across Claude Code, Codex, and Antigravity

Any MCP-capable client can register `cx-bridge mcp` over stdio (no network port,
read-only task mode by default unless `--allow-write` is passed):

```json
{
  "mcpServers": {
    "cx-bridge": {
      "command": "cx-bridge",
      "args": ["mcp", "--identity", "agy"]
    }
  }
}
```

Pin `--identity` per client (`claude`, `codex`, or `agy`) so outgoing
`bridge_say` messages carry non-spoofable sender attribution and `bridge_inbox`
defaults to that agent's queue.

## Multiple accounts, containers, or remote workers

Use multiple target names. Keep account selection in a trusted wrapper that sets
the provider's home/configuration directory, then `exec`s the provider with
`"$@"`. Never embed tokens in `argv` or assemble remote `ssh` shell strings from
prompts.
