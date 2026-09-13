#!/usr/bin/env python3
"""Stateful deterministic Herdr fixture; starts no processes or models."""

import json
import os
from pathlib import Path
import subprocess
import sys

args = sys.argv[1:]
methods = {
    "agent.get": "AgentTarget",
    "agent.prompt": "AgentPromptParams",
    "agent.start": "AgentStartParams",
    "worktree.open": "WorktreeOpenParams",
    "worktree.remove": "WorktreeRemoveParams",
    "workspace.get": "WorkspaceTarget",
    "workspace.close": "WorkspaceCloseParams",
    "session.snapshot": "EmptyParams",
}
fields = {
    "AgentTarget": ["target"],
    "AgentPromptParams": ["target", "text"],
    "AgentStartParams": ["name", "kind", "pane_id", "args"],
    "WorktreeOpenParams": ["cwd", "path", "label", "focus"],
    "WorktreeRemoveParams": ["workspace_id", "force"],
    "WorkspaceTarget": ["workspace_id"],
    "WorkspaceCloseParams": ["workspace_id"],
    "EmptyParams": [],
}
results = [
    "agent_info",
    "agent_prompted",
    "agent_started",
    "session_snapshot",
    "worktree_opened",
    "worktree_removed",
    "workspace_info",
    "ok",
]
missing = os.environ.get("FAKE_HERDR_SCHEMA_MISSING")
model_path = os.environ.get("FAKE_HERDR_MODEL")
model = (
    json.loads(Path(model_path).read_text())
    if model_path and Path(model_path).exists()
    else {
        "workspaces": [],
        "tabs": [],
        "panes": [],
        "agents": [],
        "calls": [],
        "next_id": 1,
        "settings": {},
    }
)
settings = model["settings"]
model["calls"].append(args)


def save():
    if model_path:
        Path(model_path).write_text(json.dumps(model))


def output(value):
    save()
    print(json.dumps(value))
    raise SystemExit(0)


def response(kind, **values):
    output({"id": "fixture", "result": {"type": kind, **values}})


def failure(code, message):
    save()
    print(
        json.dumps(
            {
                "id": "fixture",
                "error": {
                    "code": code,
                    "message": message,
                },
            }
        ),
        file=sys.stderr,
    )
    raise SystemExit(1)


def option(name):
    return args[args.index(name) + 1]


def workspace(ident):
    return next(w for w in model["workspaces"] if w["workspace_id"] == ident)


def agent(name):
    if os.environ.get("FAKE_HERDR_MISSING_AGENT"):
        return None
    if name == "implementer":
        return {
            "name": name,
            "agent": "codex",
            "cwd": os.getcwd(),
            "agent_status": "working",
            "pane_id": "implementer-pane",
            "terminal_id": "implementer-terminal",
        }
    return next((a for a in model["agents"] if a["name"] == name), None)


def forget(ident):
    for key in ("workspaces", "tabs", "panes", "agents"):
        model[key] = [v for v in model[key] if v["workspace_id"] != ident]


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
        "start prompt get open remove close snapshot --kind [possible values:"
        " codex, claude] --pane --cwd --path --label --no-focus --workspace"
        " --force workspace_id"
    )
elif args == ["api", "snapshot"]:
    if settings.get("unreachable"):
        failure("unreachable", "fixture Herdr is unreachable")
    response(
        "session_snapshot",
        snapshot={
            "protocol": int(os.environ.get("FAKE_HERDR_PROTOCOL", "20")),
            "version": "fixture",
            "layouts": [],
            **{k: model[k] for k in ("workspaces", "tabs", "panes", "agents")},
        },
    )
elif args == ["integration", "status"]:
    print("codex: " + os.environ.get("FAKE_HERDR_INTEGRATION", "current"))
    print("claude: current")
elif args[:2] == ["agent", "get"]:
    if settings.get("unreachable"):
        failure("unreachable", "fixture Herdr is unreachable")
    value = agent(args[2])
    if value is None:
        failure("agent_not_found", "missing")
    response("agent_info", agent=value)
