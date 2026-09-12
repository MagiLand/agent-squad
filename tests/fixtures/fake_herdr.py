#!/usr/bin/env python3
"""Only deterministic discovery and agent inspection;

never launches a model.
"""

import json
import os
import sys

args = sys.argv[1:]
methods = {
    "agent.get": "AgentTarget",
    "agent.prompt": "AgentPromptParams",
    "agent.start": "AgentStartParams",
    "worktree.open": "WorktreeOpenParams",
}
fields = {
    "AgentTarget": ["target"],
    "AgentPromptParams": ["target", "text"],
    "AgentStartParams": ["name", "kind", "pane_id", "args"],
    "WorktreeOpenParams": ["cwd", "path", "label", "focus"],
}
results = [
    "agent_info",
    "agent_prompted",
    "agent_started",
    "session_snapshot",
    "worktree_opened",
]
missing = os.environ.get("FAKE_HERDR_SCHEMA_MISSING")


def output(value):
    print(json.dumps(value))
    raise SystemExit(0)


def response(kind, **values):
    output({"id": "fixture", "result": {"type": kind, **values}})


if args == ["--version"]:
    print("herdr fixture-discovery")
elif args == ["api", "schema", "--json"]:
    output(
        {
            "schema_version": 1,
            "protocol": 20,
            "schemas": {
                "request": {
                    "$defs": {
                        name: {"properties": {f: {} for f in names}}
                        for name, names in fields.items()
                    },
                    "oneOf": [
                        {
                            "properties": {
                                "method": {"const": name},
                                "params": {
                                    "$ref": (
                                        "#/schemas/request/$defs/" + definition
                                    )
                                },
                            }
                        }
                        for name, definition in methods.items()
                        if name != missing
                    ],
                },
                "success_response": {
                    "$defs": {
                        "ResponseResult": {
                            "oneOf": [
                                {"properties": {"type": {"const": name}}}
                                for name in results
                                if name != missing
                            ]
                        }
                    }
                },
            },
        }
    )
elif "--help" in args:
    print(
        "start prompt get open --kind [possible values: codex, claude] --pane"
        " --cwd --path --label --no-focus"
    )
elif args == ["api", "snapshot"]:
    response(
        "session_snapshot",
        snapshot={
            "protocol": int(os.environ.get("FAKE_HERDR_PROTOCOL", "20")),
            "version": "fixture",
        },
    )
elif args == ["integration", "status"]:
    print("codex: " + os.environ.get("FAKE_HERDR_INTEGRATION", "current"))
    print("claude: current")
elif args[:2] == ["agent", "get"]:
    if os.environ.get("FAKE_HERDR_MISSING_AGENT"):
        print(
            json.dumps(
                {
                    "id": "fixture",
                    "error": {"code": "agent_not_found", "message": "missing"},
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(1)
    response(
        "agent_info",
        agent={
            "name": args[2],
            "agent": "codex",
            "cwd": os.getcwd(),
            "status": "working",
        },
    )
else:
    print("unsupported fake Herdr operation", file=sys.stderr)
    raise SystemExit(1)
