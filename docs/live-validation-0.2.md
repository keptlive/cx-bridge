# cx-bridge 0.2 live validation

Completed October 28, 2026 (America/New_York), using the real installed Claude
Code and Codex CLIs and their existing authenticated accounts. These were actual
model calls, separate from the deterministic regression fixtures.

## Live results

| Check | Result | Seconds |
| --- | --- | ---: |
| Claude fresh session reads its unique fixture token | Passed | 8.12 |
| Codex fresh session reads its unique fixture token | Passed | 15.55 |
| Codex resumes its original session and recalls the token | Passed | 6.92 |
| Claude resumes its original session and recalls the token | Passed | 3.50 |
| Codex result handed to a new Claude session | Passed | 4.71 |
| Claude handoff session resumes and recalls the received token | Passed | 3.35 |
| Claude result handed to a new Codex session | Passed | 6.16 |
| Codex handoff session resumes and recalls the received token | Passed | 10.63 |

Both fresh sessions actually used file-reading tools. Event audits confirmed
that all handoff and continuation calls used no tools: their correct answers
came from supplied result context or saved conversation history, rather than
rereading a fixture. Original and resumed session IDs matched. Handoffs created
new destination sessions and retained source job IDs and result fingerprints.
All eight jobs retained read-only permissions; both fixture files were unchanged.

The bridge ran under Ubuntu WSL with Python 3.12. Codex CLI
`0.163.0-alpha.11` ran as the existing Windows executable; Claude Code `2.1.311`
ran natively in WSL. Both used their configured model defaults. No credentials
were copied or committed. Raw provider events, results, session IDs and the
machine-readable receipt remain in a private local test directory outside the
repository; this report contains only the result summary.

## Regression and package checks

The existing suite passes 42 tests in WSL and 41 on Windows, with the WSL-only
path-conversion case skipped on Windows. Clean wheel installation and console
run/handoff/resume/result/event/failure checks passed in both environments.
Earlier fixes cover malformed events, timeout continuation IDs, interspersed
resume options, private diagnostic output and invalid saved/configuration data.
The eight live calls succeeded with the current adapters.

## Repeating the live checks

After explicit authorization to consume the providers' normal usage:

```sh
python tests/live_smoke.py --live --directory /path/visible/to/both/providers
```

Use `--config PATH` for another private bridge configuration and `--timeout N`
for a different deadline per call. The script requires the explicit `--live`
flag, creates a unique test directory, and makes eight bounded read-only calls.
It checks exact random tokens, session continuity, tool use, result provenance
and fixture integrity. It does not run as part of unittest or CI.

Interactive Claude-terminal injection, hosted transport, remote cancellation,
macOS and other Python versions were not validated by these checks. The optional
GitHub Actions workflow remains an example: the connected GitHub credential
does not have workflow-upload permission, and no account permissions were changed.
