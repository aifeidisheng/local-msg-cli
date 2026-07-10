import unittest
from unittest.mock import patch

import repo_update


class FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RepoUpdateTests(unittest.TestCase):
    def test_check_for_updates_detects_remote_ahead(self):
        results = iter([
            FakeCompletedProcess(stdout="true\n"),
            FakeCompletedProcess(stdout=""),
            FakeCompletedProcess(stdout="main\n"),
            FakeCompletedProcess(stdout="origin/main\n"),
            FakeCompletedProcess(stdout=""),
            FakeCompletedProcess(stdout="abc123\n"),
            FakeCompletedProcess(stdout="def456\n"),
            FakeCompletedProcess(stdout="0 2\n"),
        ])

        with patch("repo_update._run_git", side_effect=lambda *args, **kwargs: next(results)):
            result = repo_update.check_for_updates()

        self.assertEqual(result.status, "update_available")
        self.assertEqual(result.behind, 2)
        self.assertEqual(result.branch, "main")
        self.assertEqual(result.upstream, "origin/main")

    def test_check_for_updates_blocks_dirty_worktree(self):
        results = iter([
            FakeCompletedProcess(stdout="true\n"),
            FakeCompletedProcess(stdout=" M README.md\n"),
        ])

        with patch("repo_update._run_git", side_effect=lambda *args, **kwargs: next(results)):
            result = repo_update.check_for_updates()

        self.assertEqual(result.status, "dirty_worktree")

    def test_check_for_updates_detects_diverged_branch(self):
        results = iter([
            FakeCompletedProcess(stdout="true\n"),
            FakeCompletedProcess(stdout=""),
            FakeCompletedProcess(stdout="main\n"),
            FakeCompletedProcess(stdout="origin/main\n"),
            FakeCompletedProcess(stdout=""),
            FakeCompletedProcess(stdout="abc123\n"),
            FakeCompletedProcess(stdout="def456\n"),
            FakeCompletedProcess(stdout="1 3\n"),
        ])

        with patch("repo_update._run_git", side_effect=lambda *args, **kwargs: next(results)):
            result = repo_update.check_for_updates()

        self.assertEqual(result.status, "diverged")
        self.assertEqual(result.ahead, 1)
        self.assertEqual(result.behind, 3)

    def test_apply_updates_runs_fast_forward_pull(self):
        check_result = repo_update.UpdateResult(
            status="update_available",
            message="pending",
            branch="main",
            upstream="origin/main",
            head_commit="abc123",
            upstream_commit="def456",
            ahead=0,
            behind=1,
            remote="origin",
        )
        refreshed = repo_update.UpdateResult(
            status="up_to_date",
            message="latest",
            branch="main",
            upstream="origin/main",
            head_commit="def456",
            upstream_commit="def456",
            remote="origin",
        )

        with patch("repo_update.check_for_updates", side_effect=[check_result, refreshed]), \
             patch("repo_update._run_git", return_value=FakeCompletedProcess(stdout="Updating\n")) as run_git:
            result = repo_update.apply_updates()

        self.assertEqual(result.status, "updated")
        self.assertEqual(result.head_commit, "def456")
        run_git.assert_called_once_with(["pull", "--ff-only"], cwd=None)


if __name__ == "__main__":
    unittest.main()
