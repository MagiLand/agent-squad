"""Human-invoked, exact-head merge and verified, scoped cleanup."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile
import uuid
from typing import Callable, Literal, TypedDict

from .commands import is_ancestor, state_for
from .conventions import SHA, approved_review_head, closes_issue
from .forge import ForgeError, Forge, MergeRefused, PullRequest
from .herdr import HerdrClient, HerdrError
from .initialization import (
    AgentSquadError,
    GateError,
    Repository,
    RetainedError,
    Worktree,
    decode_json,
    git_output,
    list_worktrees,
    run_git,
)
from .reviewer import ReviewWorktree, close_reviewer

IMPLEMENTATION_OWNER = "agent-squad-implementation.json"
# A file in the proved implementation Git directory that names one
# occurrence of the checkout for a retried cleanup (#175).
CLEANUP_MARKER = "agent-squad-cleanup-"
DIRECTORY_FLAGS = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
)
MARKER_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
)
MERGE_RECORD_FIELDS = frozenset({
    "schema_version", "common", "pr", "head", "head_branch", "base_branch",
    "merge_method", "moved_base", "issue", "outside_pr_merge",
    "implementation_record",
})
# An earlier version wrote neither field; the record reads as one pr merge
# wrote (#175).
EARLIER_MERGE_RECORD_FIELDS = MERGE_RECORD_FIELDS - {
    "outside_pr_merge", "implementation_record",
}
# The method each integration check proves when no method is known (#175).
PROVED_METHODS = {
    "verified by ancestry": "merge",
    "verified by tree identity": "squash",
}

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
    if branch == repository.configuration.base_branch or not any(
        w.root == path and w.branch == f"refs/heads/{branch}"
        for w in worktrees
    ):
        raise RetainedError(f"not the implementation linked worktree: {path}")
    return linked_record(repository, path, worktrees)


def linked_record(
    repository: Repository, path: Path,
    worktrees: tuple[Worktree, ...] | None = None,
) -> Path:
    """Prove a registered linked checkout's Git directory identity, whatever
    it has checked out, and name its implementation record path."""
    if worktrees is None:
        worktrees = list_worktrees(repository.primary)
    if (
        path == repository.primary
        or path.is_symlink()
        or not any(w.root == path for w in worktrees)
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


def recorded_pr(repository: Repository, issue: int, branch: str) -> int | None:
    """The PR this worktree's implementation record names, or None when
    there is no such record or it is not this worktree's (#174)."""
    try:
        metadata, _ = implementation_identity(repository, issue, branch)
        if not metadata.exists():
            return None
        pr = decode_json(metadata.read_text()).get("pr")
    except (RetainedError, OSError, ValueError):
        return None
    return pr if type(pr) is int else None


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
            # The comparison below would take True for 1 (#175 REV-4).
            or type(owner.get("pr")) is not int
            or type(owner.get("schema_version")) is not int
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


def ownership_records(repository: Repository, pr: int) -> list[Path]:
    """Every implementation ownership record that may be this PR's.

    The record stays in the worktree's Git directory when the worktree is
    moved or its HEAD changes. Only a readable record that names another PR
    by a valid number is left out; any other record may be this PR's.
    """
    admin = repository.common / "worktrees"
    if not entry_remains(admin):
        return []
    records = []
    for directory in sorted(admin.iterdir()):
        record = directory / IMPLEMENTATION_OWNER
        if not entry_remains(record):
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
            records.append(record)
    return records


def named_implementations(
    repository: Repository, pr: int,
) -> list[tuple[Worktree, dict, Path]]:
    """Registered linked worktrees under ``worktree_root`` whose
    implementation record names ``pr`` by an integer (#175).

    A worktree counts whatever it has checked out, a detached HEAD included:
    the branch is checked only for the single candidate. A worktree whose
    Git identity cannot be proved, or whose record is a symlink,
    unreadable, or names another PR, is not a candidate.
    """
    root = repository.resolve_root(repository.configuration.worktree_root)
    worktrees = list_worktrees(repository.primary)
    found = []
    for worktree in worktrees:
        if worktree.root.parent != root:
            continue
        try:
            record = linked_record(repository, worktree.root, worktrees)
            if record.is_symlink() or not record.exists():
                continue
            owner = decode_json(record.read_text())
        except (AgentSquadError, OSError, ValueError):
            continue
        if (
            isinstance(owner, dict)
            and type(owner.get("pr")) is int
            and owner["pr"] == pr
        ):
            found.append((worktree, owner, record))
    return found


