"""Reusable JSON-contract validation with domain-specific errors."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
import re
from typing import TypeVar
import uuid


OID_LENGTHS = {"sha1": 40, "sha256": 64}
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
UTC_TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z"
)

EnumValue = TypeVar("EnumValue", bound=StrEnum)


class JsonValidator:
    """Validate common JSON shapes while preserving domain error types."""

    def __init__(
        self,
        error_type: type[Exception],
        *,
        reject_null_strings: bool = True,
    ) -> None:
        self._error_type = error_type
        self._reject_null_strings = reject_null_strings

    def require_object(
        self,
        value: object,
        path: str,
    ) -> dict[str, object]:
        """Require an object with string keys."""

        if not isinstance(value, dict) or not all(
            isinstance(key, str) for key in value
        ):
            raise self._error_type(f"{path} must be a JSON object")
        return value

    def check_fields(
        self,
        data: dict[str, object],
        *,
        required: set[str],
        path: str,
        optional: set[str] | None = None,
    ) -> None:
        """Require an exact set of required and optional object fields."""

        optional_fields = optional or set()
        missing = sorted(required - data.keys())
        if missing:
            raise self._error_type(
                f"{path} is missing required field(s): {', '.join(missing)}"
            )
        unknown = sorted(data.keys() - required - optional_fields)
        if unknown:
            noun = "field" if len(unknown) == 1 else "fields"
            raise self._error_type(
                f"{path} has unknown {noun}: {', '.join(unknown)}"
            )

    def require_string(self, value: object, path: str) -> str:
        """Require a non-empty string under the configured null policy."""

        if not isinstance(value, str) or not value:
            raise self._error_type(f"{path} must be a non-empty string")
        if self._reject_null_strings and "\x00" in value:
            raise self._error_type(f"{path} must not contain null bytes")
        return value

    def require_optional_string(
        self,
        value: object,
        path: str,
    ) -> str | None:
        """Require either null or a valid string."""

        if value is None:
            return None
        return self.require_string(value, path)

    def require_absolute_path(self, value: object, path: str) -> Path:
        """Require a non-empty absolute filesystem path."""

        result = Path(self.require_string(value, path))
        if not result.is_absolute():
            raise self._error_type(f"{path} must be an absolute path")
        return result

    def require_object_format(self, value: object, path: str) -> str:
        """Require a supported Git object format."""

        result = self.require_string(value, path)
        if result not in OID_LENGTHS:
            raise self._error_type(f"{path} must be sha1 or sha256")
        return result

    def require_int(self, value: object, path: str) -> int:
        """Require an integer, excluding booleans."""

        if type(value) is not int:
            raise self._error_type(f"{path} must be an integer")
        return value

    def require_uuid(self, value: object, path: str) -> str:
        """Require a lowercase canonical UUID string."""

        text = self.require_string(value, path)
        try:
            parsed = uuid.UUID(text)
        except ValueError:
            raise self._error_type(
                f"{path} must be a canonical UUID"
            ) from None
        if str(parsed) != text:
            raise self._error_type(f"{path} must be a canonical UUID")
        return text

    def require_digest(self, value: object, path: str) -> str:
        """Require a lowercase SHA-256 digest."""

        text = self.require_string(value, path)
        if SHA256_PATTERN.fullmatch(text) is None:
            raise self._error_type(
                f"{path} must be a lowercase SHA-256 digest"
            )
        return text

    def require_oid(
        self,
        value: object,
        object_format: str,
        path: str,
    ) -> str:
        """Require a full lowercase object ID for a known Git format."""

        text = self.require_string(value, path)
        expected_length = OID_LENGTHS.get(object_format)
        if expected_length is None:
            raise self._error_type(
                f"unsupported Git object format: {object_format!r}"
            )
        if (
            len(text) != expected_length
            or re.fullmatch(r"[0-9a-f]+", text) is None
        ):
            raise self._error_type(
                f"{path} must be a full lowercase {object_format} object ID"
            )
        return text

    def require_timestamp(self, value: object, path: str) -> str:
        """Require an RFC 3339 timestamp normalized to UTC."""

        text = self.require_string(value, path)
        message = f"{path} must be an RFC 3339 UTC timestamp"
        if UTC_TIMESTAMP_PATTERN.fullmatch(text) is None:
            raise self._error_type(message)
        try:
            datetime.fromisoformat(f"{text[:-1]}+00:00")
        except ValueError:
            raise self._error_type(message) from None
        return text

    def require_enum(
        self,
        value: object,
        path: str,
        enum_type: type[EnumValue],
    ) -> EnumValue:
        """Require one value from a string enumeration."""

        text = self.require_string(value, path)
        try:
            return enum_type(text)
        except ValueError:
            supported = ", ".join(member.value for member in enum_type)
            raise self._error_type(
                f"{path} must be one of: {supported}"
            ) from None
