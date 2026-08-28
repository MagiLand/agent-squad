"""Durable local-file primitives used by Agent Squad state changes."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile


class InvalidJsonError(ValueError):
    """Raised when JSON text is malformed or repeats an object key."""


def decode_json(content: str) -> object:
    """Decode JSON while rejecting duplicate object keys."""

    try:
        return json.loads(
            content,
            object_pairs_hook=_object_without_duplicates,
        )
    except (json.JSONDecodeError, _DuplicateKeyError) as error:
        raise InvalidJsonError(str(error)) from error


def utc_timestamp() -> str:
    """Return the current time as a stable RFC 3339 UTC timestamp."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def encode_json(value: dict[str, object]) -> bytes:
    """Encode one authoritative JSON object using the canonical format."""

    return f"{json.dumps(value, indent=2)}\n".encode("utf-8")


def encode_event(value: dict[str, object]) -> bytes:
    """Encode one compact JSON Lines event record."""

    return f"{json.dumps(value, separators=(',', ':'))}\n".encode("utf-8")


def atomic_write(path: Path, content: bytes, *, mode: int) -> None:
    """Flush *content* to a temporary file and atomically replace *path*."""

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_path.chmod(mode)
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        temporary_path.replace(path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def append_event(path: Path, event: dict[str, object]) -> None:
    """Flush one encoded event onto a regular non-symlink JSONL file."""

    content = encode_event(event)
    flags = os.O_WRONLY | os.O_APPEND
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"event log is not a regular file: {path}")
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written == 0:
                raise OSError(f"short write to event log: {path}")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive process lock on a non-symlink regular file."""

    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"lock path is not a regular file: {path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(descriptor)
            except OSError:
                pass


class _DuplicateKeyError(ValueError):
    pass


def _object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate object key: {key}")
        result[key] = value
    return result
