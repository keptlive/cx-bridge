# cx-bridge

A small two-way bridge between **Claude Code and Codex**, using their installed
CLIs and existing sign-ins. Send a scoped task to either agent, read its result,
and continue its saved session. Python 3.10+, no runtime dependencies.

```sh
cx-bridge run --to codex -C ./project "Explain how authentication works"
cx-bridge run --to claude -C ./project "Review the authentication flow"
cx-bridge jobs
cx-bridge resume JOB_ID "Check the edge case we just discussed"
```

Either agent can invoke these commands through its terminal tool, when the user
has authorized delegation. This is two-way **task and result exchange**. It does
not inject text into an already running interactive Claude Code terminal.
Codex desktop messaging is a separate, optional capability.

## Install

Install Codex and/or Claude Code and sign in using their own tools first.

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
    "claude": {"kind": "claude", "command": ["claude"]}
  }
}
```

Targets are names, so you can add `codex-work`, `claude-review`, or multiple
versions of the same provider. Each supports an optional `model` and `effort`;
omit them to use the provider's defaults. Per-run `-m` and `-e` override them.
No particular model, account, username, home directory, host, or repository is
required. Remove a target if you only have one provider installed.

`command` is an argv array, **not a shell string**. It may name an absolute
executable or a trusted wrapper. Prompts go through stdin for task execution.
For launchers that cross environments, the wrapper must preserve stdin, stdout,
exit status and the intended workspace. See [setup examples](docs/setups.md).
Configuration is trusted code: do not accept a command array from another agent
or an untrusted repository. Keep credentials in the provider's own login store;
never put them in a command array or commit private configurations.

## Commands

| Command | Purpose |
| --- | --- |
| `run [--to TARGET] [-C DIR] [-w] [-m MODEL] [-e EFFORT] [-n NAME] PROMPT` | Run a task, defaulting to read-only |
| `review [--to TARGET] [-C DIR] [--base REF] [FOCUS]` | Request a correctness review |
| `resume JOB_ID PROMPT` | Continue the same provider session and workspace |
| `jobs` / `show JOB_ID` | Inspect recent jobs or structured status |
| `doctor` | Check executables and CLI capabilities without inference |
| `threads [QUERY] [--to CODEX_TARGET]` | Search the local Codex session index |
| `say THREAD MESSAGE [--to CODEX_TARGET]` | Queue an authorized message into Codex desktop |
| `dispatch THREAD JOB.md [--no-wait] [--timeout SECONDS]` | Dispatch to Codex desktop and wait for a unique receipt |

Use `-` as PROMPT to read stdin. `--timeout SECONDS` also applies to `run`,
`review` and `resume`. Resuming preserves the original permission mode, provider
configuration and workspace; it cannot silently switch to a different target.
A changed target configuration requires a new job. Model/effort can be changed
explicitly on resume. Review base/focus are instructions to the agent, not a
deterministic substitute for checking a diff yourself.

Codex `queue` availability varies with installation. `doctor` reports it. No
desktop messaging is attempted automatically. Claude uses fresh tasks and saved
session continuation, not desktop queue injection. Use exact job IDs when
resuming; ambiguous prefixes are rejected. Old `cx` jobs are retained by the
original script, rather than silently imported or rewritten.

## Permissions and execution

Codex runs with `read-only` by default; `-w` chooses `workspace-write`. Its normal
sandbox applies, and approval requests are denied for these noninteractive jobs.
The bridge never enables a bypass flag. Codex's own trusted configuration still
applies, including any integrations you have enabled there.

Claude receives only `Read,Grep,Glob` by default. With `-w`, it also receives
`Edit,Write`. Shell execution, delegation tools, inherited MCP servers and hooks
are unavailable in this adapter. Tools cannot prompt for additional permissions.
This is a tool restriction, **not an OS filesystem sandbox**. Provider policies
and managed settings still apply. If a task needs shell tests in Claude, use an
interactive Claude session or a separately sandboxed wrapper; this adapter does
not silently expand its permissions.

User authorization remains necessary for delegation, publishing and messaging.
The bridge does not establish authority merely because another agent can call
it. Model runs consume the signed-in provider's normal usage or billing.

## Debugging

Every invocation creates a unique job directory with:

* `job.json`: target, workspace, permission mode, parent/session IDs, timestamps,
  terminal status and exit code.
* `prompt.txt`: the scoped request.
* `events.jsonl`: raw provider output.
* `result.md`: extracted final response when the output is parseable.
* `runner.log`: provider stderr, kept private and never echoed automatically.

The directory is announced on stderr before execution. Status is `succeeded`
only after a valid completion event, successful process exit and no reported
provider error. A failed/partial run stays failed. Timeouts return 124 and stop
the local process tree; interruption returns 130. There is no automatic retry.
Remote workers behind custom launchers need their own cancellation mechanism.

`doctor` performs version/help probes, not inference or an authentication check.
It does not print raw help, environment variables or credentials. Session locks
prevent concurrent continuation by this bridge. After an OS crash, inspect the
PID recorded in a lock before manually removing a stale lock. Runs started
outside the bridge are not covered by its locks.

Keep job directories private: prompts, results and provider logs may contain
private project data. POSIX directories/files use owner-only permissions. On
Windows and shared/mounted filesystems, configure the parent directory ACLs.
Delete or archive job history yourself; there is no hidden retention policy.

## Desktop dispatch

Dispatch is useful for tasks requiring the desktop's browser. The target needs
the same Codex home/sign-in as the desktop, and a working `queue` command. The
job file and receipt must be visible to both the bridge and desktop agent.
Each dispatch requests `DONE.<unique-id>.txt` next to the job file. It preserves
an existing `DONE.txt` and never mistakes it for the current result.

A dispatch timeout ends the local wait; desktop work may continue. The receipt
is a plain text report, not a machine-verified proof of task success. `--no-wait`
prints the receipt location and returns after queue admission. The original job
must specify the intended browser, budget and authorized actions.

## Development and compatibility

See [the original workflow baseline](docs/baseline.md) for preserved paths and
intentional differences. Tests run real deterministic subprocess fixtures for
both provider protocols. They do not contact models or publish/send anything.
Current compatibility targets are the installed CLI help contracts, plus:

* [Codex App Server documentation](https://learn.chatgpt.com/docs/app-server)
* [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)
* [Running Claude Code programmatically](https://code.claude.com/docs/en/headless)

The bridge currently uses CLI task execution, not App Server RPC or an MCP
server. Those can be added as separate transports without changing the public
task interface. It does not launch an unauthenticated listener or require a
hosted relay.

An optional [GitHub Actions test matrix](examples/github-actions.yml) covers
Linux, Windows and macOS on Python 3.10 and 3.12. Copy it to
`.github/workflows/tests.yml` to enable it with a GitHub credential allowed to
manage workflows. It is an example in this repository, not an active CI run.
