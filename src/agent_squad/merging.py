"""Human-invoked, exact-head merge and verified, scoped cleanup."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile
from typing import Callable, Literal, TypedDict

from .commands import is_ancestor, state_for
from .conventions import SHA
from .forge import ForgeError, Forge
from .herdr import HerdrClient, HerdrError
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
MERGE_RECORD_FIELDS = frozenset({
    "schema_version", "common", "pr", "head", "head_branch", "base_branch",
    "merge_method", "moved_base", "issue",
})

FastForwardResult = TypedDict("FastForwardResult", {
    "result": Literal["fast-forwarded", "up to date", "skipped", "refused"],
    "from": str | None,
    "to": str | None,
    "reason": str | None,
    "command": str | None,
})


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
) -> tuple[Path, int]:
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
    return path, issue


def cleanup_command(pr: int) -> str:
    return f"agent-squad pr cleanup --as implementer --pr {pr}"


def merge_record_path(repository: Repository, pr: int) -> Path:
    # The common Git directory outlives every worktree and is never committed.
    return repository.common / f"agent-squad-merge-pr{pr}.json"


def write_merge_record(
    repository: Repository, pr: int, target: dict, method: str, moved: bool,
) -> dict:
    """Save the identities a later pr cleanup needs (#121).

    Only a validated ownership record supplies the issue number, and the
    implementation worktree that holds it is removed during cleanup.
    """
    _, issue = owned_implementation(
        repository, pr, target["head_branch"], target["head"]
    )
    record = {
        "schema_version": 1,
        "common": str(repository.common),
        "pr": pr,
        "head": target["head"],
        "head_branch": target["head_branch"],
        "base_branch": target["base_branch"],
        "merge_method": method,
        "moved_base": moved,
        "issue": issue,
    }
    path = merge_record_path(repository, pr)
    descriptor, temporary = tempfile.mkstemp(
        dir=repository.common, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record, stream)
        # Replaces an earlier record, or a symlink, without following it.
        os.replace(temporary, path)
    except OSError:
        Path(temporary).unlink(missing_ok=True)
        raise
    return record


def load_merge_record(repository: Repository, pr: int) -> dict:
    path = merge_record_path(repository, pr)
    if path.is_symlink():
        raise AgentSquadError(
            f"pr cleanup refused: merge record is a symlink: {path}"
        )
    if not path.exists():
        raise GateError(
            f"pr cleanup refused: no merge record for PR #{pr}; pr cleanup"
            " only finishes a merge started by pr merge"
        )
    try:
        record = decode_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AgentSquadError(
            f"pr cleanup refused: unreadable merge record: {path}: {error}"
        ) from None
    if not (
        isinstance(record, dict)
        and set(record) == MERGE_RECORD_FIELDS
        and type(record["schema_version"]) is int
        and record["schema_version"] == 1
        and type(record["pr"]) is int
        and isinstance(record["head"], str)
        and re.fullmatch(SHA, record["head"]) is not None
        and record["merge_method"] in ("merge", "squash")
        and type(record["moved_base"]) is bool
        and type(record["issue"]) is int
        and record["issue"] >= 1
        and all(
            isinstance(record[key], str) and record[key]
            and run_git(
                repository.primary, "check-ref-format",
                f"refs/heads/{record[key]}",
            ).returncode == 0
            for key in ("head_branch", "base_branch")
        )
    ):
        raise AgentSquadError(
            f"pr cleanup refused: malformed merge record: {path}"
        )
    if record["common"] != str(repository.common):
        raise AgentSquadError(
            "pr cleanup refused: merge record names another repository:"
            f" {record['common']}"
        )
    if record["pr"] != pr:
        raise AgentSquadError(
            f"pr cleanup refused: merge record names PR #{record['pr']}"
        )
    return record


def check_merge_gate(
    state: dict, *, accept_moved_base: bool, accept_merge_hold: bool = False,
) -> bool:
    reasons = list(state["approval"]["reasons"])
    if state["gates"]["unaddressed_findings"]:
        reasons.append(
            "unaddressed_findings: " + ", ".join(state["unaddressed_findings"])
        )
    if state["pr"]["state"] != "open" or state["pr"]["merged"]:
        reasons.append("PR is not open")
    hold = state.get("merge_hold")
    if hold and not accept_merge_hold:
        reasons.append(
            f'merge hold in review {hold["review_id"]}: {hold["text"]}; '
            "Developer review and explicit --accept-merge-hold required"
        )
    if reasons:
        raise GateError("pr merge refused: " + "; ".join(reasons))
    moved = state["target"]["base_tip"] != state["reviews"][-1]["base"]
    if moved and not accept_moved_base:
        raise GateError(
            "base branch moved since the approving review; merge the base"
            " into the PR branch and review the new head, or ask the"
            " Developer for explicit --accept-moved-base"
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
    repository: Repository, pr: int, head: str, branch: str, issue: int,
    forge: Forge,
) -> list[dict]:
    """Stop on a failed step; preserve unrelated or replaced resources.

    The identities come from the merge record, so a repeated run finishes
    after an earlier one removed the implementation worktree and its
    ownership record. A resource that is already gone counts as done.
    """
    steps: list[dict] = []

    def step(name: str, operation: Callable[[], object]) -> bool:
        try:
            detail = operation()
        except (AgentSquadError, OSError, ValueError) as error:
            steps.append({"step": name, "ok": False, "detail": str(error)})
            return False
        steps.append({"step": name, "ok": True, "detail": detail})
        return True

    root = repository.resolve_root(repository.configuration.worktree_root)
    implementation = root / f"issue-{issue}"

    def surviving_owner() -> Path | None:
        """Find an ownership record for this PR that outlived its checks.

        The record stays in the worktree's Git directory when the worktree
        is moved or its HEAD changes. Only a record that names another PR
        by a valid number is ignored; any other record may be this PR's.
        """
        admin = repository.common / "worktrees"
        if not admin.is_dir():
            return None
        for directory in sorted(admin.iterdir()):
            record = directory / IMPLEMENTATION_OWNER
            if not (record.exists() or record.is_symlink()):
                continue
            try:
                owner = (
                    None if record.is_symlink()
                    else decode_json(record.read_text())
                )
            except (OSError, ValueError):
                owner = None
            other = owner.get("pr") if isinstance(owner, dict) else None
            if not (type(other) is int and other >= 1 and other != pr):
                return record
        return None

    def registered() -> bool:
        """Whether the implementation worktree may still exist.

        It is present while a registered worktree has the recorded branch
        or sits at the issue path, and must then prove its ownership. A
        surviving ownership record for the PR refuses the step outright.
        """
        if any(
            w.branch == f"refs/heads/{branch}" or w.root == implementation
            for w in list_worktrees(repository.primary)
        ):
            return True
        survivor = surviving_owner()
        if survivor is not None:
            raise RetainedError(
                "implementation worktree moved or changed; its ownership"
                f" record remains: {survivor}"
            )
        return False

    def owned() -> Path:
        path, owner = owned_implementation(repository, pr, branch, head)
        if owner != issue:
            raise RetainedError(
                f"implementation ownership names issue #{owner}, the merge"
                f" record #{issue}: {path}"
            )
        return path

    # Check ownership before any deletion, then recheck at removal time.
    if not step("implementation ownership", lambda: (
        str(owned()) if registered() else "already removed"
    )):
        return steps

    def remove_remote() -> str:
        forge.delete_branch(branch, head)
        return f"confirmed absent: {branch}"

    if not step("remote branch", remove_remote):
        return steps

    def remove_tracking() -> str:
        # Recheck absence before the local compare-and-delete. A moved local
        # ref or a recreated remote branch must retain the remaining resources.
        if forge.branch_head(branch) is not None:
            raise RetainedError(f"remote branch remains: {branch}")
        ref = f"refs/remotes/origin/{branch}"
        current = run_git(
            repository.primary, "show-ref", "--verify", "--quiet", ref
        )
        if current.returncode == 1:
            return "already absent"
        if current.returncode:
            raise RetainedError(current.stderr.strip())
        git_output(
            repository.primary, "update-ref", "--no-deref", "-d", ref, head
        )
        if not run_git(
            repository.primary, "show-ref", "--verify", "--quiet", ref
        ).returncode:
            raise RetainedError(f"remote-tracking ref remains: {ref}")
        return ref

    if not step("remote-tracking ref", remove_tracking):
        return steps

    def review_worktrees() -> list:
        return [
            w for w in list_worktrees(repository.primary)
            if w.root.parent == root and re.fullmatch(
                rf"reviewer-pr{pr}-[0-9a-f]{{7}}", w.root.name
            )
        ]

    # Resolve the Implementer's session while the implementation worktree is
    # still registered: the Implementer may be working in it (§8.2). A refusal
    # is remembered by the client and fails the Reviewer step below. After an
    # earlier attempt removed that worktree, only its vacant path counts.
    vacant = not (implementation.exists() or implementation.is_symlink())
    client = HerdrClient(
        repository.primary, repository=repository,
        removed_worktrees=(implementation,) if vacant else (),
    )
    if review_worktrees():
        try:
            client.session()
        except HerdrError:
            pass

    def remove_implementation() -> str:
        if not registered():
            return "already absent"
        path = owned()
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
        present = run_git(
            repository.primary, "show-ref", "--verify", "--quiet", ref
        )
        if present.returncode not in (0, 1):
            raise RetainedError(present.stderr.strip())
        if present.returncode == 0:
            # Compare-and-delete preserves a branch that advanced during
            # cleanup, and works for squash, whose approved head is not a
            # merge ancestor. Git refuses it for an absent ref.
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
        return branch if present.returncode == 0 else "already absent"

    if not step("local branch", remove_branch):
        return steps
    # Paths identify candidates only; ReviewWorktree checks Git and Herdr
    # ownership before it removes any candidate.
    for worktree in review_worktrees():
        review = ReviewWorktree.for_pr(repository, pr, worktree.head)
        if review.path != worktree.root:
            steps.append({
                "step": "review worktree", "ok": False,
                "detail": f"review path/HEAD mismatch: {worktree.root}",
            })
            return steps
        if not step(
            "review worktree", lambda: close_reviewer(review, client)
        ):
            return steps

    def remove_scratch(scratch: Path) -> str:
        if scratch.is_symlink():
            raise RetainedError(f"scratch directory is a symlink: {scratch}")
        if scratch.exists():
            if any(w.root.is_relative_to(scratch) for w in list_worktrees(
                repository.primary
            )):
                raise RetainedError(f"scratch contains a worktree: {scratch}")
            shutil.rmtree(scratch)
        return str(scratch)

    scratch_root = repository.resolve_root(
        repository.configuration.scratch_root)
    if not step("scratch directory", lambda: remove_scratch(
        scratch_root / f"pr{pr}"
    )):
        return steps
    step("issue scratch directory", lambda: remove_scratch(
        scratch_root / f"issue-{issue}"
    ))
    return steps


def fast_forward_primary(
    primary: Path, base_branch: str, verified_tip: str,
    *, verified_branch: str | None = None,
) -> FastForwardResult:
    """Advance only a clean, checked-out base to the verified commit."""
    result: FastForwardResult = {
        "result": "skipped", "from": None, "to": verified_tip,
        "reason": None, "command": None,
    }
    try:
        result["from"] = git_output(primary, "rev-parse", "HEAD")
        if verified_branch is not None and verified_branch != base_branch:
            result["reason"] = (
                f"PR base branch {verified_branch} differs from "
                f"configured base branch {base_branch}"
            )
            return result
        branch = run_git(primary, "symbolic-ref", "--quiet", "HEAD")
        if branch.returncode == 1:
            result["reason"] = "primary checkout has a detached HEAD"
            return result
        if branch.returncode:
            raise AgentSquadError(branch.stderr.strip())
        if branch.stdout.strip() != f"refs/heads/{base_branch}":
            result["reason"] = (
                f"primary checkout is on {branch.stdout.strip()}, "
                f"not refs/heads/{base_branch}"
            )
            return result
        arguments = (
            "merge", "--ff-only", "--no-overwrite-ignore", verified_tip,
        )
        result["command"] = shlex.join(["git", "-C", str(primary), *arguments])
        if git_output(
            primary, "status", "--porcelain", "--untracked-files=no"
        ):
            result["reason"] = "primary checkout has changes to tracked files"
            return result
        # Once attempted, an interrupted merge must never be called skipped.
        result["result"] = "refused"
        merged = run_git(primary, *arguments)
        if merged.returncode:
            result["reason"] = merged.stderr.strip() or merged.stdout.strip()
            return result
        result["result"] = (
            "up to date" if git_output(primary, "rev-parse", "HEAD")
            == result["from"] else "fast-forwarded"
        )
        result["command"] = None
    except (AgentSquadError, OSError, ValueError) as error:
        # This best-effort step never changes the merge/cleanup exit status.
        result["reason"] = str(error)
        if result["result"] == "refused":
            try:
                if git_output(primary, "rev-parse", "HEAD") == verified_tip:
                    result["result"] = (
                        "up to date" if result["from"] == verified_tip
                        else "fast-forwarded"
                    )
                    result["command"] = None
            except (AgentSquadError, OSError, ValueError):
                # Keep the attempted result and original failure message.
                pass
    return result


def complete_merge(
    repository: Repository, forge: Forge, record: dict,
    merge_commit: str | None, result: dict,
) -> dict:
    """Run steps 3-5 of §7.10 from the merge record, then delete it."""
    pr = record["pr"]
    base = record["base_branch"]
    try:
        if merge_commit is None:
            raise AgentSquadError("the merged PR reports no merge commit")
        git_output(
            repository.primary, "fetch", "origin",
            f"+refs/heads/{base}:refs/remotes/origin/{base}",
        )
        tip = git_output(
            repository.primary, "rev-parse", f"refs/remotes/origin/{base}",
        )
        result["integration"] = verify_integration(
            repository.primary, record["merge_method"], record["head"],
            merge_commit, tip, moved_base=record["moved_base"],
        )
    except (AgentSquadError, OSError, ValueError) as error:
        return {
            **result, "exit_code": 1, "integration": str(error),
            "cleanup": [], "resources_retained": True,
            "cleanup_command": cleanup_command(pr),
        }
    try:
        result["cleanup"] = cleanup_merge(
            repository, pr, record["head"], record["head_branch"],
            record["issue"], forge,
        )
    except (AgentSquadError, OSError, ValueError) as error:
        result["cleanup"] = [{
            "step": "cleanup inventory", "ok": False, "detail": str(error),
        }]
    result["fast_forward"] = fast_forward_primary(
        repository.primary, repository.configuration.base_branch, tip,
        verified_branch=base,
    )
    complete = all(s["ok"] for s in result["cleanup"])
    if complete:
        try:
            merge_record_path(repository, pr).unlink(missing_ok=True)
            result["merge_record"]["result"] = "deleted"
        except OSError as error:
            complete = False
            result["merge_record"]["reason"] = str(error)
    result["exit_code"] = 0 if complete else 3
    if not complete:
        result["cleanup_command"] = cleanup_command(pr)
    return result


def merge_pr(
    repository: Repository, forge: Forge, pr: int,
    *, accept_moved_base: bool = False, accept_merge_hold: bool = False,
) -> dict:
    snapshot = forge.snapshot(pr)
    rules = (
        forge.branch_rules(snapshot.pr.base_branch)
        if forge.can_read_branch_rules else {"visibility": "not visible"}
    )
    # Re-read authority after the potentially slow rules lookup.
    snapshot = forge.snapshot(pr)
    state = state_for(repository, snapshot)
    try:
        moved = check_merge_gate(
            state, accept_moved_base=accept_moved_base,
            accept_merge_hold=accept_merge_hold,
        )
    except GateError as error:
        path = merge_record_path(repository, pr)
        if state["pr"]["merged"] and (path.exists() or path.is_symlink()):
            raise GateError(
                f"{error}; to finish the merge pr merge started, run"
                f" {cleanup_command(pr)}"
            ) from None
        raise
    target = state["target"]
    method = repository.configuration.merge_method
    result = {
        "pr": pr, "head": target["head"], "merge_method": method,
        "moved_base": moved, "mergeable_state": snapshot.pr.mergeable_state,
        "branch_rules": rules,
        "fast_forward": {
            "result": "skipped", "from": None, "to": None,
            "reason": "merge integration has not been verified",
            "command": None,
        },
        "merge_record": {
            "path": str(merge_record_path(repository, pr)),
            "result": "not written", "reason": None,
        },
        "cleanup_command": None,
    }
    try:
        record = write_merge_record(repository, pr, target, method, moved)
    except (AgentSquadError, OSError, ValueError) as error:
        result["merge_record"]["reason"] = str(error)
        return {
            **result, "exit_code": 1, "merged": False,
            "error": (
                f"no merge request sent: merge record not written: {error}"
            ),
            "resources_retained": True,
        }
    result["merge_record"]["result"] = "kept"
    try:
        merged = forge.merge(pr, target["head"], method)
    except AgentSquadError as error:
        refused = isinstance(error, ForgeError) and error.status in (
            400, 401, 403, 404, 405, 409, 422,
        )
        if refused:
            try:
                merge_record_path(repository, pr).unlink(missing_ok=True)
                result["merge_record"]["result"] = "deleted"
            except OSError as unlink_error:
                result["merge_record"]["reason"] = str(unlink_error)
        else:
            result["cleanup_command"] = cleanup_command(pr)
        return {
            **result, "exit_code": 1,
            "merged": False if refused else None, "error": str(error),
            "resources_retained": True,
        }
    result.update(
        merged=True, merge_commit=merged["sha"], message=merged["message"])
    return complete_merge(repository, forge, record, merged["sha"], result)


def cleanup_pr(repository: Repository, forge: Forge, pr: int) -> dict:
    """Finish a merge that pr merge started; never send a merge request."""
    record = load_merge_record(repository, pr)
    merged = forge.pr(pr)
    if not merged.merged:
        raise GateError(
            f"pr cleanup refused: PR #{pr} is not merged; merge record kept:"
            f" {merge_record_path(repository, pr)}"
        )
    # Only the merge commit comes from the forge: a merged Forgejo PR can
    # report a synthetic head branch and an older head (E9).
    return complete_merge(repository, forge, record, merged.merge_commit, {
        "pr": pr, "head": record["head"],
        "merge_method": record["merge_method"],
        "moved_base": record["moved_base"], "merged": True,
        "merge_commit": merged.merge_commit,
        "fast_forward": {
            "result": "skipped", "from": None, "to": None,
            "reason": "merge integration has not been verified",
            "command": None,
        },
        "merge_record": {
            "path": str(merge_record_path(repository, pr)),
            "result": "kept", "reason": None,
        },
        "cleanup_command": None,
    })
