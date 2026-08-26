from __future__ import annotations

from enum import StrEnum
from pathlib import Path
import unittest

from tests._support import add_src_to_path


add_src_to_path()

from agent_squad.validation import JsonValidator  # noqa: E402


class DomainError(ValueError):
    pass


class ExampleKind(StrEnum):
    FIRST = "first"
    SECOND = "second"


class JsonValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = JsonValidator(DomainError)

    def test_object_fields_and_domain_error_are_preserved(self) -> None:
        value = {"required": 1, "optional": 2}

        self.assertIs(self.validator.require_object(value, "value"), value)
        self.validator.check_fields(
            value,
            required={"required"},
            optional={"optional"},
            path="value",
        )

        with self.assertRaisesRegex(DomainError, "missing required"):
            self.validator.check_fields(
                {},
                required={"required"},
                path="value",
            )
        with self.assertRaisesRegex(DomainError, "unknown field"):
            self.validator.check_fields(
                {"required": 1, "extra": 2},
                required={"required"},
                path="value",
            )

    def test_string_null_policy_is_explicit(self) -> None:
        with self.assertRaisesRegex(DomainError, "null bytes"):
            self.validator.require_string("bad\x00value", "value")

        permissive = JsonValidator(
            DomainError,
            reject_null_strings=False,
        )
        self.assertEqual(
            permissive.require_string("kept\x00value", "value"),
            "kept\x00value",
        )

    def test_protocol_scalars_share_one_contract(self) -> None:
        run_id = "12345678-1234-5678-9234-567812345678"
        timestamp = "2026-08-26T01:02:03Z"

        self.assertEqual(
            self.validator.require_uuid(run_id, "run_id"),
            run_id,
        )
        self.assertEqual(
            self.validator.require_digest("a" * 64, "digest"),
            "a" * 64,
        )
        self.assertEqual(
            self.validator.require_oid("b" * 40, "sha1", "head"),
            "b" * 40,
        )
        self.assertEqual(
            self.validator.require_timestamp(timestamp, "created_at"),
            timestamp,
        )
        self.assertIs(
            self.validator.require_enum("second", "kind", ExampleKind),
            ExampleKind.SECOND,
        )
        self.assertIsNone(
            self.validator.require_optional_string(None, "optional")
        )
        self.assertEqual(
            self.validator.require_optional_string("value", "optional"),
            "value",
        )
        self.assertEqual(
            self.validator.require_absolute_path("/tmp/value", "path"),
            Path("/tmp/value"),
        )
        self.assertEqual(
            self.validator.require_object_format("sha256", "format"),
            "sha256",
        )

        cases = (
            (lambda: self.validator.require_uuid("bad", "run_id"), "UUID"),
            (
                lambda: self.validator.require_digest("A" * 64, "digest"),
                "lowercase SHA-256",
            ),
            (
                lambda: self.validator.require_oid("b" * 12, "sha1", "head"),
                "full lowercase",
            ),
            (
                lambda: self.validator.require_oid(
                    "b" * 40,
                    "sha512",
                    "head",
                ),
                "unsupported Git object format",
            ),
            (
                lambda: self.validator.require_timestamp("not-time", "time"),
                "RFC 3339",
            ),
            (
                lambda: self.validator.require_enum(
                    "unknown",
                    "kind",
                    ExampleKind,
                ),
                "must be one of",
            ),
            (
                lambda: self.validator.require_optional_string(
                    1,
                    "optional",
                ),
                "non-empty string",
            ),
            (
                lambda: self.validator.require_absolute_path(
                    "relative",
                    "path",
                ),
                "absolute path",
            ),
            (
                lambda: self.validator.require_object_format(
                    "sha512",
                    "format",
                ),
                "sha1 or sha256: 'sha512'",
            ),
        )
        for operation, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(DomainError, message):
                    operation()


if __name__ == "__main__":
    unittest.main()
