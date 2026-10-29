# Setup examples

## Native Linux / macOS / Windows

Put the provider executables on PATH, run `cx-bridge init`, and use the default
configuration. Both executables must see the workspace passed with `-C`.
On Windows a command can be an absolute `.exe` path. The bridge does not invoke
PowerShell or cmd to interpret command arrays. Use an executable wrapper if your
installation only exposes a shell/batch launcher.

## WSL with native Linux providers

Run the bridge inside WSL and install both provider CLIs there. This lets both
agents work in the same Linux repository without copying it:

```sh
cx-bridge run --to claude -C ~/src/project "Explain the project"
cx-bridge run --to codex -C ~/src/project "Review the changes"
```

## WSL with Windows Codex and Linux Claude Code

Set the Codex target's `command` to the Windows executable's mounted WSL path,
set `path_style` to `windows`, and set `home` to the mounted Windows Codex home
when using `threads`. Start from [the example](../examples/wsl-windows.json) and
replace every placeholder with your actual paths. The example is not runnable
without that configuration.

The bridge converts `/mnt/<drive>/...` workspace and dispatch paths with
`wslpath -w`. Use a Windows-accessible workspace. A Linux-only workspace would
become a UNC path, which this adapter rejects with an explanation. It never
silently mirrors a working repository or applies another agent's changes.
Use native WSL Codex for Linux source, or prepare and review a separate working
copy explicitly. Both providers' authentication stays in their existing stores.

## Multiple accounts or models

Use multiple target names. Keep account selection in a trusted wrapper that
sets the provider's home/configuration directory, then `exec`s the provider
with `"$@"`. Do not embed tokens in argv. An optional `model` or `effort` may be
specified separately for each target. Do not run wrappers through a shell string
assembled from a prompt.

## Launching WSL from Windows

A command array may use a launcher such as:

```json
["wsl.exe", "-d", "Ubuntu", "--cd", "/home/example/src/project", "--exec", "claude"]
```

The fixed `--cd` is part of the trusted target configuration. The bridge's own
`-C` must be a directory accessible to its Windows process; it does not translate
the Linux child's cwd for this form. Run the bridge inside WSL for dynamic Linux
workspaces. Stopping a Windows launcher does not guarantee a remote/WSL service
has stopped: wrappers need appropriate cancellation for their execution model.

## Containers / SSH / remote workers

Use a trusted executable wrapper that manages mounts, auth, cwd, stdin and
cancellation. This release tests native subprocesses and WSL-to-Windows path
conversion; it does not claim tested remote job lifecycle management. Simply
prefixing argv with `ssh` is insufficient: SSH reconstructs a remote shell
command, and prompts/paths need deliberate quoting and transport handling.
Never put a public listener in front of a write-capable agent without separate
authentication and authorization.