def same_directory(descriptor: int, path: Path) -> bool:
    """Whether ``path`` still names the directory ``descriptor`` holds open.

    While the descriptor is open, the file system cannot give that
    directory's identity to another one, so equal identities mean the same
    directory, not a later one at the same place.
    """
    try:
        return os.path.samestat(
            os.fstat(descriptor), os.stat(path, follow_symlinks=False)
        )
    except OSError:
        return False


def create_cleanup_marker(descriptor: int | None, directory: Path) -> Path:
    """Create a new, empty marker in the Git directory that ``descriptor``
    holds open since its ownership was proved, and return its path.

    A worktree removed and created again at the same place gets a new Git
    directory under the old name (#175). The marker is therefore created
    through the descriptor, never by opening the name again, and
    ``directory`` must name the proved directory before and after: the
    replacement must neither receive the marker nor be named by a record.
    """
    changed = "implementation Git directory changed"
    if descriptor is None or not same_directory(descriptor, directory):
        raise RetainedError(changed)
    name = f"{CLEANUP_MARKER}{uuid.uuid4().hex}"
    os.close(os.open(name, MARKER_FLAGS, 0o600, dir_fd=descriptor))
    if not same_directory(descriptor, directory):
        remove_cleanup_marker(descriptor, name)
        raise RetainedError(changed)
    return directory / name


def remove_cleanup_marker(descriptor: int, name: str) -> None:
    """Take back a marker that no merge record names."""
    try:
        os.unlink(name, dir_fd=descriptor)
    except OSError:
        pass


def forge_merged_record(
    repository: Repository, pr: int, merged: PullRequest,
) -> tuple[dict, int]:
    """Prove that a PR the forge merged without pr merge is the loop's own.

    Returns the record and an open descriptor of the proved implementation
    Git directory. The caller keeps it open until the cleanup marker is
    created and then closes it.

    The implementation record supplies the issue and head branch, and the
    worktree's HEAD is the head. The forge's head and head branch are not
    trusted, because a merged Forgejo PR can report a synthetic head branch
    and an older head (E9). The method stays unknown until the integration
    check proves it (#175).
    """
    refused = f"pr cleanup refused: no merge record for PR #{pr}"
    base = repository.configuration.base_branch
    if merged.merge_commit is None:
        raise GateError(f"{refused}; the merged PR reports no merge commit")
    if merged.base_branch != base:
        raise GateError(
            f"{refused}; its base branch {merged.base_branch} is not the"
            f" configured base branch {base}"
        )
    candidates = named_implementations(repository, pr)
    if len(candidates) != 1:
        found = (
            "no implementation record names" if not candidates
            else f"{len(candidates)} implementation records name"
        )
        raise GateError(
            f"{refused}; {found} PR #{pr} under the worktree root, and"
            " pr cleanup finishes a merge made outside pr merge only when"
            " exactly one names it"
        )
    worktree, owner, owner_record = candidates[0]
    directory = owner_record.parent
    descriptor = None
    try:
        # Opened before the proof and compared after it, so the proof is
        # about the directory that the descriptor holds.
        descriptor = os.open(directory, DIRECTORY_FLAGS)
        branch = owner.get("branch")
        if not isinstance(branch, str) or not branch:
            raise RetainedError(
                f"cannot prove implementation ownership: {worktree.root}"
            )
        # The checked worktree must be the counted candidate itself.
        path, issue = owned_implementation(
            repository, pr, branch, worktree.head
        )
        if path != worktree.root:
            raise RetainedError("implementation branch or HEAD changed")
        if linked_record(repository, path).parent != directory or (
            not same_directory(descriptor, directory)
        ):
            raise RetainedError("implementation Git directory changed")
    except (AgentSquadError, OSError, ValueError) as error:
        if descriptor is not None:
            os.close(descriptor)
        raise GateError(f"{refused}; {error}") from None
    return {
        "schema_version": 1,
        "common": str(repository.common),
        "pr": pr,
        "head": worktree.head,
        "head_branch": branch,
        "base_branch": base,
        "merge_method": None,
        "moved_base": False,
        "issue": issue,
        "outside_pr_merge": True,
        # The proved checkout's Git directory; complete_merge replaces it
        # with a marker that only this occurrence of the checkout holds.
        "implementation_record": str(directory.resolve()),
    }, descriptor


