"""Human-invoked, exact-head merge and verified, scoped cleanup."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import shutil
from typing import Callable

from .commands import is_ancestor, state_for
from .forge import ForgeError, GitHub
from .herdr import HerdrClient
from .initialization import (
    AgentSquadError,
    GateError,
    Repository,
    RetainedError,
    decode_json,
    git_output,
    list_worktrees,
    run_git,
)
from .reviewer import ReviewWorktree, close_reviewer

IMPLEMENTATION_OWNER = "agent-squad-implementation.json"


def implementation_metadata(
    repository: Repository, path: Path, branch: str
) -> Path:
    """Prove a linked checkout's branch and Git directory identity."""
    worktrees = list_worktrees(repository.primary)
    if (
        path == repository.primary
        or path.is_symlink()
        or branch == repository.configuration.base_branch
        or not any(
            w.root == path and w.branch == f"refs/heads/{branch}"
            for w in worktrees
        )
    ):
        raise RetainedError(f"not the implementation linked worktree: {path}")
    admin = Path(git_output(path, "rev-parse", "--absolute-git-dir"))
    common = Path(git_output(
        path, "rev-parse", "--path-format=absolute", "--git-common-dir"
    ))
    if (
        common.resolve() != repository.common
        or admin.parent.resolve() != repository.common / "worktrees"
        or admin.is_symlink()
        or (admin / "gitdir").is_symlink()
        or Path((admin / "gitdir").read_text().strip()) != path / ".git"
    ):
        raise RetainedError(f"implementation Git identity differs: {path}")
    return admin / IMPLEMENTATION_OWNER


def implementation_identity(
    repository: Repository, issue: int, branch: str
) -> tuple[Path, dict]:
    path = repository.resolve_root(
        repository.configuration.worktree_root
    ) / f"issue-{issue}"
    if repository.root != path:
        raise RetainedError(f"run pr create from the issue worktree: {path}")
    metadata = implementation_metadata(repository, path, branch)
    identity = {
        "schema_version": 1,
        "path": str(path),
        "common": str(repository.common),
        "branch": branch,
        "issue": issue,
    }
    if metadata.is_symlink():
        raise RetainedError(f"implementation ownership is a symlink: {path}")
    if metadata.exists():
        owner = decode_json(metadata.read_text())
        if not isinstance(owner, dict) or owner != {
            **identity, "pr": owner.get("pr")
        }:
            raise RetainedError(f"implementation ownership differs: {path}")
    return metadata, identity


def record_implementation(metadata: Path, identity: dict, pr: int) -> None:
    """Record resources designated by the Implementer through pr create."""
    owner = {**identity, "pr": pr}
    if metadata.exists() or metadata.is_symlink():
        if metadata.is_symlink() or decode_json(metadata.read_text()) != owner:
            raise RetainedError(
                f"PR #{pr} created; ownership record differs: {metadata}"
            )
        return
    with metadata.open("x", encoding="utf-8") as stream:
        json.dump(owner, stream)


def owned_implementation(
    repository: Repository, pr: int, branch: str, head: str
) -> Path:
    worktree = next((
        w for w in list_worktrees(repository.primary)
        if w.branch == f"refs/heads/{branch}"
    ), None)
    if worktree is None or worktree.head != head:
        raise RetainedError("implementation branch or HEAD changed")
    path = worktree.root
    metadata = implementation_metadata(repository, path, branch)
    try:
        if metadata.is_symlink():
            raise ValueError("ownership record is a symlink")
        owner = decode_json(metadata.read_text())
        issue = owner.get("issue") if isinstance(owner, dict) else None
        root = repository.resolve_root(repository.configuration.worktree_root)
        if (
            type(issue) is not int
            or issue < 1
            or path != root / f"issue-{issue}"
            or owner != {
                "schema_version": 1,
                "path": str(path),
                "common": str(repository.common),
                "branch": branch,
                "issue": issue,
                "pr": pr,
            }
        ):
            raise ValueError("ownership record does not match the PR")
    except (OSError, ValueError) as error:
        raise RetainedError(
            f"cannot prove implementation ownership: {path}: {error}"
        ) from None
    return path


