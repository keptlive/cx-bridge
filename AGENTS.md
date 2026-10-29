# cx-bridge development

Canonical source is this local git repository. Preserve the original installed
`cx` command and existing bridge jobs. Private local config and the legacy
snapshot must stay out of commits. Never print credentials or provider stderr
automatically. Keep provider permissions explicit; prose is not authorization.

Push only with Matt's authorization. Matt explicitly authorized live tests in
both directions, handoffs, session continuation, fixes and the finished 0.2 push
to keptlive. Do not rewrite published history or enable a workflow by changing
account permissions.

Run the subprocess regression suite on Windows and WSL for substantive changes.
Test failure paths and continuation, not just happy paths. Deterministic fixture
providers do not consume model usage. Do not represent fixture tests or CLI
help checks as successful live model execution. Test actual provider inference
only when delegation is explicitly authorized. Do not spawn review subagents
without Matt's authorization.
