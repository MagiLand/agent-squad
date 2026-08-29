from __future__ import annotations

import fcntl
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import unittest

from tests._support import add_src_to_path


add_src_to_path()

from agent_squad import storage  # noqa: E402
from agent_squad.storage import (  # noqa: E402
    atomic_write,
    encode_event,
    encode_json,
    exclusive_file_lock,
    inspect_regular_tree,
    read_regular_tree,
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


class RegularTreeTests(unittest.TestCase):
    def test_inspects_paths_without_reading_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            nested = root / "nested"
            nested.mkdir()
            (root / "root.txt").write_bytes(b"root\n")
            (nested / "leaf.txt").write_bytes(b"leaf\n")

            tree = inspect_regular_tree(
                root,
                label="fixture tree",
                error_type=ValueError,
            )

            self.assertEqual(
                tree.files,
                {
                    PurePosixPath("root.txt"),
                    PurePosixPath("nested/leaf.txt"),
                },
            )
            self.assertEqual(
                tree.directories,
                {PurePosixPath("nested")},
            )

    def test_reads_regular_file_contents_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "first.txt").write_bytes(b"first\n")
            (root / "second.txt").write_bytes(b"second\n")

            files = read_regular_tree(
                root,
                label="fixture tree",
                error_type=ValueError,
            )

            self.assertEqual(
                files,
                {
                    PurePosixPath("first.txt"): b"first\n",
                    PurePosixPath("second.txt"): b"second\n",
                },
            )

    def test_rejects_missing_non_directory_and_linked_trees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            missing = root / "missing"
            regular = root / "regular"
            regular.write_bytes(b"file\n")
            linked_root = root / "linked-root"
            linked_root.symlink_to(regular)
            tree = root / "tree"
            tree.mkdir()
            external_file = root / "external.txt"
            external_file.write_bytes(b"external\n")
            (tree / "linked-file").symlink_to(external_file)

            cases = (
                (missing, "cannot inspect fixture tree"),
                (regular, "must be a normal directory"),
                (linked_root, "must be a normal directory"),
                (tree, "must be a regular non-symlink file"),
            )
            for path, message in cases:
                with self.subTest(path=path):
                    with self.assertRaisesRegex(ValueError, message):
                        inspect_regular_tree(
                            path,
                            label="fixture tree",
                            error_type=ValueError,
                        )

            (tree / "linked-file").unlink()
            external_directory = root / "external-directory"
            external_directory.mkdir()
            (tree / "linked-directory").symlink_to(
                external_directory,
                target_is_directory=True,
            )
            with self.assertRaisesRegex(
                ValueError,
                "directory must not be a symlink",
            ):
                inspect_regular_tree(
                    tree,
                    label="fixture tree",
                    error_type=ValueError,
                )

    def test_rejects_case_colliding_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            folded: dict[str, PurePosixPath] = {}
            storage._record_tree_path(
                root / "Plan.md",
                root=root,
                label="fixture tree",
                folded=folded,
                error_type=ValueError,
            )

            with self.assertRaisesRegex(
                ValueError,
                "case-colliding paths",
            ):
                storage._record_tree_path(
                    root / "plan.md",
                    root=root,
                    label="fixture tree",
                    folded=folded,
                    error_type=ValueError,
                )


if __name__ == "__main__":
    unittest.main()
