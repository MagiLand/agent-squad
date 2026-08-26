#!/usr/bin/env python3
"""Deterministic fake of the narrow Herdr surface used by tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


METHODS = {
    "agent.get": "AgentTarget",
    "agent.prompt": "AgentPromptParams",
    "agent.start": "AgentStartParams",
    "worktree.open": "WorktreeOpenParams",
}
RESULT_TYPES = [
    "agent_info",
    "agent_prompted",
    "agent_started",
    "session_snapshot",
    "worktree_opened",
]
PARAMETER_FIELDS = {
    "AgentTarget": ["target"],
    "AgentPromptParams": ["target", "text"],
    "AgentStartParams": ["name", "kind", "pane_id", "args"],
    "WorktreeOpenParams": ["path", "label", "focus"],
}


def main() -> int:
    arguments = sys.argv[1:]
    state_root_text = os.environ.get("FAKE_HERDR_STATE_DIR")
    if state_root_text is None:
        return _error("fake_configuration", "missing fake state directory")
    state_root = Path(state_root_text)
    state_root.mkdir(parents=True, exist_ok=True)
    event: dict[str, object] = {
        "arguments": arguments,
        "cwd": str(Path.cwd()),
    }
    if arguments[:2] == ["agent", "prompt"]:
        state_path = Path.cwd() / ".agent-squad/state.json"
        if state_path.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            event["phase_at_prompt"] = state.get("phase")
            event["request_id_at_prompt"] = (
                state.get("active_round") or {}
            ).get("request_id")
        marker_path = (
            Path.cwd() / ".agent-squad-review/local-state.json"
        )
        if marker_path.is_file():
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            event["review_marker_at_prompt"] = marker
    _append_event(state_root / "invocations.jsonl", event)

    if arguments == ["--version"]:
        print("herdr test-0.8.2")
        return 0
    if arguments == ["api", "schema", "--json"]:
        missing = os.environ.get("FAKE_HERDR_SCHEMA_MISSING")
        methods = {
            method: definition
            for method, definition in METHODS.items()
            if method != missing
        }
        results = [item for item in RESULT_TYPES if item != missing]
        _print_json(
            {
                "schema_version": 1,
                "protocol": 20,
                "schemas": {
                    "request": {
                        "$defs": {
                            name: {
                                "properties": {
                                    field: {"type": "string"}
                                    for field in fields
                                }
                            }
                            for name, fields in PARAMETER_FIELDS.items()
                        },
                        "oneOf": [
                            {
                                "properties": {
                                    "method": {"const": method},
                                    "params": {
                                        "$ref": (
                                            "#/schemas/request/$defs/"
                                            f"{definition}"
                                        )
                                    },
                                }
                            }
                            for method, definition in methods.items()
                        ],
                    },
                    "success_response": {
                        "$defs": {
                            "ResponseResult": {
                                "oneOf": [
                                    {
                                        "properties": {
                                            "type": {"const": result_type}
                                        }
                                    }
                                    for result_type in results
                                ]
                            }
                        }
                    },
                },
            }
        )
        return 0
    if arguments == ["api", "snapshot"]:
        _success(
            "session_snapshot",
            snapshot={"version": "test-0.8.2", "protocol": 20},
        )
        return 0
    if arguments == ["integration", "status"]:
        print("claude: current (test)")
        print("codex: current (test)")
        return 0
    if arguments == ["agent", "--help"]:
        print("Commands: list get start prompt")
        return 0
    if arguments == ["agent", "start", "--help"]:
        print("Usage: start <NAME> --kind <KIND> --pane <ID>")
        return 0
    if arguments == ["agent", "prompt", "--help"]:
        print("Usage: prompt <TARGET> <TEXT>")
        return 0
    if arguments == ["agent", "get", "--help"]:
        print("Usage: get <target>")
        return 0
    if arguments == ["worktree", "--help"]:
        print("Commands: list open")
        return 0
    if arguments == ["worktree", "open", "--help"]:
        print("Usage: open --path <PATH> --label <TEXT> --no-focus")
        return 0

    agent_path = state_root / "agent.json"
    opened_path = state_root / "opened.json"
    if arguments[:2] == ["agent", "get"]:
        if not agent_path.is_file():
            return _error("agent_not_found", "fake Reviewer not found")
        reviewer = json.loads(agent_path.read_text(encoding="utf-8"))
        agent = _agent_for_target(arguments[2], reviewer, state_root)
        if agent is None:
            return _error("agent_not_found", "fake agent not found")
        _success("agent_info", agent=agent)
        return 0
    if arguments[:2] == ["worktree", "open"]:
        path = Path(_option(arguments, "--path")).resolve(strict=True)
        opened = {
            "path": str(path),
            "workspace_id": "w-test",
            "pane_id": "w-test:p1",
        }
        opened_path.write_text(json.dumps(opened), encoding="utf-8")
        _success(
            "worktree_opened",
            already_open=False,
            worktree={"path": str(path)},
            workspace={"workspace_id": "w-test"},
            tab={"tab_id": "w-test:t1"},
            root_pane={"pane_id": "w-test:p1"},
        )
        return 0
    if arguments[:2] == ["agent", "start"]:
        if os.environ.get("FAKE_HERDR_FAIL_START") == "1":
            return _error("agent_not_ready", "injected start failure")
        opened = json.loads(opened_path.read_text(encoding="utf-8"))
        agent = {
            "name": arguments[2],
            "agent": _option(arguments, "--kind"),
            "cwd": opened["path"],
            "workspace_id": opened["workspace_id"],
            "pane_id": opened["pane_id"],
        }
        agent_path.write_text(json.dumps(agent), encoding="utf-8")
        _success("agent_started", agent=agent, argv=arguments)
        return 0
    if arguments[:2] == ["agent", "prompt"]:
        if os.environ.get("FAKE_HERDR_FAIL_PROMPT") == "1":
            return _error("agent_blocked", "injected prompt failure")
        reviewer = json.loads(agent_path.read_text(encoding="utf-8"))
        agent = _agent_for_target(arguments[2], reviewer, state_root)
        if agent is None:
            return _error("agent_not_found", "fake agent not found")
        _success("agent_prompted", agent=agent)
        return 0
    return _error(
        "unsupported_command",
        f"unsupported fake Herdr command: {arguments!r}",
        status=2,
    )


def _option(arguments: list[str], name: str) -> str:
    try:
        return arguments[arguments.index(name) + 1]
    except (ValueError, IndexError):
        raise SystemExit(f"missing fake option {name}") from None


def _agent_for_target(
    target: str,
    reviewer: dict[str, object],
    state_root: Path,
) -> dict[str, object] | None:
    if reviewer.get("name") == target:
        return reviewer
    implementer_name = os.environ.get(
        "FAKE_HERDR_IMPLEMENTER_NAME",
        "codex-main",
    )
    if target != implementer_name:
        return None
    return {
        "name": implementer_name,
        "agent": os.environ.get("FAKE_HERDR_IMPLEMENTER_KIND", "codex"),
        "cwd": os.environ.get(
            "FAKE_HERDR_IMPLEMENTER_CWD",
            str(state_root),
        ),
        "workspace_id": "w-implementer",
        "pane_id": "w-implementer:p1",
    }


def _success(result_type: str, **values: object) -> None:
    _print_json(
        {
            "id": "fake:response",
            "result": {"type": result_type, **values},
        }
    )


def _error(code: str, message: str, *, status: int = 1) -> int:
    print(
        json.dumps(
            {
                "id": "fake:error",
                "error": {"code": code, "message": message},
            }
        ),
        file=sys.stderr,
    )
    return status


def _print_json(value: object) -> None:
    print(json.dumps(value, separators=(",", ":")))


def _append_event(path: Path, event: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as event_log:
        event_log.write(json.dumps(event, separators=(",", ":")))
        event_log.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
