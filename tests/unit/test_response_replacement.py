from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from tests._support import add_src_to_path
from tests.unit.test_artifacts import (
    _developer_resolution,
    _escalation,
    _review_response,
    _review_result,
)

add_src_to_path()
from agent_squad import submissions  # noqa: E402
from agent_squad.artifacts import (  # noqa: E402
    DeveloperResolution,
    EscalationRecord,
    ReviewResponse,
    ReviewResult,
    SubmissionMode,
    validate_review_response,
)
from agent_squad.storage import encode_json  # noqa: E402


class ResponseReplacementTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.round_directory = Path(temporary.name) / "rounds/001"
        self.round_directory.mkdir(parents=True)
        review = _review_result()
        optional = copy.deepcopy(review["findings"][0])
        optional.update(id="REV-002", blocking=False, severity="low")
        review["findings"].append(optional)
        self.review = ReviewResult.from_dict(review, object_format="sha1")
        self.previous = SimpleNamespace(
            review=self.review, review_bytes=encode_json(review),
            round_directory=self.round_directory,
        )
        self.earlier = _review_response()
        self.earlier["responses"][0].update(
            disposition="needs_human",
            rationale="Developer must decide the blocking behavior.",
        )
        optional_response = copy.deepcopy(self.earlier["responses"][0])
        optional_response.update(
            finding_id="REV-002", disposition="needs_human",
            rationale="Developer must decide the optional behavior.",
        )
        self.earlier["responses"].append(optional_response)
        self.original = encode_json(self.earlier)
        escalation = _escalation()
        escalation.update(
            source_result_id=self.review.result_id,
            response_id=self.earlier["response_id"],
            related_finding_ids=["REV-001", "REV-002"],
            previous_phase="implementing", reason="implementer_requested",
        )
        self.escalation = EscalationRecord.from_dict(
            escalation, object_format="sha1", label="test escalation",
        )
        self.resolution = DeveloperResolution.from_dict(
            _developer_resolution(), label="test resolution",
        )
        self.unrelated = replace(
            self.resolution,
            resolution_id="44444444-4444-4444-8444-444444444444",
            resolves_escalation_id="55555555-5555-4555-8555-555555555555",
        )
        self.active = SimpleNamespace(
            current_round=1, git_object_format="sha1",
            escalations=(SimpleNamespace(record=self.escalation),),
            resolutions=(
                SimpleNamespace(record=self.unrelated),
                SimpleNamespace(record=self.resolution),
            ),
        )
        self.response = copy.deepcopy(self.earlier)
        self.response.update(
            response_id="66666666-6666-4666-8666-666666666666",
            supersedes_response_id=self.earlier["response_id"],
            resolution_ids=[self.resolution.resolution_id],
        )
        self.response["responses"][0].update(
            disposition="fixed",
            rationale="Implemented the blocking behavior per the resolution.",
        )
        self.response["responses"][1].update(
            disposition="fixed",
            rationale="Implemented the optional behavior per the resolution.",
        )

    def plan(self) -> tuple[Path, bytes] | None:
        # Each candidate passes the existing completeness/mode validator.
        # Refusals below must therefore exercise the replacement contract.
        parsed = ReviewResponse.from_dict(self.response, object_format="sha1")
        validate_review_response(
            parsed, self.review, SubmissionMode.NEW_REVISION,
        )
        return submissions._plan_response_archival(
            self.active, self.previous, encode_json(self.response),
            self.original,
        )

    def test_replacement_requires_resolution_of_its_own_escalation(
        self,
    ) -> None:
        self.response["resolution_ids"] = [self.unrelated.resolution_id]
        with self.assertRaisesRegex(
            submissions.SubmissionError,
            "replacement must include the resolution linked to its escalation",
        ):
            self.plan()

    def test_former_optional_needs_human_finding_must_be_answered(
        self,
    ) -> None:
        self.response["responses"].pop()
        with self.assertRaisesRegex(
            submissions.SubmissionError,
            "replacement must resolve every former needs_human finding",
        ):
            self.plan()

    def test_resolution_must_apply_to_the_former_needs_human_finding(
        self,
    ) -> None:
        self.active.resolutions = (SimpleNamespace(record=replace(
            self.resolution, applies_to_finding_ids=("REV-001",),
        )),)
        with self.assertRaisesRegex(
            submissions.SubmissionError,
            "applicable Developer resolution for every former needs_human",
        ):
            self.plan()

    def test_linked_applicable_resolution_plans_archive_without_writes(
        self,
    ) -> None:
        self.assertEqual(self.plan(), (
            self.round_directory / "diagnostics/replaced-responses"
            / f"{self.earlier['response_id']}.json",
            self.original,
        ))
        self.assertEqual(list(self.round_directory.iterdir()), [])

    def test_linked_resolution_can_address_the_escalation_as_a_whole(
        self,
    ) -> None:
        self.active.resolutions = (SimpleNamespace(record=replace(
            self.resolution, applies_to_finding_ids=(),
        )),)
        self.assertIsNotNone(self.plan())

    def test_matching_provisional_bytes_still_require_valid_linkage(
        self,
    ) -> None:
        archive = (
            self.round_directory / "diagnostics/replaced-responses"
            / f"{self.earlier['response_id']}.json"
        )
        archive.parent.mkdir(parents=True)
        archive.write_bytes(self.original)
        self.response["resolution_ids"] = [self.unrelated.resolution_id]
        self.original = encode_json(self.response)
        with self.assertRaisesRegex(
            submissions.SubmissionError,
            "replacement must include the resolution linked to its escalation",
        ):
            self.plan()


if __name__ == "__main__":
    unittest.main()