def file_name_text(value: object) -> bool:
    """Whether a saved value is text that can name a file system entry or
    be passed to Git: a non-empty string without a NUL that encodes."""
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    try:
        os.fsencode(value)
    except ValueError:
        return False
    return True


def marker_in_worktree_slot(repository: Repository, marker: object) -> bool:
    """Whether a saved value names a cleanup marker directly in one worktree
    directory of the common Git directory.

    The text itself is checked, before any path is made from it: a path
    object drops ``.`` components and repeated separators, and a path with
    a NUL only reads as one that does not exist. The value must be exactly
    ``<common>/worktrees/<one name>/<marker>`` (#175).
    """
    if not file_name_text(marker):
        return False
    prefix = f"{repository.common}/worktrees/"
    if not marker.startswith(prefix):
        return False
    slot, separator, name = marker[len(prefix):].partition("/")
    return (
        separator == "/"
        and slot not in ("", ".", "..")
        and re.fullmatch(rf"{CLEANUP_MARKER}[0-9a-f]{{32}}", name) is not None
    )


def entry_remains(path: Path) -> bool:
    """Whether a cleanup marker, an ownership record, or the directory
    that holds them is still there. Only a name that is absent means that
    it is gone; any other failure to read it raises, so that the step
    fails instead of taking the checkout for removed (#175)."""
    try:
        os.lstat(path)
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ENOTDIR):
            return False
        raise
    return True


def merge_record_path(repository: Repository, pr: int) -> Path:
    # The common Git directory outlives every worktree and is never committed.
    return repository.common / f"agent-squad-merge-pr{pr}.json"


