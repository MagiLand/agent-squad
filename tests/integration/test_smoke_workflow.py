"""The forge smoke exercises the complete loop and owned cleanup."""

import unittest
from unittest.mock import patch

from tests.smoke_workflow import run_smoke
from tests.forge_support import ForgeFixture


class SmokeTests(unittest.TestCase):
    def test_scripted_forge_scenario(self) -> None:
        result = run_smoke()
        self.assertTrue(result["ok"])
        self.assertIn("removed", result["cleanup"])
        self.assertEqual([s["step"] for s in result["steps"]],
                         list(range(1, 13)))

    def test_failed_command_removes_the_owned_temporary_root(self) -> None:
        fixtures = []

        def fail_command(fixture, *args, **kwargs):
            # Keep the fixture alive so only explicit cleanup removes it.
            fixtures.append(fixture)
            self.addCleanup(fixture.temporary.cleanup)
            raise RuntimeError("injected CLI failure")

        with patch.object(ForgeFixture, "cli", fail_command):
            with self.assertRaisesRegex(RuntimeError, "injected CLI failure"):
                run_smoke()
        self.assertEqual(len(fixtures), 1)
        self.assertFalse(fixtures[0].root.exists())

    def test_failed_cleanup_reports_the_retained_root(self) -> None:
        fixture = ForgeFixture()
        try:
            with patch.object(fixture.temporary, "cleanup",
                              side_effect=OSError("injected removal failure")):
                with self.assertRaises(RuntimeError) as caught:
                    fixture.close()
            self.assertIn(str(fixture.root), str(caught.exception))
            self.assertIn("retained", str(caught.exception))
            self.assertTrue(fixture.root.exists())
        finally:
            fixture.close()