def check_merge_gate(state: dict, *, accept_moved_base: bool) -> bool:
    reasons = list(state["approval"]["reasons"])
    if state["gates"]["unaddressed_findings"]:
        reasons.append(
            "unaddressed_findings: " + ", ".join(state["unaddressed_findings"])
        )
    if state["pr"]["state"] != "open" or state["pr"]["merged"]:
        reasons.append("PR is not open")
    if reasons:
        raise GateError("pr merge refused: " + "; ".join(reasons))
    moved = state["target"]["base_tip"] != state["reviews"][-1]["base"]
    if moved and not accept_moved_base:
        raise GateError(
            "base branch moved since the approving review; ask the Developer"
            " for a new review or explicit --accept-moved-base"
        )
    return moved


def verify_integration(
    root: Path, method: str, head: str, merge_commit: str, base_tip: str,
    *, moved_base: bool,
) -> str:
    if not is_ancestor(root, merge_commit, base_tip):
        raise AgentSquadError("merge commit is not an ancestor of base branch")
    if method == "merge":
        if not is_ancestor(root, head, merge_commit):
            raise AgentSquadError("approved head is not in merge ancestry")
        return "verified by ancestry"
    if method != "squash":
        raise AgentSquadError(f"unsupported merge method: {method}")
    if moved_base:
        raise AgentSquadError("integration not verifiable by tree identity")
    if git_output(root, "rev-parse", f"{merge_commit}^{{tree}}") != git_output(
        root, "rev-parse", f"{head}^{{tree}}"
    ):
        raise AgentSquadError("squash merge tree differs from approved head")
    return "verified by tree identity"


def cleanup_merge(
    repository: Repository, pr: int, head: str, branch: str
) -> list[dict]:
    """Stop on a failed step; preserve unrelated or replaced resources."""
    steps: list[dict] = []

    def step(name: str, operation: Callable[[], object]) -> bool:
        try:
            detail = operation()
        except (AgentSquadError, OSError, ValueError) as error:
            steps.append({"step": name, "ok": False, "detail": str(error)})
            return False
        steps.append({"step": name, "ok": True, "detail": detail})
        return True

    # Check ownership before any deletion, then recheck at removal time.
    if not step("implementation ownership", lambda: str(
        owned_implementation(repository, pr, branch, head)
    )):
        return steps

    def remove_remote() -> str:
        ref = f"refs/heads/{branch}"
        present = git_output(
            repository.primary, "ls-remote", "--heads", "origin", ref
        )
        if not present:
            return "already removed by forge"
        if present.split()[0] != head:
            raise RetainedError(
                "remote branch no longer matches approved head")
        git_output(
            repository.primary, "push", "origin",
            f"--force-with-lease={ref}:{head}", f":{ref}",
        )
        if git_output(
            repository.primary, "ls-remote", "--heads", "origin", ref
        ):
            raise RetainedError(f"remote branch remains: {branch}")
        return branch

    if not step("remote branch", remove_remote):
        return steps

    def remove_implementation() -> str:
        path = owned_implementation(repository, pr, branch, head)
        # Preserve uncommitted implementation work, including untracked data.
        git_output(repository.primary, "worktree", "remove", str(path))
        if path.exists() or any(
            w.root == path for w in list_worktrees(repository.primary)
        ):
            raise RetainedError(f"implementation worktree remains: {path}")
        return str(path)

    if not step("implementation worktree", remove_implementation):
        return steps

    def remove_branch() -> str:
        ref = f"refs/heads/{branch}"
        # Compare-and-delete preserves a branch that advanced during cleanup,
        # and works for squash, whose approved head is not a merge ancestor.
        git_output(repository.primary, "update-ref", "-d", ref, head)
        if not run_git(
            repository.primary, "show-ref", "--verify", "--quiet", ref
        ).returncode:
            raise RetainedError(f"local branch remains: {branch}")
        removed = run_git(
            repository.primary, "config", "--remove-section",
            f"branch.{branch}",
        )
        if removed.returncode and "no such section" not in removed.stderr:
            raise RetainedError(removed.stderr.strip())
        return branch

    if not step("local branch", remove_branch):
        return steps
    root = repository.resolve_root(repository.configuration.worktree_root)
    # Paths identify candidates only; ReviewWorktree checks Git and Herdr
    # ownership before it removes any candidate.
    for worktree in list_worktrees(repository.primary):
        if worktree.root.parent == root and re.fullmatch(
            rf"reviewer-pr{pr}-[0-9a-f]{{7}}", worktree.root.name
        ):
            review = ReviewWorktree.for_pr(repository, pr, worktree.head)
            if review.path != worktree.root:
                steps.append({
                    "step": "review worktree", "ok": False,
                    "detail": f"review path/HEAD mismatch: {worktree.root}",
                })
                return steps
            if not step("review worktree", lambda: close_reviewer(
                review, HerdrClient(repository.primary)
            )):
                return steps

    def remove_scratch() -> str:
        scratch = repository.resolve_root(
            repository.configuration.scratch_root
        ) / f"pr{pr}"
        if scratch.is_symlink():
            raise RetainedError(f"scratch directory is a symlink: {scratch}")
        if scratch.exists():
            if any(w.root.is_relative_to(scratch) for w in list_worktrees(
                repository.primary
            )):
                raise RetainedError(f"scratch contains a worktree: {scratch}")
            shutil.rmtree(scratch)
        return str(scratch)

    step("scratch directory", remove_scratch)
    return steps


