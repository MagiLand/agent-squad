"""Command-driven artifact proof with disposable fake Herdr and Git state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any


PROJECT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT / "tests/fixtures/smoke"
STAMP = "2026-09-05T12:00:00Z"


def require(condition: bool, message: str) -> None:
    """Keep smoke assertions active even under Python optimization."""
    if not condition:
        raise AssertionError(message)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def exercise(root: Path) -> dict[str, Any]:
    """Run one full workflow inside a newly allocated, caller-owned root."""
    repository = root / "repository"
    repository.mkdir()
    binary = root / "bin"
    binary.mkdir()
    fake = binary / "herdr"
    # Pin the interpreter rather than relying on the caller's python3 alias.
    fake.write_text(
        f"#!{sys.executable}\n" +
        (PROJECT / "tests/fixtures/fake_herdr.py").read_text(
            encoding="utf-8"
        ).split("\n", 1)[1],
        encoding="utf-8",
    )
    fake.chmod(0o755)
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("GIT_", "FAKE_HERDR_", "HERDR_", "PYTHON"))
    }
    environment.update(
        PATH=str(binary) + os.pathsep + os.environ.get("PATH", ""),
        PYTHONPATH=str(PROJECT / "src"),
        PYTHONDONTWRITEBYTECODE="1",
        XDG_DATA_HOME=str(root / "data"),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        FAKE_HERDR_STATE_DIR=str(root / "fake-herdr-state"),
        FAKE_HERDR_IMPLEMENTER_CWD=str(repository),
    )
    commands: list[dict[str, Any]] = []

    def command(
        *arguments: str,
        cwd: Path = repository,
        expected: int = 0,
        lost: bool = False,
    ) -> str:
        env = dict(environment)
        if lost:
            env["FAKE_HERDR_FAIL_PROMPT"] = "1"
        result = subprocess.run(
            arguments, cwd=cwd, env=env, shell=False, text=True,
            capture_output=True, timeout=60,
        )
        commands.append({"argv": list(arguments), "exit": result.returncode})
        require(
            result.returncode == expected,
            f"{arguments!r}: expected exit {expected}, got "
            f"{result.returncode}\n{result.stdout}\n{result.stderr}",
        )
        return result.stdout

    def cli(*arguments: str, **options: Any) -> str:
        return command(
            sys.executable, "-m", "agent_squad", *arguments, **options,
        )

    def commit(message: str) -> str:
        command("git", "add", "greeting.py", "test_greeting.py")
        command("git", "commit", "-m", message)
        return command("git", "rev-parse", "HEAD").strip()

    command("git", "init", "--initial-branch=main")
    command("git", "config", "user.name", "Agent Squad Smoke")
    command("git", "config", "user.email", "smoke@example.invalid")
    for name in ("greeting.py", "test_greeting.py"):
        shutil.copyfile(FIXTURE / name, repository / name)
    command(sys.executable, "-m", "unittest")
    base = commit("test: seed greeting fixture")
    cli("init")
    task = root / "task.md"
    task_bytes = (FIXTURE / "task.md").read_bytes()
    task.write_bytes(task_bytes)
    cli("start", "--task", str(task), "--base", "main")
    state_path = repository / ".agent-squad/state.json"
    state = read_json(state_path)
    run_id = state["active_run_id"]
    run_dir = repository / ".agent-squad/runs" / run_id
    require((run_dir / "task.md").read_bytes() == task_bytes,
            "task was not captured exactly")
    task.write_text("The external source changed.\n", encoding="utf-8")
    require("Phase: implementing" in cli("status"), "snapshot not usable")
    require(command("git", "status", "--porcelain") == "",
            "run did not start at a clean baseline")

    # The intentional incomplete implementation exists only in this fixture.
    source = repository / "greeting.py"
    source.write_text(
        'def greet(name):\n'
        '    return "Hello, " + (name or "friend") + "!"\n',
        encoding="utf-8",
    )
    first_head = commit("feat: greet an empty name")
    report = root / "report.md"
    report.write_text("# Report\n\nImplemented blank-name greetings.\n",
                      encoding="utf-8")
    cli("submit", "--report", str(report), "--mode", "new_revision")

    def active_bundle() -> tuple[Path, dict[str, Any]]:
        state = read_json(state_path)
        worktree = Path(state["active_round"]["review_worktree"])
        require(worktree.is_relative_to(root), "review escaped owned root")
        bundle = worktree / ".agent-squad-review"
        request = read_json(bundle / "input/request.json")
        require((bundle / request["task"]["path"]).read_bytes() == task_bytes,
                "fresh bundle lost captured task")
        return bundle, request

    def review(
        bundle: Path, request: dict[str, Any], *, approved: bool,
    ) -> dict[str, Any]:
        result = {
            "schema_version": 1, "created_at": STAMP,
            "result_id": (
                "11111111-1111-4111-8111-111111111111" if approved else
                "22222222-2222-4222-8222-222222222222"
            ),
            **{key: request[key] for key in (
                "request_id", "run_id", "round", "base_oid", "head_oid",
            )},
            "verdict": "approved" if approved else "changes_requested",
            "summary": (
                "The tests and implementation obey the Developer resolution."
                if approved else "Whitespace-only input still needs a fix."
            ),
            "findings": [] if approved else [{
                "id": "REV-001", "severity": "high", "blocking": True,
                "category": "correctness", "file": "greeting.py",
                "line_start": 2, "line_end": 2,
                "problem": "Whitespace-only names are not handled.",
                "evidence": "greet('  ') returns 'Hello,   !'.",
                "impact": "The blank-name requirement is not satisfied.",
                "required_change": "Detect whitespace without altering names.",
                "verification": "python -m unittest",
            }],
            "non_blocking_observations": [],
        }
        write_json(bundle / "output/review.json", result)
        (bundle / "output/review.md").write_text(
            "# Review\n\n" + result["summary"] + "\n", encoding="utf-8",
        )
        return result

    first_bundle, first_request = active_bundle()
    require(first_request["base_oid"] == base, "base changed")
    require(first_request["head_oid"] == first_head, "wrong first candidate")
    command(sys.executable, "-c",
            'from greeting import greet; '
            'assert greet("  ") == "Hello, friend!"',
            cwd=first_bundle.parent, expected=1)
    first_review = review(first_bundle, first_request, approved=False)
    cli("review-submit", cwd=first_bundle.parent)
    cli("apply-review", "--result-id", first_review["result_id"])
    require(read_json(state_path)["phase"] == "implementing",
            "changes requested did not resume implementation")
    require(not first_bundle.parent.exists(), "first worktree retained")

    earlier = {
        "schema_version": 1, "created_at": STAMP,
        "response_id": "44444444-4444-4444-8444-444444444444",
        "supersedes_response_id": None, "resolution_ids": [],
        "run_id": run_id, "review_round": 1,
        "review_result_id": first_review["result_id"],
        "reviewed_head_oid": first_head,
        "responses": [{
            "finding_id": "REV-001", "disposition": "needs_human",
            "rationale": "Confirm that padded nonblank names stay unchanged.",
            "changed_files": [], "evidence": [], "verification": "",
        }],
    }
    response = root / "response.json"
    write_json(response, earlier)
    cli("escalate", "--response", str(response))
    escalation = read_json(state_path)["active_escalation_id"]
    require(read_json(state_path)["phase"] == "needs_human",
            "response did not escalate")
    canonical_response = run_dir / "rounds/001/response.json"
    earlier_bytes = canonical_response.read_bytes()
    cli("resume", "--resolution", str(FIXTURE / "resolution.md"),
        "--applies-to-finding", "REV-001", "--extend-rounds", "1")
    resolution_file = run_dir / "resolutions/001-resolution.json"
    resolution = read_json(resolution_file)
    require(resolution["resolves_escalation_id"] == escalation,
            "resolution lost escalation linkage")
    source.write_text(
        'def greet(name):\n'
        '    return "Hello, " + (name if name.strip() else "friend") + "!"\n',
        encoding="utf-8",
    )
    with (repository / "test_greeting.py").open("a", encoding="utf-8") as test:
        test.write(
            '\n    def test_blank_and_padded_names(self):\n'
            '        for name in ("", "  ", "\\t\\n"):\n'
            '            self.assertEqual(greet(name), "Hello, friend!")\n'
            '        self.assertEqual(greet(" Ada "), "Hello,  Ada !")\n'
        )
    command(sys.executable, "-m", "unittest")
    final_head = commit("fix: preserve names while detecting blank input")
    replacement = dict(earlier)
    replacement.update(
        response_id="55555555-5555-4555-8555-555555555555",
        supersedes_response_id=earlier["response_id"],
        resolution_ids=[resolution["resolution_id"]],
        responses=[{
            "finding_id": "REV-001", "disposition": "fixed",
            "rationale": "Preserved nonblank names per Developer resolution "
                         + resolution["resolution_id"],
            "changed_files": ["greeting.py", "test_greeting.py"],
            "evidence": [], "verification": "python -m unittest",
        }],
    )
    write_json(response, replacement)
    cli("submit", "--report", str(report), "--response", str(response),
        "--mode", "new_revision")
    second_bundle, second_request = active_bundle()
    require(first_request["request_id"] != second_request["request_id"],
            "fresh review reused request")
    require(first_request["reviewer_name"] != second_request["reviewer_name"],
            "fresh review reused Reviewer")
    diagnostic = (run_dir / "rounds/001/diagnostics/replaced-responses"
                  / (earlier["response_id"] + ".json"))
    require(diagnostic.read_bytes() == earlier_bytes, "old response lost")
    require((second_bundle / "input/previous-response.json").read_bytes()
            == response.read_bytes() == canonical_response.read_bytes(),
            "replacement response was not propagated")
    require(second_request["resolution_paths"] == [
        "input/resolutions/001-resolution.json",
    ], "resolution not listed in request")
    copied_record = second_bundle / second_request["resolution_paths"][0]
    copied_decision = second_bundle / "input/resolutions/001-resolution.md"
    require(copied_record.read_bytes() == resolution_file.read_bytes(),
            "resolution record not propagated")
    decision_bytes = (FIXTURE / "resolution.md").read_bytes()
    require(copied_decision.read_bytes() == decision_bytes,
            "resolution decision not propagated")
    command(sys.executable, "-m", "unittest", cwd=second_bundle.parent)
    final_review = review(second_bundle, second_request, approved=True)
    lost = cli("review-submit", cwd=second_bundle.parent,
               expected=1, lost=True)
    require("Result notification: failed" in lost, "notification not lost")
    marker = read_json(second_bundle / "local-state.json")
    require(marker["result_id"] == final_review["result_id"], "marker missing")
    discovery = cli("status")
    apply_command = (
        f"agent-squad apply-review --result-id {marker['result_id']}"
    )
    require(apply_command in discovery, "status did not discover result")
    require(apply_command in cli("retry-handoff"), "recovery missed result")
    cli("apply-review", "--result-id", marker["result_id"])
    approved = read_json(state_path)
    require(approved["phase"] == "approved", "approval not recorded")
    require(approved["approved_head_oid"] == final_head, "wrong approval head")
    require(approved["review_budget"]["completed_change_reviews"] == 1,
            "incorrect consumed budget")
    require(approved["review_budget"]["additional_rounds_granted"] == 1,
            "budget extension missing")
    cli("complete")
    cli("complete")
    require("Active run: none" in cli("status"), "active run not released")
    require(read_json(run_dir / "run.json")["phase"] == "completed",
            "completion history missing")
    require(command("git", "status", "--porcelain") == "", "dirty completion")
    require(not second_bundle.parent.exists(), "final worktree retained")
    registrations = command("git", "worktree", "list", "--porcelain")
    require(registrations.count("worktree ") == 1,
            "review registration retained")
    require(command("git", "ls-files", ".agent-squad", ".agent-squad-review")
            == "", "runtime artifacts committed")
    return {
        "result": "passed", "transport": "fake-herdr", "real_model_calls": 0,
        "run_id": run_id, "base_oid": base, "first_head_oid": first_head,
        "approved_head_oid": final_head,
        "task_sha256": hashlib.sha256(task_bytes).hexdigest(),
        "rounds": 2, "response_replaced": True, "resolution_propagated": True,
        "lost_notification_discovered": True, "completed": True,
        "commands": commands,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="print command evidence")
    args = parser.parse_args()
    temporary = tempfile.TemporaryDirectory(prefix="agent-squad-smoke-")
    root = Path(temporary.name).resolve()
    try:
        result = exercise(root)
    except (AssertionError, OSError, subprocess.SubprocessError) as error:
        print(f"Smoke failed: {error}", file=sys.stderr)
        return 1
    finally:
        try:
            # TemporaryDirectory handles immutable archive directories too.
            temporary.cleanup()
        except OSError as error:
            print(f"Retained owned smoke resources: {root}: {error}",
                  file=sys.stderr)
            raise
    result["temporary_root"] = str(root)
    result["retained_resources"] = []
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("Smoke passed: task capture -> changes requested -> escalation "
              "-> Developer resolution -> response replacement "
              "-> fresh review "
              "-> lost-notification discovery -> approval -> completion.")
        print("Fake Herdr; no real model calls. Retained resources: none.")
    return 0
