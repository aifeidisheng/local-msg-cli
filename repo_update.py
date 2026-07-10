"""Repository update detection and fast-forward self-update helpers."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass
class UpdateResult:
    status: str
    message: str
    branch: str = ""
    upstream: str = ""
    head_commit: str = ""
    upstream_commit: str = ""
    ahead: int = 0
    behind: int = 0
    remote: str = ""


def _repo_root(cwd: Optional[str] = None) -> str:
    return os.path.abspath(cwd or os.path.dirname(__file__))


def _run_git(args: Sequence[str], cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=_repo_root(cwd),
        capture_output=True,
        text=True,
    )


def _git_ok(result: subprocess.CompletedProcess) -> bool:
    return result.returncode == 0


def _stdout(result: subprocess.CompletedProcess) -> str:
    return (result.stdout or "").strip()


def _stderr(result: subprocess.CompletedProcess) -> str:
    return (result.stderr or "").strip()


def check_for_updates(cwd: Optional[str] = None) -> UpdateResult:
    inside = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=cwd)
    if not _git_ok(inside) or _stdout(inside) != "true":
        return UpdateResult("not_git_repo", "当前目录不是 git 仓库")

    dirty = _run_git(["status", "--porcelain"], cwd=cwd)
    if not _git_ok(dirty):
        return UpdateResult("status_failed", _stderr(dirty) or "无法读取 git 状态")
    if _stdout(dirty):
        return UpdateResult("dirty_worktree", "检测到未提交改动，已跳过自动更新")

    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    if not _git_ok(branch):
        return UpdateResult("branch_failed", _stderr(branch) or "无法识别当前分支")
    branch_name = _stdout(branch)
    if branch_name == "HEAD":
        return UpdateResult("detached_head", "当前处于 detached HEAD，拒绝自动更新")

    upstream = _run_git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        cwd=cwd,
    )
    if not _git_ok(upstream):
        return UpdateResult(
            "no_upstream",
            "当前分支未跟踪远端分支，拒绝自动更新",
            branch=branch_name,
        )
    upstream_name = _stdout(upstream)
    remote_name = upstream_name.split("/", 1)[0] if "/" in upstream_name else "origin"

    fetched = _run_git(["fetch", "--prune", remote_name], cwd=cwd)
    if not _git_ok(fetched):
        return UpdateResult(
            "fetch_failed",
            _stderr(fetched) or "刷新远端信息失败",
            branch=branch_name,
            upstream=upstream_name,
            remote=remote_name,
        )

    head_commit = _run_git(["rev-parse", "HEAD"], cwd=cwd)
    upstream_commit = _run_git(["rev-parse", "@{u}"], cwd=cwd)
    counts = _run_git(["rev-list", "--left-right", "--count", "HEAD...@{u}"], cwd=cwd)
    if not (_git_ok(head_commit) and _git_ok(upstream_commit) and _git_ok(counts)):
        return UpdateResult(
            "compare_failed",
            _stderr(counts) or _stderr(upstream_commit) or _stderr(head_commit) or "无法比较本地与远端提交",
            branch=branch_name,
            upstream=upstream_name,
            remote=remote_name,
        )

    parts = _stdout(counts).split()
    if len(parts) != 2:
        return UpdateResult(
            "compare_failed",
            f"无法解析提交差异: {_stdout(counts) or '<empty>'}",
            branch=branch_name,
            upstream=upstream_name,
            head_commit=_stdout(head_commit),
            upstream_commit=_stdout(upstream_commit),
            remote=remote_name,
        )

    ahead = int(parts[0])
    behind = int(parts[1])
    result = UpdateResult(
        "up_to_date",
        "当前代码已是最新",
        branch=branch_name,
        upstream=upstream_name,
        head_commit=_stdout(head_commit),
        upstream_commit=_stdout(upstream_commit),
        ahead=ahead,
        behind=behind,
        remote=remote_name,
    )

    if ahead == 0 and behind == 0:
        return result
    if ahead == 0 and behind > 0:
        result.status = "update_available"
        result.message = f"检测到 {behind} 个远端新提交，可执行 fast-forward 更新"
        return result
    if ahead > 0 and behind == 0:
        result.status = "ahead_of_remote"
        result.message = "本地分支领先远端，拒绝自动更新"
        return result

    result.status = "diverged"
    result.message = "本地与远端分支已分叉，拒绝自动更新"
    return result


def apply_updates(cwd: Optional[str] = None) -> UpdateResult:
    result = check_for_updates(cwd=cwd)
    if result.status != "update_available":
        return result

    pulled = _run_git(["pull", "--ff-only"], cwd=cwd)
    if not _git_ok(pulled):
        return UpdateResult(
            "pull_failed",
            _stderr(pulled) or "fast-forward 更新失败",
            branch=result.branch,
            upstream=result.upstream,
            head_commit=result.head_commit,
            upstream_commit=result.upstream_commit,
            ahead=result.ahead,
            behind=result.behind,
            remote=result.remote,
        )

    refreshed = check_for_updates(cwd=cwd)
    refreshed.status = "updated"
    refreshed.message = "已完成 fast-forward 更新"
    return refreshed


def format_update_report(result: UpdateResult) -> str:
    lines = [f"[update] {result.status}: {result.message}"]
    if result.branch:
        lines.append(f"  branch       = {result.branch}")
    if result.upstream:
        lines.append(f"  upstream     = {result.upstream}")
    if result.remote:
        lines.append(f"  remote       = {result.remote}")
    if result.head_commit:
        lines.append(f"  local_commit = {result.head_commit}")
    if result.upstream_commit:
        lines.append(f"  remote_commit= {result.upstream_commit}")
    if result.ahead or result.behind:
        lines.append(f"  ahead/behind = {result.ahead}/{result.behind}")
    return "\n".join(lines)
