# Original cx workflow baseline

Inspected before implementing changes. The existing installed Bash `cx` script
is left intact. A local, git-ignored snapshot is retained in `legacy/cx`.

| Original path | New path / preservation |
| --- | --- |
| `cx run`, optional write/cwd/effort/name | `cx-bridge run`, same short options plus `--to`, model and timeout |
| `cx review`, base and focus | `cx-bridge review` with the same roles, either provider |
| `cx resume JOB FOLLOW_UP` | `cx-bridge resume JOB_ID FOLLOW_UP`, new bridge jobs |
| `cx say THREAD MESSAGE` | `cx-bridge say`, explicit Codex desktop queue |
| `cx dispatch THREAD JOB.md --no-wait` | `cx-bridge dispatch`, bounded wait and unique receipt |
| `cx threads QUERY` | `cx-bridge threads`, configured home and target |
| `cx jobs` | `cx-bridge jobs` and structured `show` |
| Original per-job artifacts | New private job directories with structured status and raw events |
| Automatic WSL-to-Windows mirror | Original script retains it; new bridge requires an explicit compatible workspace |

The original script's hard-coded local paths, fixed director/executor roles,
unbounded desktop wait, shared `DONE.txt`, timestamp-only job naming and
automatic repository mirroring are not copied into the public package.
Existing user job history and the installed command remain available unchanged.
No existing product, navigation, account or remote service was modified.

Two-way means either agent can submit a task to the other and receive the result.
Live Claude terminal injection, an MCP server, a hosted relay and fully managed
remote cancellation are outside this release's implemented capabilities.
