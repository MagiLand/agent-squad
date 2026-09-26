"""Reviewer lifecycle, with ownership in linked-worktree Git metadata.

The ownership record identifies resources only. All review authority remains
on the forge; no delivery status or review result is stored here.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from .commands import state_for
from .forge import Forge
from .herdr import (
    AGENT_STATES,
    REVIEW_DELIVERY,
    HerdrClient,
    HerdrCommandError,
    HerdrError,
    request_line,
    result_message,
    reviewer_name,
    stopped_message,
)
from .initialization import (
    AgentSquadError,
    GateError,
    Repository,
    RetainedError,
    decode_json,
    git_output,
    list_worktrees,
)

OWNER_FILE = "agent-squad-owner.json"
SHELL_READY_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class ReviewWorktree:
    repository: Repository
    name: str
    head: str
    path: Path

    @classmethod
    def for_pr(
        cls, repository: Repository, pr: int, head: str
    ) -> ReviewWorktree:
        name = reviewer_name(pr, head)
        root = repository.resolve_root(repository.configuration.worktree_root)
        return cls(repository, name, head, root / name)

    def metadata_path(self) -> Path:
        """Check the registered detached checkout and its Git back-link."""
        if self.path.is_symlink():
            raise RetainedError(f"worktree path is a symlink: {self.path}")
        worktree = next(
            (
                w
                for w in list_worktrees(self.repository.primary)
                if w.root == self.path
            ),
            None,
        )
        if worktree is None or worktree.branch or worktree.head != self.head:
            raise RetainedError(
                f"not the expected detached squad worktree: {self.path}"
            )
        if git_output(self.path, "rev-parse", "HEAD") != self.head:
            raise RetainedError(f"worktree HEAD changed: {self.path}")
        admin = Path(git_output(self.path, "rev-parse", "--absolute-git-dir"))
        common = Path(
            git_output(
                self.path,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            )
        )
        if (
            common.resolve() != self.repository.common
            or admin.parent.resolve() != self.repository.common / "worktrees"
            or admin.is_symlink()
            or (admin / "gitdir").is_symlink()
            or Path((admin / "gitdir").read_text().strip())
            != self.path / ".git"
        ):
            raise RetainedError(f"worktree Git identity differs: {self.path}")
        return admin / OWNER_FILE

    def identity(self) -> dict:
        return {
            "schema_version": 1,
            "name": self.name,
            "head": self.head,
            "path": str(self.path),
            "common": str(self.repository.common),
        }

    def owner(self) -> dict:
        try:
            metadata = self.metadata_path()
            if metadata.is_symlink():
                raise ValueError("ownership record is a symlink")
            owner = decode_json(metadata.read_text(encoding="utf-8"))
            if not isinstance(owner, dict) or owner != {
                **self.identity(),
                "herdr": owner.get("herdr"),
            }:
                raise ValueError("ownership record does not match the target")
            resource = owner["herdr"]
            if resource is not None and (
                not isinstance(resource, dict)
                or set(resource)
                != {"workspace_id", "pane_id", "terminal_id", "kind"}
                or any(
                    not isinstance(v, str) or not v for v in resource.values()
                )
                or resource["kind"] not in ("claude", "codex")
            ):
                raise ValueError("invalid Herdr ownership record")
            return owner
        except (AgentSquadError, OSError, ValueError) as error:
            raise RetainedError(
                f"cannot prove squad ownership of {self.path}: {error}"
            ) from None

    def save_owner(self, owner: dict, *, initial: bool = False) -> None:
        metadata = self.metadata_path()
        if initial:
            with metadata.open("x", encoding="utf-8") as stream:
                json.dump(owner, stream)
            return
        self.owner()
        descriptor, temporary = tempfile.mkstemp(dir=metadata.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(owner, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, metadata)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def create(self) -> dict:
        actual = git_output(
            self.repository.primary,
            "rev-parse",
            "--verify",
            f"{self.head}^{{commit}}",
        )
        if actual != self.head:
            raise AgentSquadError("review head must identify a commit exactly")
        registered = any(
            w.root == self.path
            for w in list_worktrees(self.repository.primary)
        )
        if self.path.exists() or self.path.is_symlink() or registered:
            self.owner()
            if git_output(
                self.path, "status", "--porcelain", "--untracked-files=all"
            ):
                raise RetainedError(
                    f"review worktree is not clean: {self.path}"
                )
            return {"path": str(self.path), "head": self.head, "reused": True}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        git_output(
            self.repository.primary,
            "worktree",
            "add",
            "--detach",
            str(self.path),
            self.head,
        )
        try:
            self.save_owner({**self.identity(), "herdr": None}, initial=True)
        except (AgentSquadError, OSError, ValueError) as error:
            raise RetainedError(
                f"worktree retained at {self.path}; ownership write failed:"
                f" {error}"
            ) from None
        return {"path": str(self.path), "head": self.head, "reused": False}

    def remove(self) -> dict:
        self.owner()
        git_output(
            self.repository.primary,
            "worktree",
            "remove",
            "--force",
            str(self.path),
        )
        self.verify_removed()
        return {"removed": str(self.path), "head": self.head}

    def verify_removed(self) -> None:
        if any(
            w.root == self.path
            for w in list_worktrees(self.repository.primary)
        ):
            raise RetainedError(f"worktree is still registered: {self.path}")
        if self.path.exists() or self.path.is_symlink():
            raise RetainedError(
                f"worktree path remains after cleanup: {self.path}"
            )


def resource_detail(worktree: ReviewWorktree, resource: dict) -> str:
    return (
        f"{worktree.path}; workspace={resource.get('workspace_id', 'unknown')}"
        f" pane={resource.get('pane_id', 'unknown')}"
    )


def agent_state(agent: dict) -> str:
    if not isinstance(agent, dict):
        raise HerdrError("Herdr response has no agent object")
    state = agent.get("agent_status")
    if state not in AGENT_STATES:
        raise HerdrError(f"Herdr returned an invalid agent state: {state!r}")
    return state


def resource_objects(snapshot: dict, key: str) -> list[dict]:
    """Reject incomplete inventories before considering resource deletion."""
    values = snapshot.get(key)
    if not isinstance(values, list) or any(
        not isinstance(value, dict) for value in values
    ):
        raise HerdrError(f"Herdr snapshot has no valid {key} array")
    return values


def verify_agent(agent: dict, worktree: ReviewWorktree, resource: dict) -> str:
    """Bind startup/delivery responses to the owned terminal and target."""
    observed = agent_state(agent)
    expected = {
        "name": worktree.name,
        "agent": resource["kind"],
        "cwd": str(worktree.path),
        **{k: resource[k] for k in ("workspace_id", "pane_id", "terminal_id")},
    }
    if any(agent.get(k) != v for k, v in expected.items()):
        raise RetainedError("Herdr returned an unexpected Reviewer identity")
    return observed


def open_reviewer(worktree: ReviewWorktree, client: HerdrClient) -> dict:
    """Record the workspace before starting a process, allowing recovery."""
    owner = worktree.owner()
    result = client.open_worktree(
        worktree.repository.primary, worktree.path, worktree.name
    )
    workspace = result.get("workspace", {})
    pane = result.get("root_pane", {})
    checkout = result.get("worktree", {})
    if not all(isinstance(v, dict) for v in (workspace, pane, checkout)):
        raise RetainedError(f"malformed Herdr resources for {worktree.path}")
    resource = {
        "workspace_id": workspace.get("workspace_id"),
        "pane_id": pane.get("pane_id"),
        "terminal_id": pane.get("terminal_id"),
        "kind": worktree.repository.configuration.reviewer.kind.value,
    }
    try:
        if (
            checkout.get("path") != str(worktree.path)
            or workspace.get("label") != worktree.name
            or pane.get("cwd") != str(worktree.path)
            or pane.get("workspace_id") != resource["workspace_id"]
            or any(not isinstance(v, str) or not v for v in resource.values())
        ):
            raise HerdrError("Herdr opened an unexpected worktree or pane")
        if (
            result.get("already_open") is not False
            and owner["herdr"] != resource
        ):
            raise RetainedError("existing Herdr workspace is not squad-owned")
        worktree.save_owner({**owner, "herdr": resource})
        # This also rejects a pre-existing workspace that gained another pane.
        verify_workspace(worktree, client, resource, allow_shell=True)
        return resource
    except (AgentSquadError, OSError, ValueError) as error:
        raise RetainedError(
            f"{error}; resources retained:"
            f" {resource_detail(worktree, resource)}"
        ) from None


def verify_workspace(
    worktree: ReviewWorktree,
    client: HerdrClient,
    resource: dict,
    *,
    allow_shell: bool = True,
) -> dict:
    """Verify isolation and the original terminal, including after exit."""
    workspace = client.workspace(resource["workspace_id"])
    registration = workspace.get("worktree")
    if registration is not None and not isinstance(registration, dict):
        raise RetainedError(
            "Herdr workspace has an invalid worktree registration"
        )
    if (
        workspace.get("workspace_id") != resource["workspace_id"]
        or workspace.get("label") != worktree.name
        or workspace.get("tab_count") != 1
        or workspace.get("pane_count") != 1
        or (
            registration is not None
            and (
                registration.get("checkout_path") != str(worktree.path)
                or registration.get("repo_root")
                != str(worktree.repository.primary)
                or registration.get("is_linked_worktree") is not True
            )
        )
    ):
        raise RetainedError(
            "workspace identity or isolation cannot be verified"
        )
    snapshot = client.snapshot()
    panes = [
        p
        for p in resource_objects(snapshot, "panes")
        if p.get("workspace_id") == resource["workspace_id"]
    ]
    tabs = [
        t
        for t in resource_objects(snapshot, "tabs")
        if t.get("workspace_id") == resource["workspace_id"]
    ]
    if len(panes) != 1 or len(tabs) != 1:
        raise RetainedError(
            "workspace does not contain exactly one tab and pane"
        )
    pane = panes[0]
    if (
        pane.get("pane_id") != resource["pane_id"]
        or pane.get("terminal_id") != resource["terminal_id"]
        or pane.get("tab_id") != tabs[0].get("tab_id")
        or workspace.get("active_tab_id") != tabs[0].get("tab_id")
        or pane.get("cwd") != str(worktree.path)
    ):
        raise RetainedError(
            "workspace no longer contains the original Reviewer pane"
        )
    occupants = [
        a
        for a in resource_objects(snapshot, "agents")
        if a.get("pane_id") == resource["pane_id"]
    ]
    if occupants:
        if len(occupants) != 1 or (
            occupants[0].get("name") != worktree.name
            or occupants[0].get("agent") != resource["kind"]
            or occupants[0].get("terminal_id") != resource["terminal_id"]
            or occupants[0].get("cwd") != str(worktree.path)
        ):
            raise RetainedError("the Reviewer pane has a different occupant")
    elif not allow_shell or pane.get("agent") is not None:
        raise RetainedError("the Reviewer pane occupant cannot be verified")
    return workspace


def close_reviewer(worktree: ReviewWorktree, client: HerdrClient) -> dict:
    """Remove only an owned checkout and its isolated Herdr workspace."""
    owner = worktree.owner()
    resource = owner["herdr"]
    try:
        if resource is None:
            for workspace in resource_objects(client.snapshot(), "workspaces"):
                registration = workspace.get("worktree") or {}
                if not isinstance(registration, dict):
                    raise RetainedError("Herdr worktree inventory is invalid")
                if registration.get("checkout_path") == str(worktree.path):
                    raise RetainedError(
                        "an unowned Herdr workspace still uses this worktree"
                    )
            return worktree.remove()
        workspace = verify_workspace(worktree, client, resource)
        if workspace.get("worktree") is None:
            client.close_workspace(resource["workspace_id"])
            return worktree.remove()
        removed = client.remove_worktree(resource["workspace_id"])
        if (
            removed.get("path") != str(worktree.path)
            or removed.get("workspace_id") != resource["workspace_id"]
        ):
            raise RetainedError(
                "Herdr reported removal of an unexpected resource"
            )
        worktree.verify_removed()
        return {"removed": str(worktree.path), **resource}
    except (AgentSquadError, OSError, ValueError) as error:
        raise RetainedError(
            f"{error}; resources retained or cleanup incomplete:"
            f" {resource_detail(worktree, resource or {})}"
        ) from None


def start_reviewer(
    worktree: ReviewWorktree,
    client: HerdrClient,
    resource: dict,
    args: tuple[str, ...],
) -> dict:
    """Allow a new shell to initialize, retrying only a pre-launch refusal.

    Herdr's agent_pane_busy response means no agent was started. Each bounded
    retry rechecks the original owned workspace. Other failures, blocked
    startup, and prompt delivery are never retried.
    """
    deadline = time.monotonic() + SHELL_READY_TIMEOUT_SECONDS
    while True:
        try:
            return client.start_agent(
                worktree.name,
                worktree.repository.configuration.reviewer.kind,
                resource["pane_id"],
                args,
            )
        except HerdrCommandError as error:
            if error.code != "agent_pane_busy" or time.monotonic() >= deadline:
                raise
            verify_workspace(worktree, client, resource)
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def launch(
    repository: Repository,
    forge: Forge,
    pr: int,
    *,
    client: HerdrClient | None = None,
) -> dict:
    client = client or HerdrClient(repository.primary)
    state = state_for(
        repository,
        forge.snapshot(pr),
        herdr_client=client,
        include_reviewer=True,
    )
    gates = state["gates"]
    refused = [
        name
        for name, active in gates.items()
        if active
        and name != "task_amended"
        and not (
            name == "same_head_requires_rejections" and gates["task_amended"]
        )
    ]
    if state["pr"]["merged"] or state["pr"]["state"] != "open":
        refused.append("PR is not open")
    if refused:
        raise GateError(
            "reviewer launch refused: "
            + ", ".join(refused)
            + (
                "; a DECISION with budget= is required"
                if gates["budget_exhausted"]
                else ""
            )
            + (
                "; use reviewer adopt or reviewer close"
                if gates["reviewer_live"]
                else ""
            )
        )
    target = state["target"]
    if not git_output(
        repository.primary,
        "diff",
        "--name-only",
        target["base"],
        target["head"],
    ):
        raise GateError("reviewer launch refused: review scope is empty")
    worktree = ReviewWorktree.for_pr(repository, pr, target["head"])
    created = worktree.create()
    config = repository.configuration
    resource = {}
    try:
        scratch = repository.resolve_root(config.scratch_root) / f"pr{pr}"
        if scratch.is_symlink():
            raise AgentSquadError(f"scratch directory is a symlink: {scratch}")
        scratch.mkdir(parents=True, exist_ok=True)
        client.discover(config.reviewer.kind, role="Reviewer")
        resource = open_reviewer(worktree, client)
        line = request_line(
            config.reviewer.kind,
            pr,
            target["head"],
            target["base"],
            config.implementer.agent_name,
        )
        delivery = REVIEW_DELIVERY[config.reviewer.kind]
        args = config.reviewer.start_args
        if delivery == "initial_prompt":
            args += (line,)
        elif delivery != "agent_prompt":
            raise HerdrError(
                f"unsupported review delivery mechanism: {delivery}"
            )
        started = start_reviewer(worktree, client, resource, args)
        if verify_agent(started.get("agent"), worktree, resource) == "blocked":
            raise RetainedError(
                "Reviewer is blocked; answer in its pane, then adopt"
            )
        if delivery == "agent_prompt":
            client.prompt(worktree.name, line)
        agent = client.inspect_agent(
            worktree.name,
            config.reviewer.kind,
            role="Reviewer",
            worktree=worktree.path,
        )
        observed = verify_agent(agent, worktree, resource)
        if observed == "blocked":
            raise RetainedError(
                "Reviewer is blocked; answer in its pane, then adopt"
            )
        return {
            "name": worktree.name,
            **resource,
            "worktree": str(worktree.path),
            "target": target,
            "delivery": delivery,
            "observed_state": observed,
            "reused": created["reused"],
            "scratch": str(scratch),
        }
    except (AgentSquadError, OSError, ValueError) as error:
        retained = isinstance(error, RetainedError) or (
            isinstance(error, HerdrCommandError)
            and error.code == "agent_not_ready"
        )
        cls = RetainedError if retained else HerdrError
        raise cls(
            f"{error}; resources retained:"
            f" {resource_detail(worktree, resource)}"
        ) from None


def adopt(
    repository: Repository,
    forge: Forge,
    pr: int,
    *,
    client: HerdrClient | None = None,
) -> dict:
    client = client or HerdrClient(repository.primary)
    target = state_for(repository, forge.snapshot(pr))["target"]
    worktree = ReviewWorktree.for_pr(repository, pr, target["head"])
    resource = worktree.owner()["herdr"]
    if resource is None:
        raise RetainedError(f"no owned Herdr Reviewer for {worktree.path}")
    try:
        config = repository.configuration
        agent = client.inspect_agent(
            worktree.name,
            config.reviewer.kind,
            role="Reviewer",
            worktree=worktree.path,
        )
        verify_workspace(worktree, client, resource, allow_shell=False)
        if verify_agent(agent, worktree, resource) == "blocked":
            raise RetainedError(
                "Reviewer is blocked; answer the prompt in its pane"
            )
        client.prompt(
            worktree.name,
            request_line(
                config.reviewer.kind,
                pr,
                target["head"],
                target["base"],
                config.implementer.agent_name,
            ),
        )
        observed = verify_agent(
            client.inspect_agent(
                worktree.name,
                config.reviewer.kind,
                role="Reviewer",
                worktree=worktree.path,
            ),
            worktree,
            resource,
        )
        if observed == "blocked":
            raise RetainedError("Reviewer is blocked after delivery")
        return {
            "name": worktree.name,
            **resource,
            "worktree": str(worktree.path),
            "target": target,
            "delivery": "agent_prompt",
            "observed_state": observed,
        }
    except (AgentSquadError, OSError, ValueError) as error:
        cls = RetainedError if isinstance(error, RetainedError) else HerdrError
        raise cls(
            f"{error}; resources retained:"
            f" {resource_detail(worktree, resource)}"
        ) from None


def handoff(
    repository: Repository,
    forge: Forge,
    pr: int,
    head: str,
    *,
    verdict: str | None = None,
    reason: str | None = None,
    client: HerdrClient | None = None,
) -> dict:
    message = (
        result_message(pr, head, verdict)
        if verdict is not None
        else stopped_message(pr, head, reason)
    )
    state = state_for(repository, forge.snapshot(pr))
    key, value, records = (
        ("verdict", verdict, state["reviews"])
        if verdict is not None
        else ("reason", reason, state["stops"])
    )
    if not any(r["head"] == head and r[key] == value for r in records):
        raise AgentSquadError(
            "no matching tagged review or STOPPED exists on the PR"
        )
    client = client or HerdrClient(repository.primary)
    name = repository.configuration.implementer.agent_name
    client.prompt(name, message)
    return {"pr": pr, "head": head, "target_agent": name, "message": message}


def live_probe(repository: Repository, client: HerdrClient) -> dict:
    """Probe startup and trust without sending a review request."""
    head = git_output(repository.root, "rev-parse", "HEAD")
    name = "reviewer-doctor-" + uuid.uuid4().hex[:12]
    worktree = ReviewWorktree(
        repository,
        name,
        head,
        repository.resolve_root(repository.configuration.worktree_root) / name,
    )
    worktree.create()
    resource = {}
    try:
        resource = open_reviewer(worktree, client)
        config = repository.configuration.reviewer
        try:
            result = start_reviewer(
                worktree, client, resource, config.start_args
            )
            observed = verify_agent(result.get("agent"), worktree, resource)
            blocked = observed == "blocked"
        except HerdrCommandError as error:
            if error.code != "agent_not_ready":
                raise
            observed, blocked = "blocked", True
        close_reviewer(worktree, client)
        return {
            "name": name,
            "head": head,
            "kind": config.kind.value,
            "observed_state": observed,
            "trust_or_permission_prompt": blocked,
            "removed": str(worktree.path),
            **resource,
        }
    except (AgentSquadError, OSError, ValueError) as error:
        raise RetainedError(
            f"live Reviewer probe could not safely finish: {error};"
            f" resources retained: {resource_detail(worktree, resource)}"
        ) from None
