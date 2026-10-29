# Local 0.2 debugging and extensions

Matt clarified that this is the existing cx connector and requested debugging
and extension before the next push. The initial 0.1 source was already public;
this follow-up remains local. No original cx command or user history was removed.

## Reproduced defects

The same deterministic subprocess fixture was run against the published source
and local implementation:

| Input/state | Published 0.1 | Local 0.2 |
| --- | --- | --- |
| Codex `item.completed` with `item: null` | Uncaught `AttributeError` | Failed job, clean exit 1, valid preceding session ID retained |
| Session-start event followed by truncated JSON at timeout | Exit 124, session lost | Exit 124, `timed_out`, session retained |
| `resume JOB_ID -e high PROMPT` | Optional prompt parsing rejected the follow-up | Required prompt position parses correctly with interspersed options |

Additional hardening covers corrupt job records, invalid target environment
configuration, JSON launch errors that retain a job ID, and provider queue
stdout/stderr being captured rather than echoed. Relative profile paths now
resolve consistently against the selected configuration file.

## Added capabilities

* Explicit handoffs in both directions, with result-only context, recorded
  provenance and a fingerprint of the shared text. Destination permissions start
  read-only. Handoffs require a succeeded source job and an explicit next task.
* Named workspaces with shared or per-target paths; handoffs follow the logical
  workspace across targets, while resumes retain their original physical path.
* Per-target process environment without copying values into job metadata.
* JSON job/result output and saved result/event inspection, including malformed
  event tails. Target/workspace discovery and bounded history counts.

## Verification boundaries

The regression suite passes 41 tests in WSL and 40 tests on Windows, with one
WSL-only test skipped on Windows. Both environments test actual local process
tree termination on timeout. Tests use real subprocess fixtures and private
temporary configurations. They never contact a model or send a message to
another chat. Installed provider command help/parser checks are distinct from
authenticated live inference.

Clean virtual-environment wheel installs and actual console commands also pass
on Windows and WSL: run, handoff, resume with options between positional
arguments, saved result/event inspection, and malformed-provider-output failure.
The installed Codex and Claude CLIs accept all eight checked combinations of
provider, read-only/workspace-write mode, and fresh/resumed session through their
help parsers. Those checks do not start inference. The local installation reports
version 0.2.0; the original installed cx script remains byte-for-byte unchanged.

No live model response is claimed. macOS and the Python-version CI matrix remain
unverified here; the optional GitHub Actions example is still inactive because
the connected GitHub credential lacks workflow-upload permission. No account
permissions were changed to work around it. No follow-up changes were pushed.
