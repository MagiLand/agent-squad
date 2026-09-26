"""Right-side commentable lines from Git's unified diff."""

from __future__ import annotations

from pathlib import Path
import re

from .forge import Anchor
from .initialization import AgentSquadError, git_output


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
                raw.append(int(value[index : index + 3], 8))
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


def parse_diff(diff: str) -> dict[str, set[int]]:
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
                if path is not None:
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


def commentable_lines(root: Path, base: str, head: str) -> dict[str, set[int]]:
    return parse_diff(
        git_output(
            root,
            "-c",
            "core.quotePath=true",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--unified=3",
            "--find-renames",
            base,
            head,
            "--",
        )
    )


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
