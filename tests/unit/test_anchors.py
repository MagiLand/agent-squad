"""Diff parsing at the boundaries GitHub silently omits."""

from pathlib import Path
import unittest

from tests._support import add_src_to_path

add_src_to_path()
from agent_squad.anchors import (
    Anchor,
    parse_diff,
    unquote_path,
    validate_anchor,
)
from agent_squad.initialization import AgentSquadError


class AnchorTests(unittest.TestCase):
    def test_multiple_hunks_count_only_right_lines(self) -> None:
        diff = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,3 +1,3 @@
 context
-deleted
+added
 end
@@ -20,2 +20,3 @@
 context
+added
 end
"""
        lines = parse_diff(diff)
        self.assertEqual(lines, {"a.py": {1, 2, 3, 20, 21, 22}})
        validate_anchor(Anchor("a.py", 22, 20), lines)
        for anchor in [
            Anchor("a.py", 4),
            Anchor("missing", 2),
            Anchor("a.py", 20, 22),
            Anchor("a.py", 20, 19),
            Anchor("a.py", True),
        ]:
            with self.assertRaises(AgentSquadError):
                validate_anchor(anchor, lines)

    def test_new_deleted_renamed_binary_and_no_trailing_newline(self) -> None:
        diff = """diff --git a/new b/new
new file mode 100644
--- /dev/null
+++ b/new
@@ -0,0 +1,2 @@
+one
+two
\\ No newline at end of file
diff --git a/deleted b/deleted
deleted file mode 100644
--- a/deleted
+++ /dev/null
@@ -1 +0,0 @@
-gone
diff --git a/old b/renamed
similarity index 80%
rename from old
rename to renamed
--- a/old
+++ b/renamed
@@ -1 +1 @@
-old
+new
diff --git a/pic b/pic
Binary files a/pic and b/pic differ
"""
        self.assertEqual(parse_diff(diff), {"new": {1, 2}, "renamed": {1}})

    def test_quoted_git_paths_and_literal_header_text_in_hunk(self) -> None:
        self.assertEqual(
            unquote_path(r'"b/caf\303\251\tname.py"'), "b/café\tname.py"
        )
        diff = (
            "diff --git a/file b/file\n--- a/file\n+++ b/file\n@@ -0,0 +1,2"
            " @@\n+++ a source line\n+@@ another source line\n"
        )
        self.assertEqual(parse_diff(diff), {"file": {1, 2}})

    def test_malformed_diff_hunks_and_headers_are_refused(self) -> None:
        for diff, error in [
            (
                "+++ b/file\n@@ -1 +1 @@\ninvalid\n",
                "invalid unified diff hunk",
            ),
            ("+++ b/file\n@@ invalid @@\n", "invalid unified diff header"),
        ]:
            with (
                self.subTest(diff=diff),
                self.assertRaisesRegex(AgentSquadError, error),
            ):
                parse_diff(diff)
