"""Installed Herdr discovery and deterministic review-request delivery."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess

from .initialization import AgentKind, AgentSquadError
from .storage import InvalidJsonError, decode_json


class HerdrError(AgentSquadError):
    """Raised when installed Herdr capabilities or delivery are unusable."""


def format_herdr_error(value: str) -> str:
    """Flatten a Herdr failure for stable one-line status output."""

    return " ".join(value.splitlines()).strip() or "unknown Herdr error"


@dataclass(frozen=True)
class HerdrInstallation:
    """Validated facts discovered from the installed Herdr command."""

    executable: Path
    version: str
    protocol: int


@dataclass(frozen=True)
class ReviewerSession:
    """The deterministic Herdr resources used for one Reviewer."""

    workspace_id: str
    pane_id: str
    adopted: bool


class HerdrClient:
    """Small adapter for the exact Herdr surface Agent Squad needs."""

    _REQUIRED_METHODS = {
        "agent.get": "AgentTarget",
        "agent.prompt": "AgentPromptParams",
        "agent.start": "AgentStartParams",
        "worktree.open": "WorktreeOpenParams",
    }
    _REQUIRED_RESULTS = {
        "agent_info",
        "agent_prompted",
        "agent_started",
        "session_snapshot",
        "worktree_opened",
    }
    _REQUIRED_PARAMETER_FIELDS = {
        "AgentTarget": {"target"},
        "AgentPromptParams": {"target", "text"},
        "AgentStartParams": {"name", "kind", "pane_id", "args"},
        "WorktreeOpenParams": {"path", "label", "focus"},
    }
    _HELP_CHECKS = (
        (("agent", "--help"), ("start", "prompt", "get")),
        (("agent", "start", "--help"), ("--kind", "--pane")),
        (("agent", "prompt", "--help"), ()),
        (("agent", "get", "--help"), ()),
        (("worktree", "--help"), ("open",)),
        (
            ("worktree", "open", "--help"),
            ("--path", "--label", "--no-focus"),
        ),
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

    def discover(self, agent_kind: AgentKind) -> HerdrInstallation:
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
            None
            if role_line is None
            else role_line.split(":", 1)[1].strip()
        )
        if role_status is None or not role_status.startswith("current"):
            raise HerdrError(
                f"Herdr integration for agent kind "
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
                    f"Herdr schema lacks parameter definition "
                    f"{definition_name}"
                )
            properties = definition.get("properties")
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

    def dispatch_review_request(
        self,
        *,
        reviewer_name: str,
        reviewer_kind: AgentKind,
        start_args: tuple[str, ...],
        review_worktree: Path,
        prompt: str,
    ) -> ReviewerSession:
        """Adopt or launch the deterministic Reviewer and send its prompt."""

        existing = self._get_agent(reviewer_name)
        if existing is not None:
            self._validate_agent(
                existing,
                reviewer_name=reviewer_name,
                reviewer_kind=reviewer_kind,
                review_worktree=review_worktree,
            )
            workspace_id = _required_text(
                existing.get("workspace_id"),
                "existing Reviewer workspace_id",
            )
            pane_id = _required_text(
                existing.get("pane_id"),
                "existing Reviewer pane_id",
            )
            adopted = True
        else:
            opened = self._response_result(
                self._run(
                    (
                        "worktree",
                        "open",
                        "--path",
                        str(review_worktree),
                        "--label",
                        reviewer_name,
                        "--no-focus",
                    )
                ),
                expected_type="worktree_opened",
            )
            worktree_value = opened.get("worktree")
            pane_value = opened.get("root_pane")
            workspace_value = opened.get("workspace")
            if not isinstance(worktree_value, dict):
                raise HerdrError(
                    "Herdr worktree-open response has no worktree object"
                )
            if not isinstance(pane_value, dict):
                raise HerdrError(
                    "Herdr worktree-open response has no root pane"
                )
            if not isinstance(workspace_value, dict):
                raise HerdrError(
                    "Herdr worktree-open response has no workspace"
                )
            opened_path = _required_path(
                worktree_value.get("path"),
                "opened Herdr worktree path",
            )
            if opened_path != review_worktree.resolve(strict=True):
                raise HerdrError(
                    "Herdr opened a different worktree than the requested "
                    f"round: {opened_path}"
                )
            pane_id = _required_text(
                pane_value.get("pane_id"),
                "opened Herdr pane_id",
            )
            workspace_id = _required_text(
                workspace_value.get("workspace_id"),
                "opened Herdr workspace_id",
            )
            start_command = [
                "agent",
                "start",
                reviewer_name,
                "--kind",
                reviewer_kind.value,
                "--pane",
                pane_id,
            ]
            if start_args:
                start_command.append("--")
                start_command.extend(start_args)
            started = self._response_result(
                self._run(tuple(start_command)),
                expected_type="agent_started",
            )
            agent_value = started.get("agent")
            if not isinstance(agent_value, dict):
                raise HerdrError(
                    "Herdr agent-start response has no agent object"
                )
            self._validate_agent(
                agent_value,
                reviewer_name=reviewer_name,
                reviewer_kind=reviewer_kind,
                review_worktree=review_worktree,
            )
            adopted = False

        prompted = self._response_result(
            self._run(("agent", "prompt", reviewer_name, prompt)),
            expected_type="agent_prompted",
        )
        prompted_agent = prompted.get("agent")
        if not isinstance(prompted_agent, dict):
            raise HerdrError("Herdr prompt response has no agent object")
        self._validate_agent(
            prompted_agent,
            reviewer_name=reviewer_name,
            reviewer_kind=reviewer_kind,
            review_worktree=review_worktree,
        )
        return ReviewerSession(
            workspace_id=workspace_id,
            pane_id=pane_id,
            adopted=adopted,
        )

    def dispatch_review_result(
        self,
        *,
        implementer_name: str,
        implementer_kind: AgentKind,
        prompt: str,
    ) -> None:
        """Send one marker-confirmed result prompt to the Implementer."""

        implementer = self._get_agent(
            implementer_name,
            role="Implementer",
        )
        if implementer is None:
            raise HerdrError(
                f"Implementer {implementer_name!r} is not available"
            )
        self._validate_agent_identity(
            implementer,
            expected_name=implementer_name,
            expected_kind=implementer_kind,
            role="Implementer",
        )
        prompted = self._response_result(
            self._run(("agent", "prompt", implementer_name, prompt)),
            expected_type="agent_prompted",
        )
        prompted_agent = prompted.get("agent")
        if not isinstance(prompted_agent, dict):
            raise HerdrError("Herdr prompt response has no agent object")
        self._validate_agent_identity(
            prompted_agent,
            expected_name=implementer_name,
            expected_kind=implementer_kind,
            role="Implementer",
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

    def _validate_agent(
        self,
        value: dict[str, object],
        *,
        reviewer_name: str,
        reviewer_kind: AgentKind,
        review_worktree: Path,
    ) -> None:
        self._validate_agent_identity(
            value,
            expected_name=reviewer_name,
            expected_kind=reviewer_kind,
            role="Reviewer",
        )
        cwd = _required_path(value.get("cwd"), "Reviewer cwd")
        if cwd != review_worktree.resolve(strict=True):
            raise HerdrError(
                f"Reviewer {reviewer_name!r} is in {cwd}, expected "
                f"{review_worktree}"
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
            raise HerdrError(f"{command} failed: {detail}")
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
        except InvalidJsonError as error:
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
