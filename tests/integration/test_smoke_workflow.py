"""The forge smoke exercises the complete loop and owned cleanup."""

import unittest
from unittest.mock import patch

from tests.smoke_workflow import run_smoke, run_scenario
from tests.forge_support import ForgeFixture, ForgejoFixture


class SmokeTests(unittest.TestCase):
    def test_scripted_forge_scenario(self) -> None:
        result = run_smoke()
        self.assertTrue(result["ok"])
        self.assertIn("removed", result["cleanup"])
        self.assertEqual(
            [(s["forge"], s["identity_mode"]) for s in result["scenarios"]],
            [("github", "dual"), ("forgejo", "single")],
        )
        for scenario in result["scenarios"]:
            self.assertTrue(scenario["ok"])
            self.assertEqual([s["step"] for s in scenario["steps"]],
                             list(range(1, 13)))
        self.assertTrue(result["github_single"]["ok"])

    def test_failed_command_removes_the_owned_temporary_root(self) -> None:
        fixtures = []

        def fail_command(fixture, *args, **kwargs):
            # Keep the fixture alive so only explicit cleanup removes it.
            fixtures.append(fixture)
            self.addCleanup(fixture.temporary.cleanup)
            raise RuntimeError("injected CLI failure")

        for fixture_type in (ForgeFixture, ForgejoFixture):
            with self.subTest(forge=fixture_type.__name__):
                with patch.object(fixture_type, "cli", fail_command):
                    with self.assertRaisesRegex(RuntimeError,
                                                "injected CLI failure"):
                        run_scenario(fixture_type)
                self.assertFalse(fixtures[-1].root.exists())
        self.assertEqual(len(fixtures), 2)

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
