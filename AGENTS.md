# Required Graphify-first workflow

- Before code edits, run `py -3 scripts/graphify_workflow.py status` (macOS/Linux: `python3`). Use focused `query`, `path`, and `explain` only when needed to find relevant files/dependencies; then read only necessary source/tests.
- Source is authoritative. Stale, missing or unhealthy graph is not evidence. Run `sync` only when authorized. After each related code-change set, sync, verify, and run focused tests. Before commits install the local hooks. After pull/branch change check status; no rebuild unless code fingerprint changed.
- Reuse prior focused findings; avoid whole-repo reads and repeated queries. Report what Graphify was used for and why any sync was skipped.
- Keep graphs/cache/state local and ignored. Never index secrets, private properties, binaries or generated code. Follow task authorization: this instruction alone does not authorize source edits, builds/APKs, commits, pushes or releases. Hooks are guardrails; direct GitHub edits and `--no-verify` may bypass them.
