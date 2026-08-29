from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from tests._support import add_src_to_path


add_src_to_path()

from agent_squad import review_applications, runs  # noqa: E402
from agent_squad.artifacts import (  # noqa: E402
    BundleArtifact,
    RoundStatus,
    SubmissionMode,
)
from agent_squad.initialization import AgentKind  # noqa: E402


RUN_ID = "87654321-4321-6789-a234-678912345678"
REQUEST_ID = "12345678-1234-5678-9234-567812345678"
RESULT_ID = "abcdefab-1234-5678-9234-567812345678"


class ResultIdentityTests(unittest.TestCase):
    def test_optional_result_id_accepts_only_canonical_uuid(self) -> None:
        self.assertIsNone(review_applications._optional_result_id(None))
        self.assertEqual(
            review_applications._optional_result_id(RESULT_ID),
            RESULT_ID,
        )
        for value in ("not-a-uuid", RESULT_ID.upper()):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    review_applications.ReviewApplicationError,
                    "must be a canonical UUID",
                ):
                    review_applications._optional_result_id(value)


class EventRecoveryTests(unittest.TestCase):
    def test_event_is_appended_once_and_exact_replay_is_unchanged(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "events.jsonl"
            path.write_bytes(b"")
            event = {
                "timestamp": "2026-08-28T00:00:00Z",
                "event": "review_applied",
                "run_id": RUN_ID,
            }

            review_applications._ensure_event(
                path,
                event,
                identity_fields=("event", "run_id"),
            )
            first = path.read_bytes()
            review_applications._ensure_event(
                path,
                event,
                identity_fields=("event", "run_id"),
            )

            self.assertEqual(path.read_bytes(), first)

    def test_event_log_rejects_unsafe_malformed_or_conflicting_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "events.jsonl"
            event = {
                "timestamp": "2026-08-28T00:00:00Z",
                "event": "run_completed",
                "run_id": RUN_ID,
            }
            cases = (
                (b"{\n", "contains invalid JSON"),
                (b"[]\n", "must be a JSON object"),
                (
                    (
                        b'{"timestamp":"earlier","event":"run_completed",'
                        b'"run_id":"' + RUN_ID.encode("ascii") + b'"}\n'
                    ),
                    "conflicting logical transition event",
                ),
                (
                    (
                        b'{"timestamp":"2026-08-28T00:00:00Z",'
                        b'"event":"run_completed","run_id":"'
                        + RUN_ID.encode("ascii")
                        + b'"}\n'
                    )
                    * 2,
                    "duplicate logical transition events",
                ),
            )
            for content, message in cases:
                with self.subTest(message=message):
                    path.write_bytes(content)
                    with self.assertRaisesRegex(
                        review_applications.ReviewApplicationError,
                        message,
                    ):
                        review_applications._ensure_event(
                            path,
                            event,
                            identity_fields=("event", "run_id"),
                        )

            path.unlink()
            path.mkdir()
            with self.assertRaisesRegex(OSError, "not a regular"):
                review_applications._ensure_event(
                    path,
                    event,
                    identity_fields=("event", "run_id"),
                )


class ImmutableArchiveTests(unittest.TestCase):
    def test_bundle_verification_rejects_missing_and_changed_content(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = b"review\n"
            artifact = BundleArtifact(
                path="bundle/review.txt",
                sha256=hashlib.sha256(content).hexdigest(),
            )
            (root / "review.txt").write_bytes(content)

            review_applications._verify_bundle_tree(
                root,
                (artifact,),
                label="fixture",
            )
            (root / "review.txt").unlink()
            with self.assertRaisesRegex(
                review_applications.ReviewApplicationError,
                "does not match validated evidence",
            ):
                review_applications._verify_bundle_tree(
                    root,
                    (artifact,),
                    label="fixture",
                )
            (root / "review.txt").write_bytes(b"changed\n")
            with self.assertRaisesRegex(
                review_applications.ReviewApplicationError,
                "digest mismatch",
            ):
                review_applications._verify_bundle_tree(
                    root,
                    (artifact,),
                    label="fixture",
                )

    def test_immutable_artifact_reuses_only_identical_regular_content(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "approval.json"
            content = b"authority\n"

            first = review_applications._write_immutable_artifact(
                path,
                content,
                path="approval.json",
            )
            second = review_applications._write_immutable_artifact(
                path,
                content,
                path="approval.json",
            )
            self.assertEqual(first, second)
            with self.assertRaisesRegex(
                review_applications.ReviewApplicationError,
                "existing immutable artifact differs",
            ):
                review_applications._write_immutable_artifact(
                    path,
                    b"different\n",
                    path="approval.json",
                )

            path.unlink()
            path.mkdir()
            with self.assertRaisesRegex(
                review_applications.ReviewApplicationError,
                "cannot inspect existing immutable artifact",
            ):
                review_applications._write_immutable_artifact(
                    path,
                    content,
                    path="approval.json",
                )


class GitAuthorityTests(unittest.TestCase):
    def test_current_head_rejects_git_failures_and_wrong_authority(
        self,
    ) -> None:
        success_format = SimpleNamespace(
            returncode=0,
            stdout="sha1\n",
            stderr="",
        )
        success_head = SimpleNamespace(
            returncode=0,
            stdout=f"{'a' * 40}\n",
            stderr="",
        )
        cases = (
            (
                [SimpleNamespace(returncode=1, stdout="", stderr="format")],
                "could not determine implementation Git object format",
            ),
            ([success_format], "object format does not match"),
            (
                [
                    success_format,
                    SimpleNamespace(
                        returncode=1,
                        stdout="",
                        stderr="head",
                    ),
                ],
                "could not resolve implementation HEAD",
            ),
            (
                [
                    success_format,
                    SimpleNamespace(
                        returncode=0,
                        stdout="ABC\n",
                        stderr="",
                    ),
                ],
                "not a full lowercase sha1 object ID",
            ),
        )
        for results, message in cases:
            with self.subTest(message=message):
                expected_format = "sha256" if len(results) == 1 else "sha1"
                with (
                    mock.patch.object(
                        review_applications,
                        "run_git",
                        side_effect=results,
                    ),
                    self.assertRaisesRegex(
                        review_applications.ReviewApplicationError,
                        message,
                    ),
                ):
                    review_applications._current_head(
                        Path("/repository"),
                        expected_format,
                        label="implementation",
                    )

        with mock.patch.object(
            review_applications,
            "run_git",
            side_effect=(success_format, success_head),
        ):
            self.assertEqual(
                review_applications._current_head(
                    Path("/repository"),
                    "sha1",
                    label="implementation",
                ),
                "a" * 40,
            )

    def test_review_evidence_comparison_rejects_each_authority_mismatch(
        self,
    ) -> None:
        review_root = Path("/review")
        common = Path("/git-common")
        active_round = SimpleNamespace(
            review_worktree=review_root,
            request_id=REQUEST_ID,
            mode=SubmissionMode.NEW_REVISION,
            reviewer_name="asq-876543214321-r001-reviewer",
        )
        active = SimpleNamespace(
            active_round=active_round,
            repository=SimpleNamespace(git_common_dir=common),
            run_id=RUN_ID,
            current_round=1,
            git_object_format="sha1",
            base_oid="a" * 40,
            current_head_oid="b" * 40,
            task_sha256="c" * 64,
            implementer_agent="codex-main",
            implementer_kind=AgentKind.CODEX,
            reviewer_kind=AgentKind.CLAUDE,
        )
        request = SimpleNamespace(
            run_id=RUN_ID,
            round_number=1,
            request_id=REQUEST_ID,
            mode=SubmissionMode.NEW_REVISION,
            object_format="sha1",
            base_oid="a" * 40,
            head_oid="b" * 40,
            task=SimpleNamespace(sha256="c" * 64),
            implementer_agent="codex-main",
            implementer_kind=AgentKind.CODEX,
            reviewer_kind=AgentKind.CLAUDE,
            reviewer_name=active_round.reviewer_name,
            allowed_generated_paths=(),
        )
        evidence = SimpleNamespace(
            worktree=SimpleNamespace(
                root=review_root,
                common_directory=common,
            ),
            request=request,
            review=SimpleNamespace(result_id=RESULT_ID),
            marker=SimpleNamespace(
                request_id=REQUEST_ID,
                result_id=RESULT_ID,
            ),
        )
        repository = SimpleNamespace(
            configuration=SimpleNamespace(allowed_generated_paths=())
        )
        review_applications._validate_evidence(
            repository,
            active,
            evidence,
        )

        cases = (
            (
                "review worktree",
                lambda item: setattr(
                    item[2].worktree,
                    "root",
                    Path("/other-review"),
                ),
            ),
            (
                "different Git repository",
                lambda item: setattr(
                    item[2].worktree,
                    "common_directory",
                    Path("/other-common"),
                ),
            ),
            (
                "run ID",
                lambda item: setattr(item[2].request, "run_id", REQUEST_ID),
            ),
            (
                "round number",
                lambda item: setattr(item[2].request, "round_number", 2),
            ),
            (
                "request ID",
                lambda item: setattr(item[2].marker, "request_id", RUN_ID),
            ),
            (
                "head OID",
                lambda item: setattr(item[2].request, "head_oid", "d" * 40),
            ),
            (
                "task digest",
                lambda item: setattr(item[2].request.task, "sha256", "d" * 64),
            ),
            (
                "Reviewer session",
                lambda item: setattr(
                    item[2].request,
                    "reviewer_name",
                    "asq-other-r001-reviewer",
                ),
            ),
            (
                "generated-path policy",
                lambda item: setattr(
                    item[2].request,
                    "allowed_generated_paths",
                    ("build/",),
                ),
            ),
            (
                "result ID",
                lambda item: setattr(item[2].marker, "result_id", REQUEST_ID),
            ),
        )
        for message, mutate in cases:
            with self.subTest(message=message):
                copied = copy.deepcopy((repository, active, evidence))
                mutate(copied)
                with self.assertRaisesRegex(
                    review_applications.ReviewApplicationError,
                    message,
                ):
                    review_applications._validate_evidence(*copied)

    def test_implementation_identity_rejects_each_changed_component(
        self,
    ) -> None:
        identity = SimpleNamespace(
            implementation_root=Path("/implementation"),
            git_common_dir=Path("/common"),
            worktree_git_dir=Path("/worktree-git"),
            repository_id="repository-id",
            start_branch_ref="refs/heads/feature",
            start_head_detached=False,
        )
        repository = SimpleNamespace(worktree=object())
        active = SimpleNamespace(repository=copy.deepcopy(identity))
        with mock.patch.object(
            review_applications.runs,
            "repository_identity",
            return_value=identity,
        ):
            review_applications._validate_implementation_identity(
                repository,
                active,
            )

        fields = (
            "implementation_root",
            "git_common_dir",
            "worktree_git_dir",
            "repository_id",
            "start_branch_ref",
            "start_head_detached",
        )
        for field in fields:
            with self.subTest(field=field):
                changed = copy.deepcopy(identity)
                current = getattr(changed, field)
                setattr(
                    changed,
                    field,
                    not current
                    if isinstance(current, bool)
                    else f"{current}-changed",
                )
                with (
                    mock.patch.object(
                        review_applications.runs,
                        "repository_identity",
                        return_value=changed,
                    ),
                    self.assertRaisesRegex(
                        review_applications.ReviewApplicationError,
                        "does not match the active run",
                    ),
                ):
                    review_applications._validate_implementation_identity(
                        repository,
                        active,
                    )


class AuthoritativeTransitionTests(unittest.TestCase):
    def test_rollback_refuses_to_overwrite_concurrently_changed_record(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            record_path = root / "run.json"
            state_path = root / "state.json"
            original_record = b"original record\n"
            staged_record = b"staged record\n"
            external_record = b"external record\n"
            original_state = b"original state\n"
            record_path.write_bytes(original_record)
            state_path.write_bytes(original_state)
            real_atomic_write = review_applications.atomic_write

            def fail_state(path, content, *, mode):
                if path == state_path:
                    record_path.write_bytes(external_record)
                    raise OSError("state commit failed")
                real_atomic_write(path, content, mode=mode)

            with (
                mock.patch.object(
                    review_applications,
                    "atomic_write",
                    side_effect=fail_state,
                ),
                self.assertRaisesRegex(
                    review_applications.ReviewApplicationError,
                    "rollback also failed.*content changed",
                ),
            ):
                review_applications._persist_authoritative_transition(
                    records=(
                        review_applications._StagedRecord(
                            path=record_path,
                            original=original_record,
                            staged=staged_record,
                        ),
                    ),
                    state_path=state_path,
                    original_state=original_state,
                    next_state=b"next state\n",
                    failure_message="transition failed",
                )

            self.assertEqual(record_path.read_bytes(), external_record)
            self.assertEqual(state_path.read_bytes(), original_state)

    def test_external_state_change_preserves_staged_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            record_path = root / "run.json"
            state_path = root / "state.json"
            original_record = b"original record\n"
            staged_record = b"staged record\n"
            original_state = b"original state\n"
            external_state = b"external state\n"
            record_path.write_bytes(original_record)
            state_path.write_bytes(original_state)
            real_atomic_write = review_applications.atomic_write

            def replace_state(path, content, *, mode):
                if path == state_path:
                    state_path.write_bytes(external_state)
                    raise OSError("state commit lost race")
                real_atomic_write(path, content, mode=mode)

            with (
                mock.patch.object(
                    review_applications,
                    "atomic_write",
                    side_effect=replace_state,
                ),
                self.assertRaisesRegex(
                    review_applications.ReviewApplicationError,
                    "state changed unexpectedly, so staged metadata was left",
                ),
            ):
                review_applications._persist_authoritative_transition(
                    records=(
                        review_applications._StagedRecord(
                            path=record_path,
                            original=original_record,
                            staged=staged_record,
                        ),
                    ),
                    state_path=state_path,
                    original_state=original_state,
                    next_state=b"next state\n",
                    failure_message="transition failed",
                )

            self.assertEqual(record_path.read_bytes(), staged_record)
            self.assertEqual(state_path.read_bytes(), external_state)


class CompletionPreconditionTests(unittest.TestCase):
    def test_complete_rejects_approved_status_without_authority(self) -> None:
        repository = SimpleNamespace(
            worktree=SimpleNamespace(invocation_directory=Path("/repo"))
        )
        active = SimpleNamespace(
            run_id=RUN_ID,
            phase=runs.RunPhase.APPROVED,
            approval=None,
        )
        with (
            mock.patch.object(
                review_applications.runs,
                "inspect_status_locked",
                return_value=SimpleNamespace(active_run=active),
            ),
            self.assertRaisesRegex(
                review_applications.ReviewApplicationError,
                "missing its exact approval authority",
            ),
        ):
            review_applications._complete_run_locked(repository)


class RoundReplayGuardTests(unittest.TestCase):
    def test_active_round_comparison_rejects_each_changed_authority(
        self,
    ) -> None:
        active_round = SimpleNamespace(
            request_id=REQUEST_ID,
            reviewer_name="asq-876543214321-r001-reviewer",
        )
        active = SimpleNamespace(
            active_round=active_round,
            run_id=RUN_ID,
            current_round=1,
            base_oid="a" * 40,
            current_head_oid="b" * 40,
        )
        record = SimpleNamespace(
            run_id=RUN_ID,
            round_number=1,
            request_id=REQUEST_ID,
            result_id=None,
            status=RoundStatus.REVIEWING,
            base_oid="a" * 40,
            head_oid="b" * 40,
            reviewer_name=active_round.reviewer_name,
        )
        review_applications._assert_round_is_active(record, active)

        fields = (
            "run_id",
            "round_number",
            "request_id",
            "result_id",
            "status",
            "base_oid",
            "head_oid",
            "reviewer_name",
        )
        for field in fields:
            with self.subTest(field=field):
                changed = copy.deepcopy(record)
                values = {
                    "run_id": REQUEST_ID,
                    "round_number": 2,
                    "request_id": RUN_ID,
                    "result_id": RESULT_ID,
                    "status": RoundStatus.APPLIED,
                    "base_oid": "c" * 40,
                    "head_oid": "c" * 40,
                    "reviewer_name": "asq-other-r001-reviewer",
                }
                setattr(changed, field, values[field])
                with self.assertRaisesRegex(
                    review_applications.ReviewApplicationError,
                    "changed during application",
                ):
                    review_applications._assert_round_is_active(
                        changed,
                        active,
                    )

        missing = copy.deepcopy(active)
        missing.active_round = None
        with self.assertRaisesRegex(
            review_applications.ReviewApplicationError,
            "has no active round",
        ):
            review_applications._assert_round_is_active(record, missing)

    def test_approved_replay_rejects_missing_mismatched_and_event_failure(
        self,
    ) -> None:
        repository = SimpleNamespace(control_root=Path("/control"))
        missing = SimpleNamespace(active_round=None, approval=None)
        with self.assertRaisesRegex(
            review_applications.ReviewApplicationError,
            "missing its applied result authority",
        ):
            review_applications._approved_replay(
                repository,
                missing,
                presented_result_id=None,
            )

        approval = SimpleNamespace(
            created_at="2026-08-28T00:00:00Z",
            run_id=RUN_ID,
            round_number=1,
            request_id=REQUEST_ID,
            result_id=RESULT_ID,
            head_oid="b" * 40,
        )
        active = SimpleNamespace(
            active_round=SimpleNamespace(result_id=RESULT_ID),
            approval=approval,
            run_id=RUN_ID,
            current_round=1,
        )
        with self.assertRaisesRegex(
            review_applications.ReviewApplicationError,
            "is not the result that approved",
        ):
            review_applications._approved_replay(
                repository,
                active,
                presented_result_id=REQUEST_ID,
            )

        with (
            mock.patch.object(
                review_applications.runs,
                "safe_run_directory",
                return_value=Path("/run"),
            ),
            mock.patch.object(
                review_applications,
                "_ensure_event",
                side_effect=OSError("disk full"),
            ),
            self.assertRaisesRegex(
                review_applications.ReviewApplicationError,
                "missing event could not be recovered",
            ),
        ):
            review_applications._approved_replay(
                repository,
                active,
                presented_result_id=RESULT_ID,
            )


if __name__ == "__main__":
    unittest.main()
