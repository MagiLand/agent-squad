"""Right-side commentable lines from Git's unified diff."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

from .forge import Anchor
from .initialization import AgentSquadError


def unquote_path(value: str) -> str:
    """Decode Git's quoted UTF-8 paths, including octal byte escapes."""
    if not value.startswith('"'):
        return value
    raw = bytearray()
    index = 1
    while index < len(value) - 1:
        char = value[index]
        if char != "\\":
            raw.extend(char.encode("utf-8"))
        else:
            index += 1
            char = value[index]
            if char in "01234567":
                raw.append(int(value[index:index + 3], 8))
                index += 2
            else:
                raw.extend(
                    {
                        "t": b"\t",
                        "n": b"\n",
                        "r": b"\r",
                        "a": b"\a",
                        "b": b"\b",
                        "f": b"\f",
                        "v": b"\v",
                        "\\": b"\\",
                        '"': b'"',
                    }[char]
                )
        index += 1
    return raw.decode("utf-8")


def parse_diff(diff: str, added_only: bool = False) -> dict[str, set[int]]:
    """Map each head-side path to its right-side lines.

    With ``added_only``, only ``+`` lines count: context lines, including
    any that ``diff.interHunkContext`` adds between hunks, do not.
    """
    result: dict[str, set[int]] = {}
    path = None
    line_number = 0
    remaining = 0
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            path = None
            remaining = 0
        elif remaining:
            if line.startswith((" ", "+")):
                if path is not None and (
                    not added_only or line.startswith("+")
                ):
                    result[path].add(line_number)
                line_number += 1
                remaining -= 1
            elif line.startswith("-") or line.startswith("\\ No newline"):
                continue
            else:
                raise AgentSquadError("invalid unified diff hunk")
        elif line.startswith("+++ "):
            value = unquote_path(line[4:])
            path = None if value == "/dev/null" else value.removeprefix("b/")
            if path is not None:
                result.setdefault(path, set())
        elif line.startswith("@@ "):
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if match is None:
                raise AgentSquadError("invalid unified diff header")
            line_number = int(match[1])
            remaining = int(match[2]) if match[2] is not None else 1
    return result


def patch_output(root: Path, *arguments: str) -> str:
    """Git's output, decoded without universal newlines (REV-7).

    Only LF separates the lines of a patch; a carriage return inside a
    source line stays part of it. A Git failure raises.
    """
    try:
        result = subprocess.run(
            ["git", *arguments], cwd=root, shell=False, capture_output=True,
            timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AgentSquadError(f"git {arguments[0]} failed: {error}") from None
    if result.returncode:
        raise AgentSquadError(
            result.stderr.decode("utf-8", "replace").strip()
            or f"git exited {result.returncode}"
        )
    return result.stdout.decode("utf-8", "replace").rstrip("\n")


def right_side_lines(
    root: Path, base: str, head: str, context: int, added_only: bool = False
) -> dict[str, set[int]]:
    # Settings that change a patch's shape are fixed here: a blank context
    # line keeps its space prefix, and hunks are joined only by --unified.
    return parse_diff(
        patch_output(
            root,
            "-c",
            "core.quotePath=true",
            "-c",
            "diff.suppressBlankEmpty=false",
            "-c",
            "diff.interHunkContext=0",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            f"--unified={context}",
            "--find-renames",
            base,
            head,
            "--",
        ),
        added_only,
    )


def commentable_lines(root: Path, base: str, head: str) -> dict[str, set[int]]:
    return right_side_lines(root, base, head, 3)


def changed_lines(root: Path, base: str, head: str) -> dict[str, set[int]]:
    """Lines added or changed on the head side, without any context."""
    return right_side_lines(root, base, head, 0, added_only=True)


def changed_line_count(root: Path, base: str, head: str) -> int:
    """Added plus deleted lines, as the anchor diff finds renames.

    ``--numstat`` reports a binary file as ``-`` twice, which counts 0.
    With ``-z`` each record ends at a NUL, so no character a path may hold
    splits one (REV-3); a rename's record has an empty path followed by
    its source and destination.
    """
    fields = iter(patch_output(
        root, "diff", "--no-ext-diff", "--no-textconv", "--no-color",
        "--numstat", "-z", "--find-renames", base, head, "--",
    ).split("\0"))
    total = 0
    for record in fields:
        if not record:
            continue  # The empty field after the final NUL.
        added, deleted, path = record.split("\t", 2)
        if not path:
            next(fields), next(fields)
        total += sum(int(count) for count in (added, deleted) if count != "-")
    return total


def validate_anchor(anchor: Anchor, lines: dict[str, set[int]]) -> None:
    available = lines.get(anchor.path, set())
    if (
        type(anchor.line) is not int
        or anchor.line not in available
        or (
            anchor.start_line is not None
            and (
                type(anchor.start_line) is not int
                or anchor.start_line not in available
                or anchor.start_line > anchor.line
            )
        )
    ):
        raise AgentSquadError(
            "anchor is outside the right-side diff:"
            f" {anchor.path}:{anchor.line}"
        )
