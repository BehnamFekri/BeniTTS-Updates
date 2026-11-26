import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("graphify_workflow", HERE / "scripts" / "graphify_workflow.py")
workflow = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = workflow
SPEC.loader.exec_module(workflow)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.py").write_text("def value(): return 1\n", encoding="utf-8")
        (self.repo / "README.md").write_text("start\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        self.old_root = workflow.ROOT
        workflow.ROOT = self.repo
        self.cfg = {"exclude_paths": []}

    def tearDown(self):
        workflow.ROOT = self.old_root
        self.temp.cleanup()

    def test_fingerprint_changes_only_for_code(self):
        files = workflow.code_files(".", self.cfg)
        self.assertEqual(len(files), 1)
        before = workflow.fingerprint(files)
        (self.repo / "README.md").write_text("different docs\n", encoding="utf-8")
        self.assertEqual(before, workflow.fingerprint(workflow.code_files(".", self.cfg)))
        (self.repo / ".graphifyignore").write_text("oldfiles/\\n", encoding="utf-8")
        self.assertNotEqual(before, workflow.fingerprint(workflow.code_files(".", self.cfg)))
        (self.repo / "src" / "main.py").write_text("def value(): return 2\n", encoding="utf-8")
        self.assertNotEqual(before, workflow.fingerprint(workflow.code_files(".", self.cfg)))

    def test_excludes_generated_and_secret_files(self):
        self.assertTrue(workflow.is_excluded("node_modules/pkg/index.js", self.cfg))
        self.assertTrue(workflow.is_excluded(".env.production", self.cfg))
        self.assertTrue(workflow.is_excluded("local.properties", self.cfg))
        self.assertFalse(workflow.is_excluded("src/main.py", self.cfg))

    def test_lock_blocks_concurrent_operation(self):
        with workflow.RunLock():
            with self.assertRaises(workflow.WorkflowError):
                workflow.RunLock().__enter__()
        self.assertFalse((self.repo / ".git" / workflow.LOCK_NAME).exists())

    def test_dead_old_lock_can_be_recovered(self):
        lock = self.repo / ".git" / workflow.LOCK_NAME
        lock.mkdir()
        owner = {
            "pid": 2147483647,
            "started_utc": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        }
        (lock / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
        with mock.patch.object(workflow, "process_alive", return_value=False):
            workflow.clear_stale_lock()
        self.assertFalse(lock.exists())

    def test_detects_all_non_sample_hooks(self):
        hooks = self.repo / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        (hooks / "commit-msg").write_text("custom", encoding="utf-8")
        (hooks / "pre-push.sample").write_text("sample", encoding="utf-8")
        self.assertEqual(workflow.active_hooks(hooks), ["commit-msg"])

    def test_staged_code_must_match_worktree(self):
        (self.repo / "src" / "main.py").write_text("def value(): return 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "src/main.py"], cwd=self.repo, check=True)
        workflow.staged_code_consistency(self.cfg)
        (self.repo / "src" / "main.py").write_text("def value(): return 3\n", encoding="utf-8")
        with self.assertRaises(workflow.WorkflowError):
            workflow.staged_code_consistency(self.cfg)


    def test_unstaged_code_blocks_commit_consistency(self):
        (self.repo / "src" / "main.py").write_text("def value(): return 9\n", encoding="utf-8")
        with self.assertRaises(workflow.WorkflowError):
            workflow.staged_code_consistency(self.cfg)

if __name__ == "__main__":
    unittest.main()
