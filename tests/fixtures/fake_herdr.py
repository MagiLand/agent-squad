#!/usr/bin/env python3
"""Stateful deterministic Herdr fixture; starts no harnesses or models.

The top level of the model file is the session named ``fixture``, which is
also the default session. ``sessions`` maps further session names to states of
the same shape. Each session has its own resources, settings and recorded
calls. Like Herdr, the fixture chooses the session from HERDR_SOCKET_PATH,
then HERDR_SESSION, then the default session.
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
PRIMARY = "fixture"


def blank():
    return {
        "workspaces": [],
        "tabs": [],
        "panes": [],
        "agents": [],
        "calls": [],
        "next_id": 1,
        "settings": {},
    }


if model_path and os.path.exists(model_path):
    with open(model_path) as stream:
        root = json.load(stream)
else:
    root = blank()
states = {PRIMARY: root, **root.setdefault("sessions", {})}
for state in states.values():
    for key, value in blank().items():
        state.setdefault(key, value)


def socket_for(name):
    directory = os.path.dirname(model_path) if model_path else os.getcwd()
    return os.path.join(directory, f"herdr-{name}.sock")


def running(name):
    return states[name]["settings"].get("running", True)


socket_path = os.environ.get("HERDR_SOCKET_PATH")
if root["settings"].get("ignore_socket_path"):
    # Deliberately off contract: a Herdr that does not honour the variable.
    socket_path = None
if socket_path:
    target = next((n for n in states if socket_for(n) == socket_path), None)
elif os.environ.get("HERDR_SESSION"):
    target = os.environ["HERDR_SESSION"]
else:
    target = PRIMARY
# None when the addressed session does not exist or is not running.
model = states[target] if target in states and running(target) else None
settings = (model or root)["settings"]
if model is not None:
    model["calls"].append(args)
elif target in states:
    states[target]["calls"].append(args)
else:
    root.setdefault("stray", []).append(args)


def save():
    if model_path:
        with open(model_path, "w") as stream:
            stream.write(json.dumps(root))


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
    # ``implementer`` places the Implementer: false removes it, an object
    # sets its name, kind and cwd. Only the default session has one unless a
    # further session configures its own.
    spec = settings.get("implementer", {} if model is root else False)
    if isinstance(spec, dict) and name == spec.get("name", "implementer"):
        return {
            "name": name,
            "agent": spec.get(
                "kind", settings.get("implementer_kind", "codex")
            ),
            "cwd": spec.get("cwd", os.getcwd()),
            "agent_status": "working",
            "pane_id": "implementer-pane",
            "terminal_id": "implementer-terminal",
        }
    return next((a for a in model["agents"] if a["name"] == name), None)


def forget(ident):
    for key in ("workspaces", "tabs", "panes", "agents"):
        model[key] = [v for v in model[key] if v["workspace_id"] != ident]


socket_command = "--help" not in args and (
    args == ["api", "snapshot"]
    or args[:1] in (["agent"], ["worktree"], ["workspace"])
)
if args == ["--version"]:
    print("herdr fixture-discovery")
elif args == ["session", "list", "--json"]:
    if root["settings"].get("session_list_failure"):
        failure("unsupported", "fixture has no session listing")
    output(
        root["settings"].get(
            "session_list",
            {
                "sessions": [
                    {
                        "name": name,
                        "default": name == PRIMARY,
                        "running": running(name),
                        "socket_path": socket_for(name),
                    }
                    for name in states
                ]
            },
        )
    )
elif socket_command and model is None:
    failure(
        "server_not_running",
        f"no herdr server is running at {socket_path or target}",
    )
elif args == ["api", "schema", "--json"]:
    output(
        {
            "schema_version": 1,
            "protocol": 20,
            "schemas": {
                "request": {
                    "$defs": {
                        name: (
                            {"properties": {f: {} for f in names}}
                            if names
                            else {"type": "object"}
                        )
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
    if settings.get("unreachable") or settings.get("snapshot_failure"):
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
    print("claude: " + os.environ.get(
        "FAKE_HERDR_CLAUDE_INTEGRATION", "current"))
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
    if settings.get("start_failure"):
        failure("agent_start_failed", "fixture agent exited during startup")
    state = settings.get("start_state", "idle")
    if any(a.startswith("$squad-reviewer ") for a in args):
        state = settings.get("initial_request_state", state)
    value = {
        **pane,
        "name": args[2],
        "agent": option("--kind"),
        "agent_status": state,
        "launch_pending": True,
    }
    if settings.get("start_not_ready"):
        value["agent_status"] = "blocked"
    pane.update(agent=value["agent"], agent_status=value["agent_status"])
    model["agents"].append(value)
    if settings.get("start_extra_pane"):
        w = workspace(pane["workspace_id"])
        w["pane_count"] += 1
        model["panes"].append({**pane, "pane_id": pane["pane_id"] + "-extra"})
    if settings.get("start_blocked_success"):
        # Deliberately off contract: exercise launch's defensive guard even
        # though real Herdr does not report a successful blocked start.
        value.update(agent_status="blocked", launch_pending=False)
        pane.update(agent_status="blocked")
        response("agent_started", agent=value, argv=args)
    if value["agent_status"] == "blocked":
        failure("agent_not_ready", "fixture startup needs a human")
    if value["agent_status"] != "idle":
        # The selected state persists through the startup deadline. Model the
        # timeout immediately, without a wall-clock wait or a real agent.
        value.update(name=None, launch_pending=False)
        failure("timeout", "timed out waiting for agent startup")
    value["launch_pending"] = False
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
    import subprocess

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
