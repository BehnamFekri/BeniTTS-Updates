# Graphify workflow

- Pin: Graphify 0.9.121 (`graphifyy[sql]==0.9.121`), Python >=3.11.5. Helper verifies exact version and invokes uv offline if needed. Warm once with `uv tool install --python 3.14 'graphifyy[sql]==0.9.121'` when authorized/network available. No auto upgrade.
- Graph roots: .. Only catalog/workflow files exist, so graph extraction is a no-op until source code appears.
- Commands: `py -3 scripts/graphify_workflow.py status|sync|verify|precommit|install-hooks|hooks-status`; use `query --root ROOT WORDS`, `path --root ROOT A B`, `explain --root ROOT SYMBOL`. Query defaults to depth 2 and budget 600 (override explicitly only when needed). On Windows, keep the checkout path short enough for Graphify AST cache paths.
- Start each code task with status, then focused query/path; open only relevant source/tests. After related code changes, sync then verify/test. Before commit, install hooks once. Check status after pull/branch switch; sync only if code fingerprint changed.
- Fingerprint covers supported code files from Git only, excludes generated/cache/secret files. If fingerprint, graph hash and pin match, sync is a no-op. A local `.git` lock prevents concurrent runs; existing graph output is backed up with SHA256 manifest before update.
- Health checks validate JSON and Graphify multigraph endpoint diagnostics. If unhealthy/stale or the pin is missing, do not claim graph-based findings; sync only within task authorization. `unlock` only clears old lock with dead PID and age >=10 minutes.
- Graph/cache/state/backups remain local. No LLM/API for extraction or queries; SQL parser uses pinned optional package extra. This catalog-only TTS repo has no code, so sync is a documented no-op.
- Hooks: `install-hooks` refuses to overwrite active hooks and backs up/hashes `.git/config`. Hooks are clone-local, not distributed. Precommit blocks stale/broken graphs; `--no-verify` or GitHub API edits can bypass. After checkout/merge, status-only checks warn; they do not rebuild.
- Unit tests: `py -3 -m unittest discover -s tests -p 'test_graphify_workflow.py' -v`.
- Report pin/status, roots/counts, focused query/path, source spot-check, sync/no-op reason, verify/tests, hook state and limitations.
