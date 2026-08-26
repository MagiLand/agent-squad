from __future__ import annotations

import fcntl
import os
from pathlib import Path
import stat
import tempfile
import unittest

from tests._support import add_src_to_path


add_src_to_path()

from agent_squad.storage import (  # noqa: E402
    atomic_write,
    encode_event,
    encode_json,
    exclusive_file_lock,
)


class AtomicWriteTests(unittest.TestCase):
    def test_replaces_complete_content_and_sets_requested_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            destination = root / "state.json"
            destination.write_bytes(b"old content\n")

            atomic_write(destination, b'{"complete":true}\n', mode=0o600)

            self.assertEqual(destination.read_bytes(), b'{"complete":true}\n')
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(list(root.iterdir()), [destination])

    def test_json_encodings_are_canonical(self) -> None:
        value = {"message": "caf\N{LATIN SMALL LETTER E WITH ACUTE}"}

        self.assertEqual(
            encode_json(value),
            b'{\n  "message": "caf\\u00e9"\n}\n',
        )
        self.assertEqual(
            encode_event(value),
            b'{"message":"caf\\u00e9"}\n',
        )


class ExclusiveFileLockTests(unittest.TestCase):
    def test_excludes_an_independent_open_file_description(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            lock_path = Path(temporary_directory) / "lock"

            with exclusive_file_lock(lock_path):
                competing_descriptor = os.open(lock_path, os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(
                            competing_descriptor,
                            fcntl.LOCK_EX | fcntl.LOCK_NB,
                        )
                finally:
                    os.close(competing_descriptor)

            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)

    def test_rejects_a_symlinked_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "external-lock"
            target.write_bytes(b"untouched\n")
            lock_path = root / "lock"
            lock_path.symlink_to(target)

            with self.assertRaises(OSError):
                with exclusive_file_lock(lock_path):
                    self.fail("a symlinked lock must never be acquired")

            self.assertEqual(target.read_bytes(), b"untouched\n")


if __name__ == "__main__":
    unittest.main()