def merge_pr(
    repository: Repository, forge: GitHub, pr: int,
    *, accept_moved_base: bool = False,
) -> dict:
    snapshot = forge.snapshot(pr)
    rules = forge.branch_rules(snapshot.pr.base_branch)
    # Re-read authority after the potentially slow rules lookup.
    snapshot = forge.snapshot(pr)
    state = state_for(repository, snapshot)
    moved = check_merge_gate(state, accept_moved_base=accept_moved_base)
    target = state["target"]
    method = repository.configuration.merge_method
    result = {
        "pr": pr, "head": target["head"], "merge_method": method,
        "moved_base": moved, "mergeable_state": snapshot.pr.mergeable_state,
        "branch_rules": rules,
        "fast_forward_command": shlex.join([
            "git", "-C", str(repository.primary), "merge", "--ff-only",
            f"origin/{target['base_branch']}",
        ]),
    }
    try:
        merged = forge.merge(pr, target["head"], method)
    except AgentSquadError as error:
        refused = isinstance(error, ForgeError) and error.status in (
            400, 401, 403, 404, 405, 409, 422,
        )
        return {
            **result, "exit_code": 1,
            "merged": False if refused else None, "error": str(error),
            "resources_retained": True,
        }
    result.update(
        merged=True, merge_commit=merged["sha"], message=merged["message"])
    try:
        git_output(
            repository.primary, "fetch", "origin",
            f"+refs/heads/{target['base_branch']}:"
            f"refs/remotes/origin/{target['base_branch']}",
        )
        tip = git_output(
            repository.primary, "rev-parse",
            f"refs/remotes/origin/{target['base_branch']}",
        )
        result["integration"] = verify_integration(
            repository.primary, method, target["head"], merged["sha"], tip,
            moved_base=moved,
        )
    except (AgentSquadError, OSError, ValueError) as error:
        return {
            **result, "exit_code": 1, "integration": str(error),
            "cleanup": [], "resources_retained": True,
        }
    try:
        result["cleanup"] = cleanup_merge(
            repository, pr, target["head"], target["head_branch"]
        )
    except (AgentSquadError, OSError, ValueError) as error:
        result["cleanup"] = [{
            "step": "cleanup inventory", "ok": False, "detail": str(error),
        }]
    result["exit_code"] = (
        0 if all(s["ok"] for s in result["cleanup"]) else 3
    )
    return result
