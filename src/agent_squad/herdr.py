"""Installed Herdr discovery, resource operations, and fixed handoff lines."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import subprocess

from .initialization import AgentKind, AgentSquadError, decode_json
from .conventions import SHA, TAG, render_line


class HerdrError(AgentSquadError):
    """Raised when installed Herdr capabilities or delivery are unusable."""


class HerdrCommandError(HerdrError):
    """A failed command with Herdr's machine-readable error code preserved."""

    def __init__(self, message: str, code: str | None) -> None:
        super().__init__(message)
        self.code = code


# Selected by the live delivery experiment, not by consumer configuration.
REVIEW_DELIVERY = {
    AgentKind.CLAUDE: "agent_prompt",
    AgentKind.CODEX: "agent_prompt",
}
AGENT_NAME = r"[a-z][a-z0-9_-]{0,31}"
AGENT_STATES = {"idle", "working", "blocked", "done", "unknown"}


def reviewer_name(pr: int, head: str) -> str:
    """Return the reserved name after validating the complete target."""
    if type(pr) is not int or pr < 1 or re.fullmatch(SHA, head) is None:
        raise AgentSquadError(
            "review target requires a PR number and full SHA"
        )
    name = f"reviewer-pr{pr}-{head[:7]}"
    if re.fullmatch(AGENT_NAME, name) is None:
        raise AgentSquadError("PR number exceeds the Herdr agent-name limit")
    return name


def request_line(
    kind: AgentKind, pr: int, head: str, base: str, implementer: str
) -> str:
    """Render the exact harness-specific request of §8.3."""
    reviewer_name(pr, head)
    if (
        re.fullmatch(SHA, base) is None
        or re.fullmatch(AGENT_NAME, implementer) is None
    ):
        raise AgentSquadError("invalid review base or Implementer agent name")
    prefix = {AgentKind.CLAUDE: "/", AgentKind.CODEX: "$"}[kind]
    return (
        f"{prefix}squad-reviewer pr={pr} head={head} base={base}"
        f" implementer={implementer}"
    )


def result_message(pr: int, head: str, verdict: str) -> str:
    """Render the fixed result line and per-verdict instruction of §8.5."""
    reviewer_name(pr, head)
    render_line("review", pr=pr, head=head, base=head, verdict=verdict)
    sentences = {
        "approved": (
            f"Run agent-squad status --pr {pr}, report the approval to the"
            " Developer, and do not merge without the Developer's instruction."
        ),
        "changes_requested": (
            f"Run agent-squad status --pr {pr}, evaluate every blocking thread"
            " on the PR, and record dispositions before requesting another"
            " review."
        ),
        "needs_human": (
            f"Run agent-squad status --pr {pr} and relay the decision required"
            " to the Developer."
        ),
    }
    return (
        f"{TAG} REVIEW_RESULT pr={pr} head={head} verdict={verdict}\n"
        + sentences[verdict]
    )


def stopped_message(pr: int, head: str, reason: str) -> str:
    """Render the fixed stop line and instruction of §8.5."""
    reviewer_name(pr, head)
    render_line("stop", head=head, reason=reason)
    return (
        f"{TAG} STOPPED pr={pr} head={head} reason={reason}\n"
        f"Automated review has stopped; run agent-squad status --pr {pr} and"
        " relay the reason and the remaining problems to the Developer."
    )


def format_herdr_error(value: str) -> str:
    """Flatten a Herdr failure for stable one-line status output."""

    return " ".join(value.splitlines()).strip() or "unknown Herdr error"


@dataclass(frozen=True)
class HerdrInstallation:
    """Validated facts discovered from the installed Herdr command."""

    executable: Path
    version: str
    protocol: int