def write_merge_record(
    repository: Repository, pr: int, target: dict, method: str, moved: bool,
    *, outside: bool = False, implementation_record: str | None = None,
) -> dict:
    """Save the identities a later pr cleanup needs (#121).

    Only a validated ownership record supplies the issue number, and the
    implementation worktree that holds it is removed during cleanup.
    ``outside`` marks a merge the forge made without pr merge, and
    ``implementation_record`` names the ownership record proved then
    (#175).
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
        "outside_pr_merge": outside,
        "implementation_record": implementation_record,
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


def load_merge_record(repository: Repository, pr: int) -> dict | None:
    """The merge record for ``pr``, or None when there is none."""
    path = merge_record_path(repository, pr)
    if path.is_symlink():
        raise AgentSquadError(
            f"pr cleanup refused: merge record is a symlink: {path}"
        )
    if not path.exists():
        return None
    try:
        record = decode_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AgentSquadError(
            f"pr cleanup refused: unreadable merge record: {path}: {error}"
        ) from None
    if isinstance(record, dict) and set(record) == EARLIER_MERGE_RECORD_FIELDS:
        record = {
            **record, "outside_pr_merge": False,
            "implementation_record": None,
        }
    if not (
        isinstance(record, dict)
        and set(record) == MERGE_RECORD_FIELDS
        and type(record["outside_pr_merge"]) is bool
        and (
            record["implementation_record"] is None
            if not record["outside_pr_merge"]
            else marker_in_worktree_slot(
                repository, record["implementation_record"]
            )
        )
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
            file_name_text(record[key])
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
    elif hold and not hold["released"]:
        reasons.append(
            f'merge hold in review {hold["review_id"]}: {hold["text"]}; '
            "--accept-merge-hold requires a hold-release decision newer than"
            " that review: record the Developer's release with decision post"
            " --finding none --kind hold-release --recommendation"
            " <followed|changed|none>"
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


def parents(root: Path, commit: str) -> list[str]:
    line = git_output(root, "rev-list", "--parents", "-n", "1", commit)
    return line.split()[1:]


def verify_forge_merge(
    root: Path, head: str, branch: str, merge_commit: str, base_tip: str,
) -> str:
    """Prove that a merge the forge made without pr merge holds ``head``.

    Ancestry suffices. A matching tree alone also arises after a rebase,
    a base advance that changed no files, or an unpushed local commit that
    changes none, so tree identity counts only when the head is the last
    pushed head and the merge commit's one parent is an ancestor of the
    head: a squash onto a base that had not moved (Developer, #175).
    """
    if not is_ancestor(root, merge_commit, base_tip):
        raise AgentSquadError("merge commit is not an ancestor of base branch")
    if is_ancestor(root, head, merge_commit):
        return "verified by ancestry"
    if git_output(root, "rev-parse", f"{merge_commit}^{{tree}}") != (
        git_output(root, "rev-parse", f"{head}^{{tree}}")
    ):
        raise AgentSquadError(
            "merge commit contains neither the local head in its ancestry"
            " nor its tree"
        )
    pushed = run_git(
        root, "rev-parse", "--verify", "--quiet",
        f"refs/remotes/origin/{branch}^{{commit}}",
    )
    if pushed.returncode or pushed.stdout.strip() != head:
        raise AgentSquadError(
            "squash merge not verifiable: the local head is not the last"
            f" pushed head refs/remotes/origin/{branch}"
        )
    above = parents(root, merge_commit)
    if len(above) != 1 or not is_ancestor(root, above[0], head):
        raise AgentSquadError(
            "squash merge not verifiable: the merge commit's parent is not"
            " an ancestor of the local head"
        )
    return "verified by tree identity"


def merged_source(
    root: Path, head: str, merge_commit: str, integration: str,
) -> str | None:
    """The source a verified merge commit is proved to have integrated."""
    above = parents(root, merge_commit)
    if len(above) == 2:
        return above[1]
    if integration == "verified by tree identity" or merge_commit == head:
        return head
    return None


def cleanup_merge(
    repository: Repository, pr: int, head: str, branch: str, issue: int,
    forge: Forge, *, cleanup_marker: Path | None = None,
) -> list[dict]:
    """Stop on a failed step; preserve unrelated or replaced resources.

    ``cleanup_marker`` is the marker left in the proved implementation Git
    directory for a merge made outside pr merge (#175). A registered
    implementation worktree must hold it in its own Git directory. After
    the worktree is gone, only that marker surviving, which means the
    original checkout was moved, refuses the step; other records neither
    block nor authorize that cleanup. Without it, any record that may be
    the PR's refuses.

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
        if cleanup_marker is None:
            survivors = ownership_records(repository, pr)
        elif entry_remains(cleanup_marker):
            survivors = [cleanup_marker]
        else:
            survivors = []
        if survivors:
            raise RetainedError(
                "implementation worktree moved or changed; its ownership"
                f" record remains: {survivors[0]}"
            )
        return False

    def owned() -> Path:
        path, owner = owned_implementation(repository, pr, branch, head)
        if owner != issue:
            raise RetainedError(
                f"implementation ownership names issue #{owner}, the merge"
                f" record #{issue}: {path}"
            )
        # The ownership record can be copied into a replacement checkout
        # at the same path, branch, and head; only the checkout that the
        # record proved holds the marker in its Git directory (#175).
        if cleanup_marker is not None and (
            linked_record(repository, path).parent.resolve()
            != cleanup_marker.parent
            or cleanup_marker.is_symlink()
            or not cleanup_marker.is_file()
        ):
            raise RetainedError(
                "implementation worktree does not hold the merge record's"
                f" cleanup marker: {path}"
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
    *, implementation: int | None = None,
) -> dict:
    """Run steps 3-5 of §7.10 from the merge record, then delete it.

    A record whose ``merge_method`` is None describes a merge the forge made
    without pr merge (#175) and has not been written yet: the integration
    check proves the method, and the record is written before any cleanup.
    """
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
        result["integration"] = (
            verify_forge_merge(
                repository.primary, record["head"], record["head_branch"],
                merge_commit, tip,
            ) if record["merge_method"] is None else verify_integration(
                repository.primary, record["merge_method"], record["head"],
                merge_commit, tip, moved_base=record["moved_base"],
            )
        )
        result["merged_head"] = merged_source(
            repository.primary, record["head"], merge_commit,
            result["integration"],
        )
    except (AgentSquadError, OSError, ValueError) as error:
        return {
            **result, "exit_code": 1, "integration": str(error),
            "cleanup": [], "resources_retained": True,
            "cleanup_command": cleanup_command(pr),
        }
    if record["merge_method"] is None:
        method = PROVED_METHODS[result["integration"]]
        result["merge_method"] = method
        # Git reuses a removed worktree's directory name, and an unrelated
        # record can have identical content, so a retry recognizes the
        # original checkout only by this marker in its Git directory (#175).
        # ``implementation`` holds that directory open since its ownership
        # was proved; a directory put in its place meanwhile is refused.
        marker = None
        try:
            marker = create_cleanup_marker(
                implementation, Path(record["implementation_record"])
            )
            record = write_merge_record(
                repository, pr, record, method, False, outside=True,
                implementation_record=str(marker),
            )
        except (AgentSquadError, OSError, ValueError) as error:
            result["merge_record"]["reason"] = str(error)
            if marker is not None:
                # No record names it; leave the Git directory as it was.
                remove_cleanup_marker(implementation, marker.name)
            return {
                **result, "exit_code": 1, "cleanup": [],
                "resources_retained": True,
                "cleanup_command": cleanup_command(pr),
            }
        result["merge_record"]["result"] = "kept"
    try:
        result["cleanup"] = cleanup_merge(
            repository, pr, record["head"], record["head_branch"],
            record["issue"], forge,
            cleanup_marker=(
                Path(record["implementation_record"])
                if record["outside_pr_merge"] else None
            ),
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
    def refuse_merged(merged: PullRequest) -> None:
        if not merged.merged:
            return
        # Refused from the merged state and local records alone, before the
        # branch rules, the PR's reviews and conversation, or any read of
        # the PR head, which a merged Forgejo PR may report stale (E9) so
        # that fetching it fails (#175).
        path = merge_record_path(repository, pr)
        refused = "pr merge refused: PR is not open"
        if path.exists() or path.is_symlink():
            refused += (
                "; to finish the merge pr merge started, run"
                f" {cleanup_command(pr)}"
            )
        # Named only while pr cleanup can still find the PR's resources.
        elif named_implementations(repository, pr):
            refused += (
                "; the PR was merged outside pr merge; to finish its"
                f" cleanup, run {cleanup_command(pr)}"
            )
        raise GateError(refused)

    # The PR alone first: a failed read of anything else must not hide the
    # cleanup command of a PR that is already merged (#175).
    opened = forge.pr(pr)
    refuse_merged(opened)
    rules = (
        forge.branch_rules(opened.base_branch)
        if forge.can_read_branch_rules else {"visibility": "not visible"}
    )
    # Read authority after the potentially slow rules lookup. If that read
    # fails, the PR alone is read once more: the forge may have merged it
    # meanwhile, and then the refusal with its command is the answer.
    try:
        snapshot = forge.snapshot(pr)
    except ForgeError:
        refuse_merged(forge.pr(pr))
        raise
    refuse_merged(snapshot.pr)
    state = state_for(repository, snapshot)
    moved = check_merge_gate(
        state, accept_moved_base=accept_moved_base,
        accept_merge_hold=accept_merge_hold,
    )
    target = state["target"]
    method = repository.configuration.merge_method
    result = {
        "pr": pr, "head": target["head"], "merge_method": method,
        "moved_base": moved, "closes_issue": state["closes_issue"],
        "mergeable_state": snapshot.pr.mergeable_state,
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
        "merge_answer": None,
        "cleanup_command": None,
        "merged_outside_pr_merge": False,
        "approved_head": approved_head(state),
        "merged_head": None,
        "forge_head": snapshot.pr.head,
        "forge_head_branch": snapshot.pr.head_branch,
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
        # A Forgejo error answer has already been classified by a read of
        # the PR (#142); another forge's refusal is known by its status.
        refused = isinstance(error, MergeRefused) or (
            isinstance(error, ForgeError) and error.status in (
                400, 401, 403, 404, 405, 409, 422,
            )
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
        merged=True, merge_commit=merged["sha"], message=merged["message"],
        merge_answer=merged.get("answer"),
    )
    return complete_merge(repository, forge, record, merged["sha"], result)


def approved_head(state: dict) -> str | None:
    """The full head SHA the latest tagged approving review names (#175)."""
    return next((
        review["head"] for review in reversed(state["reviews"])
        if review["verdict"] == "approved"
    ), None)


def cleanup_pr(repository: Repository, forge: Forge, pr: int) -> dict:
    """Finish a merge that pr merge started, or one that the forge made
    without pr merge (#175); never send a merge request."""
    record = load_merge_record(repository, pr)
    merged = forge.pr(pr)
    if not merged.merged:
        if record is None:
            raise GateError(
                f"pr cleanup refused: no merge record for PR #{pr}, and the"
                " PR is not merged; pr cleanup only finishes a merge started"
                " by pr merge or one the forge made"
            )
        raise GateError(
            f"pr cleanup refused: PR #{pr} is not merged; merge record kept:"
            f" {merge_record_path(repository, pr)}"
        )
    written = record is not None
    implementation = None
    if record is None:
        record, implementation = forge_merged_record(repository, pr, merged)
    try:
        return finish_cleanup(
            repository, forge, pr, record, merged, written, implementation
        )
    finally:
        if implementation is not None:
            os.close(implementation)


def finish_cleanup(
    repository: Repository, forge: Forge, pr: int, record: dict,
    merged: PullRequest, written: bool, implementation: int | None,
) -> dict:
    """The reads and steps of pr cleanup once the record is in hand.

    ``implementation`` is the open descriptor of the implementation Git
    directory that a first run proved, or None on a run from a saved record.
    """
    if record["outside_pr_merge"]:
        # Read on every run for a merge made outside pr merge, before any
        # change, so the Implementer can report whether the merged head
        # was the approved one. It is read from review evidence alone:
        # fetching the PR head or the commits of earlier reviews can fail
        # after a merge (E9) and must not strand the cleanup, and neither
        # must the PR's ordinary conversation, which is not read.
        approved = approved_review_head(
            forge.snapshot(pr, conversation=False), repository.configuration
        )
    else:
        # pr merge verified this approval before its merge request and
        # recorded the head. No review is read again: a review dismissed
        # since then would lose the approval that authorized the merge,
        # and an unreadable one would strand the cleanup.
        approved = record["head"]
    # Only the merge commit comes from the forge: a merged Forgejo PR can
    # report a synthetic head branch and an older head (E9).
    return complete_merge(repository, forge, record, merged.merge_commit, {
        "pr": pr, "head": record["head"],
        "merge_method": record["merge_method"],
        "moved_base": record["moved_base"],
        "closes_issue": closes_issue(merged.evidence.body), "merged": True,
        "merge_commit": merged.merge_commit,
        "fast_forward": {
            "result": "skipped", "from": None, "to": None,
            "reason": "merge integration has not been verified",
            "command": None,
        },
        "merge_record": {
            "path": str(merge_record_path(repository, pr)),
            "result": "kept" if written else "not written", "reason": None,
        },
        # The merge record does not keep the merge request's answer.
        "merge_answer": None,
        "cleanup_command": None,
        "merged_outside_pr_merge": record["outside_pr_merge"],
        "approved_head": approved,
        "merged_head": None,
        "forge_head": merged.head,
        "forge_head_branch": merged.head_branch,
    }, implementation=implementation)
