from __future__ import annotations

import unittest

from tests._support import add_src_to_path


add_src_to_path()

from agent_squad import runs  # noqa: E402


class ReviewBudgetTests(unittest.TestCase):
    def test_initial_budget_round_trips(self) -> None:
        budget = runs.ReviewBudget.initial(4)

        self.assertEqual(
            runs.ReviewBudget.from_dict(budget.to_dict()),
            budget,
        )

    def test_invalid_budget_invariants_are_rejected(self) -> None:
        cases = (
            (
                {
                    "original_limit": 0,
                    "additional_rounds_granted": 0,
                    "effective_limit": 0,
                    "completed_change_reviews": 0,
                },
                "original_limit must be positive",
            ),
            (
                {
                    "original_limit": 4,
                    "additional_rounds_granted": -1,
                    "effective_limit": 3,
                    "completed_change_reviews": 0,
                },
                "additional_rounds_granted must not be negative",
            ),
            (
                {
                    "original_limit": 4,
                    "additional_rounds_granted": 1,
                    "effective_limit": 4,
                    "completed_change_reviews": 0,
                },
                "effective_limit must equal",
            ),
            (
                {
                    "original_limit": 4,
                    "additional_rounds_granted": 0,
                    "effective_limit": 4,
                    "completed_change_reviews": 5,
                },
                "completed_change_reviews must be between",
            ),
        )
        for value, message in cases:
            with self.subTest(value=value):
                with self.assertRaisesRegex(runs.RunStateError, message):
                    runs.ReviewBudget.from_dict(value)

    def test_budget_rejects_missing_unknown_and_boolean_fields(self) -> None:
        valid = runs.ReviewBudget.initial(4).to_dict()
        cases = (
            ({key: value for key, value in valid.items() if key != "effective_limit"},
             "missing required field"),
            ({**valid, "unexpected": 1}, "unknown field"),
            ({**valid, "original_limit": True}, "must be an integer"),
        )
        for value, message in cases:
            with self.subTest(value=value):
                with self.assertRaisesRegex(runs.RunStateError, message):
                    runs.ReviewBudget.from_dict(value)


class ProtocolValueValidationTests(unittest.TestCase):
    def test_full_object_ids_support_sha1_and_sha256(self) -> None:
        sha1 = "a" * 40
        sha256 = "b" * 64

        self.assertEqual(runs._require_oid(sha1, "sha1", "oid"), sha1)
        self.assertEqual(runs._require_oid(sha256, "sha256", "oid"), sha256)

    def test_abbreviated_and_uppercase_object_ids_are_rejected(self) -> None:
        for value, object_format in (("a" * 12, "sha1"), ("A" * 40, "sha1")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    runs.RunStateError,
                    "full lowercase",
                ):
                    runs._require_oid(value, object_format, "oid")

    def test_timestamps_require_rfc3339_utc_form(self) -> None:
        valid = "2026-08-24T12:34:56Z"
        self.assertEqual(runs._require_timestamp(valid, "timestamp"), valid)

        for value in (
            "2026-08-24 12:34:56Z",
            "2026-08-24T12:34:56+00:00",
            "2026-13-24T12:34:56Z",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    runs.RunStateError,
                    "RFC 3339 UTC timestamp",
                ):
                    runs._require_timestamp(value, "timestamp")

    def test_start_selections_reject_ambiguous_text(self) -> None:
        for value in ("", " main", "main ", "main\x00other"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    runs.RunStartError,
                    "must be non-empty",
                ):
                    runs._validate_selection(value, "base reference")


if __name__ == "__main__":
    unittest.main()