class HerdrClient:
    """Small adapter for the exact Herdr surface Agent Squad needs."""

    _REQUIRED_METHODS = {
        "agent.get": "AgentTarget",
        "agent.prompt": "AgentPromptParams",
        "agent.start": "AgentStartParams",
        "worktree.open": "WorktreeOpenParams",
        "worktree.remove": "WorktreeRemoveParams",
        "workspace.get": "WorkspaceTarget",
        "workspace.close": "WorkspaceCloseParams",
        "session.snapshot": "EmptyParams",
    }
    _REQUIRED_RESULTS = {
        "agent_info",
        "agent_prompted",
        "agent_started",
        "session_snapshot",
        "worktree_opened",
        "worktree_removed",
        "workspace_info",
        "ok",
    }
    _REQUIRED_PARAMETER_FIELDS = {
        "AgentTarget": {"target"},
        "AgentPromptParams": {"target", "text"},
        "AgentStartParams": {"name", "kind", "pane_id", "args"},
        "WorktreeOpenParams": {"cwd", "path", "label", "focus"},
        "WorktreeRemoveParams": {"workspace_id", "force"},
        "WorkspaceTarget": {"workspace_id"},
        "WorkspaceCloseParams": {"workspace_id"},
        "EmptyParams": set(),
    }
    _HELP_CHECKS = (
        (("agent", "--help"), ("start", "prompt", "get")),
        (("agent", "start", "--help"), ("--kind", "--pane")),
        (("agent", "prompt", "--help"), ()),
        (("agent", "get", "--help"), ()),
        (("worktree", "--help"), ("open", "remove")),
        (
            ("worktree", "open", "--help"),
            ("--cwd", "--path", "--label", "--no-focus"),
        ),
        (("worktree", "remove", "--help"), ("--workspace", "--force")),
        (("workspace", "--help"), ("get", "close")),
        (("workspace", "get", "--help"), ("workspace_id",)),
        (("workspace", "close", "--help"), ("workspace_id",)),
        (("api", "snapshot", "--help"), ()),
    )

    def __init__(
        self,
        working_directory: Path,
        *,
        executable: str | Path | None = None,
        timeout_seconds: float = 45.0,
    ) -> None:
        self._working_directory = working_directory
        self._configured_executable = executable
        self._timeout_seconds = timeout_seconds
        self._executable: Path | None = None

    def discover(
        self,
        agent_kind: AgentKind,
        *,
        role: str,
    ) -> HerdrInstallation:
        """Validate schema, live protocol, commands, and agent integration."""

        executable = self._resolve_executable()
        version_result = self._run(("--version",))
        version = version_result.stdout.strip()
        if not version:
            raise HerdrError("herdr --version returned no version text")

        schema_result = self._run(("api", "schema", "--json"))
        schema = self._decode_object(
            schema_result.stdout,
            "Herdr API schema",
        )
        schema_version = schema.get("schema_version")
        protocol = schema.get("protocol")
        if type(schema_version) is not int or schema_version < 1:
            raise HerdrError(
                "Herdr API schema has no supported schema_version"
            )
        if type(protocol) is not int or protocol < 1:
            raise HerdrError("Herdr API schema has no valid protocol number")
        self._validate_schema_contract(schema)

        for arguments, expected_fragments in self._HELP_CHECKS:
            output = self._run(arguments).stdout
            if arguments == ("agent", "start", "--help"):
                kinds = re.search(
                    r"--kind\b.*?\[possible values:\s*([^]]+)\]",
                    output,
                    re.DOTALL,
                )
                supported = (
                    {value.strip() for value in kinds.group(1).split(",")}
                    if kinds
                    else set()
                )
                if agent_kind.value not in supported:
                    raise HerdrError(
                        f"installed Herdr does not advertise {role} kind "
                        f"{agent_kind.value!r} in agent start help"
                    )
            absent = [
                fragment
                for fragment in expected_fragments
                if fragment not in output
            ]
            if absent:
                command = " ".join(("herdr", *arguments))
                raise HerdrError(
                    f"{command} does not expose required syntax: "
                    f"{', '.join(absent)}"
                )

        snapshot = self._response_result(
            self._run(("api", "snapshot")),
            expected_type="session_snapshot",
        )
        snapshot_value = snapshot.get("snapshot")
        if not isinstance(snapshot_value, dict):
            raise HerdrError("Herdr session snapshot has no snapshot object")
        live_protocol = snapshot_value.get("protocol")
        if live_protocol != protocol:
            raise HerdrError(
                "installed Herdr schema protocol does not match the live "
                f"session: schema {protocol}, live {live_protocol!r}"
            )
        if not isinstance(snapshot_value.get("version"), str):
            raise HerdrError("Herdr session snapshot has no version string")

        integration = self._run(("integration", "status")).stdout
        role_line = next(
            (
                line
                for line in integration.splitlines()
                if line.startswith(f"{agent_kind.value}:")
            ),
            None,
        )
        role_status = (
            None if role_line is None else role_line.split(":", 1)[1].strip()
        )
        if role_status is None or not role_status.startswith("current"):
            raise HerdrError(
                f"Herdr integration for {role} kind "
                f"{agent_kind.value!r} is not current"
            )

        return HerdrInstallation(
            executable=executable,
            version=version,
            protocol=protocol,
        )

    def _validate_schema_contract(self, schema: dict[str, object]) -> None:
        schemas = schema.get("schemas")
        if not isinstance(schemas, dict):
            raise HerdrError("Herdr API schema has no schemas object")
        request = schemas.get("request")
        success = schemas.get("success_response")
        if not isinstance(request, dict) or not isinstance(success, dict):
            raise HerdrError(
                "Herdr API schema lacks request or success-response contracts"
            )
        request_definitions = request.get("$defs")
        variants = request.get("oneOf")
        if not isinstance(request_definitions, dict) or not isinstance(
            variants,
            list,
        ):
            raise HerdrError("Herdr request schema has an invalid shape")

        methods: dict[str, str] = {}
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            properties = variant.get("properties")
            if not isinstance(properties, dict):
                continue
            method_value = properties.get("method")
            params_value = properties.get("params")
            if not isinstance(method_value, dict) or not isinstance(
                params_value,
                dict,
            ):
                continue
            method = method_value.get("const")
            reference = params_value.get("$ref")
            if isinstance(method, str) and isinstance(reference, str):
                methods[method] = reference.rsplit("/", 1)[-1]
        for method, definition_name in self._REQUIRED_METHODS.items():
            if methods.get(method) != definition_name:
                raise HerdrError(
                    "installed Herdr schema is missing required method "
                    f"contract {method} -> {definition_name}"
                )
            definition = request_definitions.get(definition_name)
            if not isinstance(definition, dict):
                raise HerdrError(
                    "Herdr schema lacks parameter definition "
                    f"{definition_name}"
                )
            properties = definition.get("properties", {})
            if not isinstance(properties, dict):
                raise HerdrError(
                    f"Herdr parameter definition {definition_name} has no "
                    "properties"
                )
            missing_fields = sorted(
                self._REQUIRED_PARAMETER_FIELDS[definition_name]
                - properties.keys()
            )
            if missing_fields:
                raise HerdrError(
                    f"Herdr parameter definition {definition_name} lacks: "
                    f"{', '.join(missing_fields)}"
                )

        success_definitions = success.get("$defs")
        if not isinstance(success_definitions, dict):
            raise HerdrError(
                "Herdr success-response schema has no definitions"
            )
        response_result = success_definitions.get("ResponseResult")
        if not isinstance(response_result, dict) or not isinstance(
            response_result.get("oneOf"),
            list,
        ):
            raise HerdrError(
                "Herdr response-result schema has an invalid shape"
            )
        result_types: set[str] = set()
        for variant in response_result["oneOf"]:
            if not isinstance(variant, dict):
                continue
            properties = variant.get("properties")
            if not isinstance(properties, dict):
                continue
            type_value = properties.get("type")
            if isinstance(type_value, dict) and isinstance(
                type_value.get("const"),
                str,
            ):
                result_types.add(type_value["const"])
        missing_results = sorted(self._REQUIRED_RESULTS - result_types)
        if missing_results:
            raise HerdrError(
                "installed Herdr schema is missing required response "
                f"contracts: {', '.join(missing_results)}"
            )

    def inspect_agent(
        self,
        name: str,
        kind: AgentKind,
        *,
        role: str,
        worktree: Path | None = None,
    ) -> dict[str, object]:
        """Read and validate one configured agent without prompting it."""

        agent = self._get_agent(name, role=role)
        if agent is None:
            raise HerdrError(f"{role} {name!r} is not available")
        self._validate_agent_identity(
            agent,
            expected_name=name,
            expected_kind=kind,
            role=role,
        )
        if worktree is not None:
            cwd = _required_path(agent.get("cwd"), f"{role} cwd")
            if cwd != worktree.resolve(strict=True):
                raise HerdrError(
                    f"{role} {name!r} is in {cwd}, expected {worktree}"
                )
        return agent

    def snapshot(self) -> dict[str, object]:
        """Read the installed session's resource inventory."""

        result = self._response_result(
            self._run(("api", "snapshot")),
            expected_type="session_snapshot",
        )
        value = result.get("snapshot")
        if not isinstance(value, dict):
            raise HerdrError("Herdr snapshot has no snapshot object")
        return value

    def get_agent(self, name: str) -> dict[str, object] | None:
        """Read a named agent, distinguishing absence from server failure."""
        return self._get_agent(name)

    def open_worktree(self, primary: Path, path: Path, name: str) -> dict:
        """Open exactly the supplied checkout without changing UI focus."""
        return self._response_result(
            self._run(
                (
                    "worktree",
                    "open",
                    "--cwd",
                    str(primary),
                    "--path",
                    str(path),
                    "--label",
                    name,
                    "--no-focus",
                )
            ),
            expected_type="worktree_opened",
        )

    def start_agent(
        self, name: str, kind: AgentKind, pane: str, args: tuple[str, ...]
    ) -> dict:
        """Start an interactive agent; leave blocked sessions running."""
        arguments = (
            "agent",
            "start",
            name,
            "--kind",
            kind.value,
            "--pane",
            pane,
        )
        if args:
            arguments += ("--", *args)
        return self._response_result(
            self._run(arguments), expected_type="agent_started"
        )

    def prompt(self, name: str, message: str) -> None:
        """Send once, without waiting for the receiving agent's turn."""
        self._response_result(
            self._run(("agent", "prompt", name, message)),
            expected_type="agent_prompted",
        )

    def workspace(self, workspace_id: str) -> dict:
        result = self._response_result(
            self._run(("workspace", "get", workspace_id)),
            expected_type="workspace_info",
        )
        workspace = result.get("workspace")
        if not isinstance(workspace, dict):
            raise HerdrError("Herdr workspace-get has no workspace object")
        return workspace

    def remove_worktree(self, workspace_id: str) -> dict:
        return self._response_result(
            self._run(
                ("worktree", "remove", "--workspace", workspace_id, "--force")
            ),
            expected_type="worktree_removed",
        )

    def close_workspace(self, workspace_id: str) -> None:
        self._response_result(
            self._run(("workspace", "close", workspace_id)),
            expected_type="ok",
        )

    def _resolve_executable(self) -> Path:
        if self._executable is not None:
            return self._executable
        configured = self._configured_executable
        if configured is None:
            discovered = shutil.which("herdr")
            if discovered is None:
                raise HerdrError(
                    "Herdr is not installed or is not available on PATH"
                )
            candidate = Path(discovered)
        else:
            candidate = Path(configured)
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise HerdrError(
                f"cannot resolve the Herdr executable {candidate}: {error}"
            ) from error
        if not resolved.is_file():
            raise HerdrError(
                f"Herdr executable is not a regular file: {resolved}"
            )
        self._executable = resolved
        return resolved

    def _get_agent(
        self,
        agent_name: str,
        *,
        role: str = "Reviewer",
    ) -> dict[str, object] | None:
        result = self._run(
            ("agent", "get", agent_name),
            allow_failure=True,
        )
        if result.returncode != 0:
            error = _decode_error_response(result.stderr or result.stdout)
            if error is not None and error[0] == "agent_not_found":
                return None
            detail = error[1] if error is not None else _process_detail(result)
            raise HerdrError(f"could not inspect {role} session: {detail}")
        response = self._response_result(
            result,
            expected_type="agent_info",
        )
        agent = response.get("agent")
        if not isinstance(agent, dict):
            raise HerdrError("Herdr agent-get response has no agent object")
        return agent

    def _validate_agent_identity(
        self,
        value: dict[str, object],
        *,
        expected_name: str,
        expected_kind: AgentKind,
        role: str,
    ) -> None:
        name = value.get("name")
        if name != expected_name:
            raise HerdrError(
                f"Herdr returned {role} name {name!r}, expected "
                f"{expected_name!r}"
            )
        kind = value.get("agent")
        if kind != expected_kind.value:
            raise HerdrError(
                f"{role} {expected_name!r} uses agent kind {kind!r}, "
                f"expected {expected_kind.value!r}"
            )

    def _run(
        self,
        arguments: tuple[str, ...],
        *,
        allow_failure: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        executable = self._resolve_executable()
        try:
            result = subprocess.run(
                [str(executable), *arguments],
                cwd=self._working_directory,
                check=False,
                shell=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self._timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            command = " ".join(("herdr", *arguments))
            raise HerdrError(f"{command} timed out") from error
        except OSError as error:
            raise HerdrError(f"could not run Herdr: {error}") from error
        if result.returncode != 0 and not allow_failure:
            error = _decode_error_response(result.stderr or result.stdout)
            detail = error[1] if error is not None else _process_detail(result)
            command = " ".join(("herdr", *arguments[:2]))
            raise HerdrCommandError(
                f"{command} failed"
                + (f" ({error[0]})" if error is not None else "")
                + f": {detail}",
                error[0] if error is not None else None,
            )
        return result

    def _response_result(
        self,
        process: subprocess.CompletedProcess[str],
        *,
        expected_type: str,
    ) -> dict[str, object]:
        response = self._decode_object(process.stdout, "Herdr response")
        if not isinstance(response.get("id"), str):
            raise HerdrError("Herdr response has no string id")
        result = response.get("result")
        if not isinstance(result, dict):
            raise HerdrError("Herdr response has no result object")
        if result.get("type") != expected_type:
            raise HerdrError(
                "Herdr response type is "
                f"{result.get('type')!r}, expected {expected_type!r}"
            )
        return result

    @staticmethod
    def _decode_object(content: str, label: str) -> dict[str, object]:
        try:
            value = decode_json(content)
        except ValueError as error:
            raise HerdrError(f"{label} is not valid JSON: {error}") from error
        if not isinstance(value, dict):
            raise HerdrError(f"{label} must be a JSON object")
        return value


def _decode_error_response(content: str) -> tuple[str, str] | None:
    try:
        value = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("error"), dict):
        return None
    error = value["error"]
    code = error.get("code")
    message = error.get("message")
    if not isinstance(code, str) or not isinstance(message, str):
        return None
    return code, message


def _process_detail(process: subprocess.CompletedProcess[str]) -> str:
    return (process.stderr or process.stdout).strip() or (
        f"exit status {process.returncode}"
    )


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise HerdrError(f"{label} must be a non-empty string")
    return value


def _required_path(value: object, label: str) -> Path:
    text = _required_text(value, label)
    path = Path(text)
    if not path.is_absolute():
        raise HerdrError(f"{label} must be an absolute path")
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise HerdrError(f"cannot resolve {label} {path}: {error}") from error
