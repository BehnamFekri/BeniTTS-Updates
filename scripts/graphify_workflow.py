#!/usr/bin/env python3
"""Local, pinned, code-only Graphify workflow for this repository."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.path.abspath(__file__)).parents[1]
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(errors="backslashreplace")
CONFIG = ROOT / "graphify-workflow.json"
VERSION_FILE = ROOT / ".graphify-version"
STATE_NAME = "graphify-workflow-state.json"
LOCK_NAME = "graphify-workflow.lock"
CODE_EXTS = {
    ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".java", ".kt",
    ".kts", ".go", ".rs", ".cs", ".c", ".cc", ".cpp", ".h", ".hh", ".hpp",
    ".rb", ".php", ".swift", ".scala", ".sql", ".lua", ".sh", ".bash", ".zsh",
    ".pl", ".r", ".f", ".f90", ".f95", ".f03", ".f08",
}
EXCLUDED_DIRS = {
    ".git", ".gradle", ".idea", ".next", ".nuxt", ".output", ".venv",
    "__pycache__", "build", "coverage", "dist", "graphify-out", "node_modules",
    "out", "target", "vendor", "venv", ".vercel",
}
SECRET_NAMES = {"local.properties"}
SECRET_SUFFIXES = {".apk", ".aab", ".jks", ".keystore", ".p12", ".pfx", ".pem", ".key"}


class WorkflowError(RuntimeError):
    pass


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace")
    if check and result.returncode:
        raise WorkflowError(result.stderr.strip() or f"git {args[0]} failed")
    return result


def git_dir() -> Path:
    raw = git(["rev-parse", "--absolute-git-dir"]).stdout.strip()
    if not raw:
        raise WorkflowError("This directory is not inside a Git working tree.")
    return Path(raw).absolute()


def load_config() -> dict:
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        version = VERSION_FILE.read_text(encoding="utf-8").strip()
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Graphify config is missing or invalid: {exc}") from exc
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise WorkflowError(".graphify-version must contain one exact x.y.z version.")
    cfg["version"] = version
    cfg.setdefault("package", "graphifyy")
    cfg.setdefault("graph_roots", ["."])
    cfg.setdefault("exclude_paths", [])
    if not isinstance(cfg["graph_roots"], list) or not all(isinstance(x, str) for x in cfg["graph_roots"]):
        raise WorkflowError("graph_roots must be a list of repository-relative paths.")
    for rel in cfg["graph_roots"]:
        resolved = (ROOT / rel).absolute()
        try:
            resolved.relative_to(ROOT.absolute())
        except ValueError as exc:
            raise WorkflowError(f"Graph root escapes repository: {rel}") from exc
        if not resolved.is_dir():
            raise WorkflowError(f"Configured Graphify root does not exist: {rel}")
    return cfg


def graphify_runner(cfg: dict) -> list[str]:
    expected = cfg["version"]
    exe = shutil.which("graphify")
    if exe:
        result = subprocess.run([exe, "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        match = re.search(r"graphify\s+(\d+\.\d+\.\d+)", result.stdout, re.I)
        if result.returncode == 0 and match and match.group(1) == expected:
            return [exe]
    uv = shutil.which("uv")
    if uv:
        return [uv, "tool", "run", "--offline", "--from",
                f"{cfg['package']}=={expected}", "graphify"]
    actual = "unknown"
    if exe:
        actual = result.stdout.strip() or "version check failed"
    raise WorkflowError(
        f"Graphify {expected} is required; found {actual}. Install the pinned tool once "
        "using the documented command, then retry. No automatic install or network lookup was made."
    )


def graphify_call(cfg: dict, cwd: Path, args: list[str], *, quiet: bool = False) -> str:
    cmd = graphify_runner(cfg)
    env = os.environ.copy()
    env["GRAPHIFY_NO_AUTO_REFRESH"] = "1"
    env["GRAPHIFY_FORCE"] = "0"
    result = subprocess.run(cmd + args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", timeout=900)
    if not quiet and result.stdout:
        print(result.stdout.rstrip())
    if result.returncode:
        raise WorkflowError(f"Graphify exited with {result.returncode}. Existing graph backup is retained.")
    return result.stdout


def ensure_graphify_version(cfg: dict) -> None:
    output = graphify_call(cfg, ROOT, ["--version"], quiet=True)
    match = re.search(r"graphify\s+(\d+\.\d+\.\d+)", output, re.I)
    if not match or match.group(1) != cfg["version"]:
        raise WorkflowError(f"Graphify version mismatch: expected {cfg['version']}, got {output.strip()!r}.")
    print(f"Graphify {match.group(1)} (pinned, local execution).")


def git_paths(scope: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", scope],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise WorkflowError(result.stderr.decode("utf-8", "replace").strip())
    return sorted({os.fsdecode(part) for part in result.stdout.split(b"\0") if part})


def is_excluded(rel: str, cfg: dict) -> bool:
    path = Path(rel)
    parts = {part.lower() for part in path.parts}
    name = path.name.lower()
    if parts & EXCLUDED_DIRS or name in SECRET_NAMES or name.startswith(".env"):
        return True
    if path.suffix.lower() in SECRET_SUFFIXES:
        return True
    normalized = path.as_posix()
    for exclusion in cfg.get("exclude_paths", []):
        if normalized == exclusion or normalized.startswith(exclusion.rstrip("/") + "/"):
            return True
    return False


def code_files(scope: str, cfg: dict) -> list[Path]:
    scope_path = (ROOT / scope).absolute()
    rel_scope = Path(scope).as_posix().rstrip("/")
    found = []
    for rel in git_paths(scope):
        path = Path(rel)
        try:
            (ROOT / path).absolute().relative_to(scope_path)
        except ValueError:
            continue
        if path.suffix.lower() not in CODE_EXTS or is_excluded(rel, cfg):
            continue
        absolute = ROOT / path
        if absolute.is_file():
            found.append(absolute)
    return sorted(found, key=lambda p: p.relative_to(ROOT).as_posix())


def fingerprint(files: list[Path], graph_root: Path | None = None) -> str:
    outer = hashlib.sha256()
    metadata = {ROOT / "graphify-workflow.json", ROOT / ".graphify-version", ROOT / ".graphifyignore"}
    if graph_root is not None:
        metadata.add(graph_root / ".graphifyignore")
    for meta in sorted(metadata, key=lambda p: p.as_posix()):
        if meta.is_file():
            rel = meta.relative_to(ROOT).as_posix().encode("utf-8", "surrogateescape")
            outer.update(b"META\\0" + rel + b"\\0" + hashlib.sha256(meta.read_bytes()).digest() + b"\\n")
    for path in files:
        rel = path.relative_to(ROOT).as_posix().encode("utf-8", "surrogateescape")
        content_hash = hashlib.sha256(path.read_bytes()).digest()
        outer.update(rel + b"\0" + content_hash + b"\n")
    return outer.hexdigest()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_file(source: Path, backup: Path) -> str:
    before = sha256(source)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, backup)
    if sha256(backup) != before:
        raise WorkflowError(f"Backup verification failed: {backup}")
    return before


def backup_graph_output(graph_root: Path) -> Path | None:
    output = graph_root / "graphify-out"
    if not output.is_dir():
        return None
    files = [p for p in output.rglob("*") if p.is_file()]
    if not files:
        return None
    rel = graph_root.relative_to(ROOT).as_posix() or "root"
    tag = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:12]
    target = git_dir() / "graphify-backups" / tag / now_stamp()
    target.mkdir(parents=True, exist_ok=False)
    manifest = []
    for src in files:
        relpath = src.relative_to(output)
        dst = target / relpath
        dst.parent.mkdir(parents=True, exist_ok=True)
        before = backup_file(src, dst)
        manifest.append({"path": relpath.as_posix(), "sha256": before})
    (target / "SHA256.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {len(files)} pre-update Graphify files with SHA256 manifest: {target}")
    return target


def load_state(gitdir: Path) -> dict:
    path = gitdir / STATE_NAME
    if not path.exists():
        return {"schema": 1, "roots": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != 1 or not isinstance(data.get("roots"), dict):
            raise ValueError("unsupported state schema")
        return data
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise WorkflowError(f"State file is invalid; preserved at {path}: {exc}") from exc


def save_state(gitdir: Path, state: dict) -> None:
    path = gitdir / STATE_NAME
    if path.exists():
        backup_file(path, gitdir / "graphify-backups" / f"{STATE_NAME}.{now_stamp()}.bak")
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class RunLock:
    def __init__(self):
        self.path = git_dir() / LOCK_NAME
        self.owned = False

    def __enter__(self):
        try:
            self.path.mkdir()
        except FileExistsError as exc:
            detail = "owner metadata unavailable"
            owner = self.path / "owner.json"
            if owner.exists():
                try:
                    detail = owner.read_text(encoding="utf-8")
                except OSError:
                    pass
            raise WorkflowError(f"Another Graphify operation holds the lock: {self.path} ({detail})") from exc
        (self.path / "owner.json").write_text(
            json.dumps({"pid": os.getpid(), "started_utc": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8",
        )
        self.owned = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.owned and self.path.parent.absolute() == git_dir():
            shutil.rmtree(self.path)


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False


def clear_stale_lock() -> None:
    lock = git_dir() / LOCK_NAME
    if not lock.exists():
        print("No Graphify lock to clear.")
        return
    owner_path = lock / "owner.json"
    try:
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        pid = int(owner["pid"])
        started = datetime.fromisoformat(owner["started_utc"])
        age = (datetime.now(timezone.utc) - started).total_seconds()
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Lock metadata is invalid; inspect manually before recovery: {exc}") from exc
    if process_alive(pid) or age < 600:
        raise WorkflowError(f"Lock is not stale (pid={pid}, age={int(age)}s); it was left intact.")
    if lock.absolute().parent != git_dir() or lock.name != LOCK_NAME:
        raise WorkflowError("Refusing to remove a path outside the expected Git lock location.")
    shutil.rmtree(lock)
    print(f"Removed abandoned Graphify lock from dead pid {pid} (age={int(age)}s).")


def graph_file(graph_root: Path) -> Path:
    return graph_root / "graphify-out" / "graph.json"


def graph_health(cfg: dict, graph_root: Path) -> tuple[int, int, str]:
    path = graph_file(graph_root)
    if not path.is_file():
        raise WorkflowError(f"Graph is missing: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"graph.json is unreadable: {exc}") from exc
    nodes = data.get("nodes")
    edges = data.get("edges", data.get("links"))
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise WorkflowError("graph.json schema is invalid: nodes/edges arrays are required.")
    ids = [node.get("id") for node in nodes if isinstance(node, dict)]
    if len(ids) != len(nodes) or any(not isinstance(node_id, str) for node_id in ids):
        raise WorkflowError("graph.json contains a node without a string id.")
    if len(set(ids)) != len(ids):
        raise WorkflowError("graph.json contains duplicate node ids.")
    for edge in edges:
        if not isinstance(edge, dict) or not isinstance(edge.get("source"), str) or not isinstance(edge.get("target"), str):
            raise WorkflowError("graph.json contains an edge with invalid endpoints.")
    output = graphify_call(cfg, graph_root, ["diagnose", "multigraph", "--graph", "graphify-out/graph.json"], quiet=True)
    for metric in ("missing_endpoint_edges", "dangling_endpoint_edges"):
        match = re.search(rf"{metric}:\s*(\d+)", output)
        if not match:
            raise WorkflowError(f"Graphify diagnostic did not report {metric}.")
        if int(match.group(1)) != 0:
            raise WorkflowError(f"Graphify diagnostic found {metric}={match.group(1)}.")
    return len(nodes), len(edges), sha256(path)


def all_roots(cfg: dict) -> list[tuple[str, Path]]:
    return [(rel, (ROOT / rel).absolute()) for rel in cfg["graph_roots"]]


def root_record(state: dict, rel: str) -> dict:
    return state.get("roots", {}).get(rel, {})


def status_all(cfg: dict, *, quiet: bool = False, allow_locked: bool = False) -> bool:
    gitdir = git_dir()
    if (gitdir / LOCK_NAME).exists() and not allow_locked:
        if not quiet:
            print(f"Graphify is busy: {gitdir / LOCK_NAME}")
        return False
    state = load_state(gitdir)
    ok = True
    total_files = 0
    for rel, graph_root in all_roots(cfg):
        files = code_files(rel, cfg)
        total_files += len(files)
        graph = graph_file(graph_root)
        if not files:
            if not quiet:
                print(f"{rel}: NO PROJECT CODE ({'graph retained' if graph.exists() else 'no graph needed'})")
            continue
        current = fingerprint(files, graph_root)
        record = root_record(state, rel)
        if not graph.exists():
            ok = False
            if not quiet:
                print(f"{rel}: MISSING graph; {len(files)} code files")
            continue
        try:
            nodes, edges, graph_hash = graph_health(cfg, graph_root)
        except WorkflowError as exc:
            ok = False
            if not quiet:
                print(f"{rel}: UNHEALTHY graph: {exc}")
            continue
        fresh = (
            record.get("source_sha256") == current
            and record.get("graph_sha256") == graph_hash
            and record.get("graphify_version") == cfg["version"]
        )
        if not fresh:
            ok = False
        if not quiet:
            print(f"{rel}: {'CURRENT' if fresh else 'STALE'}; code_files={len(files)} nodes={nodes} edges={edges}")
    if total_files == 0 and not quiet:
        print("No supported project code is currently configured; Graphify sync is a no-op.")
    return ok


def uncovered_code_files(cfg: dict) -> list[Path]:
    all_files = code_files(".", cfg)
    roots = [(ROOT / rel).absolute() for rel in cfg["graph_roots"]]
    return [p for p in all_files if not any(_is_under(p, root) for root in roots)]


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.absolute().relative_to(root.absolute())
        return True
    except ValueError:
        return False


def sync_locked(cfg: dict) -> None:
    uncovered = uncovered_code_files(cfg)
    if uncovered:
        paths = ", ".join(p.relative_to(ROOT).as_posix() for p in uncovered[:10])
        raise WorkflowError(
            f"Code files are outside configured graph_roots: {paths}. Update graphify-workflow.json before continuing."
        )
    state = load_state(git_dir())
    updated = False
    for rel, graph_root in all_roots(cfg):
        files = code_files(rel, cfg)
        if not files:
            print(f"{rel}: no project code; no graph build/update required.")
            continue
        current = fingerprint(files, graph_root)
        path = graph_file(graph_root)
        record = root_record(state, rel)
        previous_hash = sha256(path) if path.exists() else None
        is_current = (
            path.exists()
            and record.get("source_sha256") == current
            and record.get("graph_sha256") == previous_hash
            and record.get("graphify_version") == cfg["version"]
        )
        if is_current:
            nodes, edges, digest = graph_health(cfg, graph_root)
            print(f"{rel}: unchanged; skipped Graphify update (nodes={nodes}, edges={edges}).")
            continue
        if (graph_root / "graphify-out").exists():
            backup_graph_output(graph_root)
        if path.exists():
            graphify_call(cfg, graph_root, ["update", "."])
        else:
            graphify_call(cfg, graph_root, [".", "--code-only", "--no-viz", "--no-cluster"])
        nodes, edges, digest = graph_health(cfg, graph_root)
        state.setdefault("roots", {})[rel] = {
            "source_sha256": current,
            "graph_sha256": digest,
            "graphify_version": cfg["version"],
            "source_file_count": len(files),
            "node_count": nodes,
            "edge_count": edges,
            "validated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        updated = True
        print(f"{rel}: graph verified ({nodes} nodes, {edges} edges).")
    if updated or not (git_dir() / STATE_NAME).exists():
        state["schema"] = 1
        state["graphify_version"] = cfg["version"]
        state["package"] = cfg["package"]
        save_state(git_dir(), state)


def sync() -> None:
    cfg = load_config()
    ensure_graphify_version(cfg)
    with RunLock():
        sync_locked(cfg)


def precommit() -> None:
    cfg = load_config()
    ensure_graphify_version(cfg)
    with RunLock():
        sync_locked(cfg)
        staged_code_consistency(cfg)
        if not status_all(cfg, quiet=False, allow_locked=True):
            raise WorkflowError("Pre-commit blocked: Graphify graph is missing, stale, or unhealthy.")


def staged_code_consistency(cfg: dict) -> None:
    unstaged = git(["diff", "--name-only", "-z"]).stdout.split("\0")
    untracked = git(["ls-files", "--others", "--exclude-standard", "-z"]).stdout.split("\0")
    dirty = [rel for rel in unstaged + untracked if rel and Path(rel).suffix.lower() in CODE_EXTS and not is_excluded(rel, cfg)]
    if dirty:
        raise WorkflowError("Unstaged or untracked code is present; stage the final code snapshot before committing: " + ", ".join(dirty[:10]))
    staged = git(["diff", "--cached", "--name-only", "-z"]).stdout.split("\0")
    mismatches = []
    for rel in staged:
        if not rel or Path(rel).suffix.lower() not in CODE_EXTS or is_excluded(rel, cfg):
            continue
        comparison = git(["diff", "--quiet", "--", rel], check=False)
        if comparison.returncode == 1:
            mismatches.append(rel)
        elif comparison.returncode not in (0,):
            raise WorkflowError(f"Could not compare staged and working contents for {rel}.")
    if mismatches:
        raise WorkflowError(
            "Staged code differs from the worktree; stage the final contents, then rerun Graphify: "
            + ", ".join(mismatches[:10])
        )


def hook_status() -> None:
    configured = git(["config", "--local", "--get", "core.hooksPath"], check=False).stdout.strip()
    expected = ".githooks"
    print(f"core.hooksPath={configured or '(Git default)'}; expected={expected}")
    if configured.replace("\\", "/") != expected:
        raise WorkflowError("Hooks are not installed for this clone. Run install-hooks once.")


def active_hooks(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if p.is_file() and not p.name.endswith(".sample"))

def install_hooks() -> None:
    cfg = load_config()
    gitdir = git_dir()
    target = ROOT / ".githooks"
    required = ("pre-commit", "post-checkout", "post-merge")
    if any(not (target / name).is_file() for name in required):
        raise WorkflowError("Required versioned hook scripts are missing from .githooks/.")
    configured = git(["config", "--local", "--get", "core.hooksPath"], check=False).stdout.strip()
    old_dir = (ROOT / configured).absolute() if configured and not Path(configured).is_absolute() else (
        Path(configured).absolute() if configured else gitdir / "hooks"
    )
    if old_dir != target.absolute():
        active = active_hooks(old_dir)
        if active:
            raise WorkflowError(
                "Existing active hooks were found and left untouched: "
                + ", ".join(str(old_dir / name) for name in active)
                + ". Integrate them manually before installing Graphify hooks."
            )
    if configured.replace("\\", "/") == ".githooks":
        print("Graphify hooks are already installed.")
        return
    config_path = Path(git(["rev-parse", "--git-path", "config"]).stdout.strip())
    if not config_path.is_absolute():
        config_path = (ROOT / config_path).absolute()
    if not config_path.is_file():
        raise WorkflowError(f"Local Git config not found: {config_path}")
    backup = gitdir / "graphify-backups" / f"git-config.pre-hooks.{now_stamp()}.bak"
    before = backup_file(config_path, backup)
    print(f"Git config SHA256 before hook install: {before}; backup: {backup}")
    git(["config", "--local", "core.hooksPath", ".githooks"])
    hook_status()


def run_query(kind: str, rel: str | None, words: list[str]) -> None:
    cfg = load_config()
    ensure_graphify_version(cfg)
    choices = all_roots(cfg)
    if rel is None:
        if len(choices) != 1:
            raise WorkflowError("Select a graph root with --root PATH.")
        rel, graph_root = choices[0]
    else:
        selected = [(name, path) for name, path in choices if name == rel]
        if not selected:
            raise WorkflowError(f"Unknown graph root: {rel}")
        rel, graph_root = selected[0]
    if not words:
        raise WorkflowError(f"Usage: {Path(__file__).name} {kind} --root {rel} <query>")
    with RunLock():
        if not status_all(cfg, quiet=True, allow_locked=True):
            raise WorkflowError("Graph is missing/stale/unhealthy; run sync before querying.")
        args = [kind, *words]
        if kind == "query":
            if "--budget" not in words:
                args.extend(["--budget", "600"])
            if "--depth" not in words:
                args.extend(["--depth", "2"])
        graphify_call(cfg, graph_root, args)


def print_status() -> None:
    cfg = load_config()
    ensure_graphify_version(cfg)
    if not status_all(cfg):
        raise WorkflowError("Graphify status is stale or unhealthy; run sync before editing/committing.")


def verify() -> None:
    print_status()
    print("Graphify verification passed.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pinned, local, code-only Graphify workflow.")
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "sync", "verify", "precommit", "install-hooks", "hooks-status", "unlock"):
        subs.add_parser(name)
    for name in ("query", "path", "explain"):
        p = subs.add_parser(name)
        p.add_argument("--root", default=None)
        p.add_argument("words", nargs=argparse.REMAINDER)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "status":
            print_status()
        elif args.command == "sync":
            sync()
        elif args.command == "verify":
            verify()
        elif args.command == "precommit":
            precommit()
        elif args.command == "install-hooks":
            install_hooks()
        elif args.command == "hooks-status":
            hook_status()
        elif args.command == "unlock":
            clear_stale_lock()
        else:
            words = [word for word in args.words if word != "--"]
            run_query(args.command, args.root, words)
        return 0
    except (WorkflowError, subprocess.TimeoutExpired) as exc:
        print(f"Graphify workflow error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