elif args[:2] == ["worktree", "open"]:
    if settings.get("open_failure"):
        failure("open_failed", "fixture open failed")
    path, primary, name = option("--path"), option("--cwd"), option("--label")
    found = next(
        (
            w
            for w in model["workspaces"]
            if w.get("worktree", {}).get("checkout_path") == path
        ),
        None,
    )
    already = found is not None
    if found is None:
        ident = f'w{model["next_id"]}'
        model["next_id"] += 1
        pane_id, tab_id = ident + ":p1", ident + ":t1"
        found = {
            "workspace_id": ident,
            "label": name,
            "tab_count": 1,
            "pane_count": 1,
            "active_tab_id": tab_id,
            "worktree": {
                "checkout_path": path,
                "repo_root": primary,
                "is_linked_worktree": True,
            },
        }
        model["workspaces"].append(found)
        model["tabs"].append({"workspace_id": ident, "tab_id": tab_id})
        model["panes"].append(
            {
                "workspace_id": ident,
                "tab_id": tab_id,
                "pane_id": pane_id,
                "terminal_id": ident + "-terminal",
                "cwd": path,
                "agent": None,
                "agent_status": "unknown",
            }
        )
    pane = next(
        p for p in model["panes"] if p["workspace_id"] == found["workspace_id"]
    )
    response(
        "worktree_opened",
        workspace=found,
        root_pane=pane,
        tab=next(t for t in model["tabs"] if t["tab_id"] == pane["tab_id"]),
        worktree={"path": settings.get("opened_path", path)},
        already_open=already,
    )
elif args[:2] == ["agent", "start"]:
    if settings.get("start_busy", 0) > 0:
        settings["start_busy"] -= 1
        failure("agent_pane_busy", "fixture shell is still starting")
    pane = next(p for p in model["panes"] if p["pane_id"] == option("--pane"))
    value = {
        **pane,
        "name": args[2],
        "agent": option("--kind"),
        "agent_status": settings.get("start_state", "idle"),
    }
    if settings.get("start_not_ready"):
        value["agent_status"] = "blocked"
    pane.update(agent=value["agent"], agent_status=value["agent_status"])
    model["agents"].append(value)
    if settings.get("start_extra_pane"):
        w = workspace(pane["workspace_id"])
        w["pane_count"] += 1
        model["panes"].append({**pane, "pane_id": pane["pane_id"] + "-extra"})
    if settings.get("start_not_ready"):
        failure("agent_not_ready", "fixture startup needs a human")
    response("agent_started", agent=value, argv=args)
elif args[:2] == ["agent", "prompt"]:
    if settings.get("prompt_failure"):
        failure("prompt_failed", "fixture prompt failed")
    value = agent(args[2])
    if value is None or settings.get("prompt_agent_not_found"):
        failure("agent_not_found", "missing")
    value["agent_status"] = settings.get("prompt_state", "working")
    response("agent_prompted", agent=value)
elif args[:2] == ["workspace", "get"]:
    value = workspace(args[2])
    response("workspace_info", workspace=value)
elif args[:2] == ["workspace", "close"]:
    if settings.get("close_failure"):
        failure("close_failed", "fixture close failed")
    forget(args[2])
    response("ok")
elif args[:2] == ["worktree", "remove"]:
    if settings.get("remove_failure"):
        failure("remove_failed", "fixture remove failed")
    ident = option("--workspace")
    value = workspace(ident)
    registration = value.get("worktree")
    if registration is None:
        failure("not_a_worktree", "workspace no longer registers a worktree")
    path = registration["checkout_path"]
    removed = subprocess.run(
        ["git", "worktree", "remove", "--force", path],
        cwd=registration["repo_root"],
        text=True,
        capture_output=True,
        shell=False,
        timeout=30,
    )
    if removed.returncode:
        failure("git_error", removed.stderr)
    forget(ident)
    response("worktree_removed", path=path, workspace_id=ident, forced=True)
else:
    failure("unsupported", "unsupported fake Herdr operation")
save()
