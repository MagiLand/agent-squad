"""Forge response boundaries, including values produced by the browser."""

from pathlib import Path
import unittest
from unittest.mock import patch

from tests._support import add_src_to_path

add_src_to_path()

from agent_squad.forge import (  # noqa: E402
    Evidence,
    ForgeError,
    GitHub,
    PullRequest,
    Review,
    boolean,
    oid,
)
from agent_squad.initialization import Repository  # noqa: E402
from tests.unit.test_conventions import (  # noqa: E402
    A,
    H,
    config,
    derive_state,
    evidence,
    render_line,
    snapshot,
)


def forge_evidence(body: object = "") -> dict:
    return {
        "id": 10,
        "user": {"login": "dev"},
        "created_at": "2026-01-01T00:00:10Z",
        "body": body,
    }


class ForgeValidationTests(unittest.TestCase):
    def test_null_body_is_empty_but_other_non_strings_are_refused(
        self,
    ) -> None:
        self.assertEqual(Evidence.from_dict(forge_evidence(None)).body, "")
        for invalid in (False, 0, [], {}, 42):
            with (
                self.subTest(body=invalid),
                self.assertRaisesRegex(
                    ForgeError, "evidence.body must be a string"
                ),
            ):
                Evidence.from_dict(forge_evidence(invalid))

    def test_browser_line_endings_preserve_decisions_stops_and_task(
        self,
    ) -> None:
        bodies = [
            render_line("decision", finding="none", budget=5)
            + "\n\nDecided.\n\n## Task\n\nAmended objective.\n",
            render_line("stop", head=H, reason="design") + "\n\nStop.\n",
        ]
        for body in bodies:
            with self.subTest(body=body):
                normalized = Evidence.from_dict(
                    forge_evidence(body.replace("\n", "\r\n"))
                )
                self.assertEqual(normalized.body, body)
                self.assertEqual(
                    derive_state(snapshot(conversation=(normalized,))),
                    derive_state(
                        snapshot(conversation=(evidence(10, body, "dev"),))
                    ),
                )
        state = derive_state(
            snapshot(
                conversation=(
                    Evidence.from_dict(
                        forge_evidence(bodies[0].replace("\n", "\r\n"))
                    ),
                )
            )
        )
        self.assertEqual(state["budget"]["effective"], 5)
        self.assertEqual(
            state["effective_task"], "## Task\n\nAmended objective."
        )

    def test_boolean_and_forge_state_validators(self) -> None:
        for value in (H[:7], H.upper(), "g" * 40, H + "0"):
            with (
                self.subTest(sha=value),
                self.assertRaisesRegex(ForgeError, "full lowercase Git SHA"),
            ):
                oid(value, "head.sha")
        for value in (None, 0, 1, "false"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ForgeError, "must be a boolean"),
            ):
                boolean(value, "merged")
        record = dict(
            forge_evidence(),
            commit_id=H,
            state="UNKNOWN",
            submitted_at="2026-01-01T00:00:10Z",
        )
        with self.assertRaisesRegex(ForgeError, "unknown review state"):
            Review.from_dict(record)
        pr = dict(
            forge_evidence(),
            number=1,
            title="Fixture",
            merged=False,
            head={"sha": H, "ref": "feature"},
            base={"sha": A, "ref": "main"},
            state="UNKNOWN",
        )
        with self.assertRaisesRegex(
            ForgeError, "state must be open or closed"
        ):
            PullRequest.from_dict(pr)

    def test_token_and_resolution_confirmation_validators(self) -> None:
        path = Path("/repo")
        repo = Repository(path, path, path / ".git", config())
        for value in ("", "\n", "token\nsecond", "token\rsecond"):
            forge = GitHub(repo, "implementer")
            with (
                self.subTest(value=value),
                patch.object(forge, "_run", return_value=value),
                self.assertRaisesRegex(ForgeError, "invalid token response"),
            ):
                forge.token()
        forge = GitHub(repo, "reviewer")
        for thread in (
            {"id": "wrong", "isResolved": True},
            {"id": "T-1", "isResolved": False},
            {"id": "T-1", "isResolved": 1},
        ):
            with (
                self.subTest(thread=thread),
                patch.object(
                    forge,
                    "api",
                    return_value={
                        "data": {"resolveReviewThread": {"thread": thread}}
                    },
                ),
                self.assertRaisesRegex(ForgeError, "did not confirm"),
            ):
                forge.resolve("T-1")
