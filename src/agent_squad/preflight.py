"""Owned live Reviewer proof using the real review-submit protocol."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import time
import uuid

from .artifacts import (
    BundleArtifact,
    ReviewRequest,
    ReviewResult,
    ReviewVerdict,
    SubmissionMode,
    deterministic_reviewer_name,
)
from .doctor import git_output
from .herdr import HerdrClient
from .initialization import (
    AgentSquadError,
    InitializedRepository,
    REVIEW_DIRECTORY_NAME,
)
from .runs import repository_identity
from .review_submissions import (
    load_marker_confirmed_review,
    submit_review_result,
)
from .storage import atomic_write, encode_json, utc_timestamp


def live_preflight(
    repository: InitializedRepository,
    client: HerdrClient,
    *,
    timeout_seconds: float,
) -> str:
    """Prove a temporary Reviewer can submit and hand off a real result."""

    if timeout_seconds <= 0:
        raise AgentSquadError("live preflight timeout must be positive")
    root = (
        repository.configuration.review_worktree_root.resolve()
        / repository_identity(repository.worktree).repository_id
    )
    if root.is_symlink():
        raise AgentSquadError(f"diagnostic namespace is a symlink: {root}")
    root.mkdir(parents=True, exist_ok=True)
    parent = Path(tempfile.mkdtemp(prefix=".preflight-", dir=root))
    run_id = str(uuid.uuid4())
    worktree = parent / run_id / "round-001"
    worktree.parent.mkdir(mode=0o700)
    manifest: dict[str, object] = {
        "created_at": utc_timestamp(),
        "worktree": str(worktree),
    }
    name: str | None = None
    stage = "create detached snapshot"
    try:
        base = git_output(
            repository.worktree.root, "rev-parse", "HEAD^{commit}"
        )
        tree = git_output(repository.worktree.root, "rev-parse", "HEAD^{tree}")
        # An unreferenced synthetic child preserves the request's first-round
        # contract without moving any project branch or changing project files.
        head = git_output(
            repository.worktree.root,
            "-c",
            "user.name=Agent Squad Preflight",
            "-c",
            "user.email=preflight@example.invalid",
            "-c",
            "commit.gpgSign=false",
            "commit-tree",
            tree,
            "-p",
            base,
            "-m",
            "Agent Squad owned preflight",
        )
        git_output(
            repository.worktree.root,
            "-c",
            "core.hooksPath=/dev/null",
            "worktree",
            "add",
            "--detach",
            str(worktree),
            head,
        )
        stage = "prepare owned request"
        files = git_output(worktree, "ls-tree", "-r", "-z", "HEAD").split("\0")
        snapshot_file = next(
            (
                item.split("\t", 1)[1]
                for item in files
                if item.startswith(("100644 blob ", "100755 blob "))
            ),
            None,
        )
        if snapshot_file is None:
            raise AgentSquadError(
                "snapshot has no tracked regular file to read"
            )
        bundle = worktree / REVIEW_DIRECTORY_NAME
        bundle.mkdir(mode=0o700)
        (bundle / "input/context").mkdir(parents=True, mode=0o700)
        (bundle / "output").mkdir(mode=0o700)
        name = deterministic_reviewer_name(run_id, 1)
        manifest.update({"reviewer_name": name, "run_id": run_id})
        task = (
            b"Prove local Reviewer read, write, submission and Herdr "
            b"handoff capabilities.\n"
        )
        challenge = encode_json(
            {"nonce": str(uuid.uuid4()), "snapshot_file": snapshot_file}
        )
        package_root = Path(__file__).resolve().parents[1]
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(package_root)!r})\n"
            "from agent_squad.preflight import complete_probe\n"
            "complete_probe()\n"
        ).encode()
        inputs = {
            "input/task.md": task,
            "input/implementation-report.md": task,
            "input/context/challenge.json": challenge,
            "input/context/probe.py": script,
        }
        request = ReviewRequest(
            created_at=utc_timestamp(),
            request_id=str(uuid.uuid4()),
            run_id=run_id,
            round_number=1,
            mode=SubmissionMode.NEW_REVISION,
            object_format=git_output(
                worktree, "rev-parse", "--show-object-format"
            ),
            base_oid=base,
            head_oid=head,
            task=_artifact("input/task.md", task),
            implementation_report=_artifact(
                "input/implementation-report.md", task
            ),
            context_files=tuple(
                _artifact(path, content)
                for path, content in inputs.items()
                if path.startswith("input/context/")
            ),
            previous_review_path=None,
            previous_response_path=None,
            recovery_round_path=None,
            resolution_paths=(),
            review_output_path="output/review.json",
            review_markdown_path="output/review.md",
            implementer_agent=name,
            implementer_kind=repository.configuration.reviewer.kind,
            reviewer_kind=repository.configuration.reviewer.kind,
            reviewer_name=name,
            allowed_generated_paths=(),
        )
        request = ReviewRequest.from_dict(request.to_dict())
        request_bytes = encode_json(request.to_dict())
        inputs["input/request.json"] = request_bytes
        for path, content in inputs.items():
            atomic_write(bundle / path, content, mode=0o400)
        expected_sentinel = hashlib.sha256(
            challenge
            + (worktree / snapshot_file).read_bytes()
            + request_bytes,
        ).hexdigest()
        manifest["request_id"] = request.request_id
        atomic_write(
            parent / "diagnostic.json", encode_json(manifest), mode=0o600
        )
        command = shlex.join(
            [sys.executable, str(bundle / "input/context/probe.py")]
        )
        stage = "launch and prompt temporary Reviewer"
        session = client.dispatch_review_request(
            reviewer_name=name,
            reviewer_kind=request.reviewer_kind,
            start_args=repository.configuration.reviewer.start_args,
            review_worktree=worktree,
            allow_adoption=False,
            prompt=(
                "Agent Squad live capability preflight. "
                "Read the local request at "
                f"{bundle / 'input/request.json'} and its task, "
                f"then run {command} from {worktree}. "
                "The owned helper reads a tracked snapshot file, "
                "writes only permitted review outputs and local-state.json, "
                "and uses review-submit to send a result handoff to this "
                "temporary session. If any permission or command fails, "
                "report the exact error. Do not edit project files or "
                "contact other agents. After the helper finishes, stop; "
                "the self-addressed REVIEW_RESULT is diagnostic only."
            ),
        )
        manifest.update(
            {"workspace_id": session.workspace_id, "pane_id": session.pane_id}
        )
        atomic_write(
            parent / "diagnostic.json", encode_json(manifest), mode=0o600
        )
        stage = "observe Reviewer output and submission"
        deadline = time.monotonic() + timeout_seconds
        receipt_path = bundle / "output/preflight-receipt.json"
        while not receipt_path.exists():
            agent = client.inspect_agent(
                name, request.reviewer_kind, role="Reviewer", worktree=worktree
            )
            if agent.get("agent_status") == "blocked":
                raise AgentSquadError(
                    "temporary Reviewer is blocked on a permission "
                    "or interactive prompt"
                )
            if time.monotonic() >= deadline:
                raise AgentSquadError(
                    f"timed out after {timeout_seconds:g}s waiting for "
                    f"{receipt_path}; inspect Reviewer {name} for denied "
                    "read/write or command permission"
                )
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        receipt = _read_object(receipt_path)
        if receipt.get("error"):
            raise AgentSquadError(str(receipt["error"]))
        stage = "inspect sentinel and marker-confirmed result"
        sentinel_path = bundle / "output/sentinel.txt"
        if (
            sentinel_path.is_symlink()
            or sentinel_path.read_text(encoding="utf-8") != expected_sentinel
        ):
            raise AgentSquadError(
                f"request/snapshot read sentinel differs: {sentinel_path}"
            )
        evidence = load_marker_confirmed_review(worktree)
        if evidence.request != request:
            raise AgentSquadError(
                "observed request differs from owned preflight request"
            )
        if (
            receipt.get("notification_sent") is not True
            or receipt.get("result_id") != evidence.review.result_id
        ):
            raise AgentSquadError(
                "review-submit did not confirm the owned result handoff"
            )
        stage = "observe result handoff in installed Herdr history"
        history = client.read_history(name)
        atomic_write(parent / "history.txt", history.encode(), mode=0o600)
        if (
            "AGENT_SQUAD/0.4.4 REVIEW_RESULT" not in history
            or evidence.review.result_id not in history
        ):
            raise AgentSquadError(
                "Herdr history does not expose the submitted "
                "result marker and result ID"
            )
        stage = "clean up owned preflight resources"
        client.close_preflight(
            session, name=name, kind=request.reviewer_kind, worktree=worktree
        )
        # Marker validation above verifies the snapshot and regular bundle.
        # Remove only the owned bundle, then let Git refuse any other dirt.
        shutil.rmtree(bundle)
        git_output(
            repository.worktree.root, "worktree", "remove", str(worktree)
        )
        worktree.parent.rmdir()
        for filename in ("diagnostic.json", "history.txt"):
            (parent / filename).unlink()
        parent.rmdir()
        return (
            "snapshot/request read, output write, review-submit, "
            "local marker, "
            "result inspection and Herdr handoff verified; "
            "owned resources removed"
        )
    except (AgentSquadError, OSError, ValueError, RuntimeError) as error:
        manifest.update({"failed_stage": stage, "error": str(error)})
        if name is not None:
            try:
                history = client.read_history(name)
                atomic_write(
                    parent / "history.txt", history.encode(), mode=0o600
                )
                manifest["history_path"] = str(parent / "history.txt")
            except (AgentSquadError, OSError) as history_error:
                manifest["history_error"] = str(history_error)
        try:
            atomic_write(
                parent / "diagnostic.json", encode_json(manifest), mode=0o600
            )
        except OSError:
            pass
        raise AgentSquadError(
            f"{stage}: {error}; owned diagnostic evidence retained at {parent}"
        ) from error


def complete_probe() -> None:
    """Run inside the temporary Reviewer, through its actual permissions."""

    bundle = Path.cwd() / REVIEW_DIRECTORY_NAME
    receipt: dict[str, object]
    try:
        request_bytes = (bundle / "input/request.json").read_bytes()
        request = ReviewRequest.from_dict(json.loads(request_bytes))
        challenge_bytes = (
            bundle / "input/context/challenge.json"
        ).read_bytes()
        challenge = json.loads(challenge_bytes)
        snapshot_bytes = (Path.cwd() / challenge["snapshot_file"]).read_bytes()
        sentinel = hashlib.sha256(
            challenge_bytes + snapshot_bytes + request_bytes
        ).hexdigest()
        atomic_write(
            bundle / "output/sentinel.txt", sentinel.encode(), mode=0o600
        )
        review = ReviewResult(
            created_at=utc_timestamp(),
            result_id=str(uuid.uuid4()),
            request_id=request.request_id,
            run_id=request.run_id,
            round_number=request.round_number,
            base_oid=request.base_oid,
            head_oid=request.head_oid,
            verdict=ReviewVerdict.APPROVED,
            summary="Owned live preflight read/write proof completed.",
            findings=(),
            non_blocking_observations=(),
        )
        atomic_write(
            bundle / request.review_output_path,
            encode_json(review.to_dict()),
            mode=0o600,
        )
        atomic_write(
            bundle / request.review_markdown_path,
            b"# Preflight\n\nRead/write proof completed.\n",
            mode=0o600,
        )
        result = submit_review_result(Path.cwd())
        receipt = {
            "notification_sent": result.notification_sent,
            "result_id": result.result_id,
            "error": result.notification_error,
        }
    except (AgentSquadError, OSError, ValueError) as error:
        receipt = {"error": str(error)}
    atomic_write(
        bundle / "output/preflight-receipt.json",
        encode_json(receipt),
        mode=0o600,
    )
    print(json.dumps(receipt))


def _artifact(path: str, content: bytes) -> BundleArtifact:
    return BundleArtifact(
        path=path, sha256=hashlib.sha256(content).hexdigest()
    )


def _read_object(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise AgentSquadError(
            f"diagnostic receipt is not a regular file: {path}"
        )
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise AgentSquadError(f"diagnostic receipt is not an object: {path}")
    return value
