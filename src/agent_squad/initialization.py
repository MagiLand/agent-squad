"""Git discovery and schema 2 configuration;

no review state is stored locally.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import StrEnum
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit

from .validation import JsonValidator

CONTROL_DIRECTORY_NAME = ".agent-squad"
REVIEW_DIRECTORY_NAME = ".agent-squad-review"
LOCAL_EXCLUDE_PATTERNS = (".agent-squad/", ".agent-squad-review/")


class AgentSquadError(Exception):
    """An actionable command failure."""


class ConfigurationError(AgentSquadError):
    """An invalid configuration."""


class GateError(AgentSquadError):
    """An action refused by the review protocol (exit 4)."""


class RetainedError(AgentSquadError):
    """Resources retained, or a published Task mirror to repair (exit 3)."""


class AgentKind(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"


V = JsonValidator(ConfigurationError)


def decode_json(content: str) -> object:
    """Read strict JSON, including duplicate-key and non-finite rejection."""

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    return json.loads(
        content, object_pairs_hook=pairs, parse_constant=constant
    )


@dataclass(frozen=True)
class ForgeConfiguration:
    kind: str
    owner: str
    repo: str
    base_url: str | None = None


@dataclass(frozen=True)
class ImplementerConfiguration:
    agent_name: str
    kind: AgentKind
    forge_account: str
    token_file: str | None = None


@dataclass(frozen=True)
class ReviewerConfiguration:
    kind: AgentKind
    start_args: tuple[str, ...]
    forge_account: str
    token_file: str | None = None


@dataclass(frozen=True)
class Configuration:
    schema_version: int
    forge: ForgeConfiguration
    implementer: ImplementerConfiguration
    reviewer: ReviewerConfiguration
    developer_accounts: tuple[str, ...]
    base_branch: str
    max_review_passes: int
    merge_method: str
    worktree_root: str
    scratch_root: str

    @classmethod
    def from_dict(cls, value: object) -> Configuration:
        data = V.require_object(value, "configuration")
        if data.get("schema_version") == 1:
            raise ConfigurationError(
                "schema 1 is unsupported; move config.json aside and rerun "
                "agent-squad init with both accounts"
            )
        V.check_fields(
            data,
            required={
                "schema_version",
                "forge",
                "implementer",
                "reviewer",
                "base_branch",
                "max_review_passes",
                "merge_method",
                "worktree_root",
                "scratch_root",
            },
            optional={
                "developer_accounts", "identity_mode", "approver_accounts",
            },
            path="configuration",
        )
        if V.require_int(data["schema_version"], "schema_version") != 2:
            raise ConfigurationError("schema_version must equal 2")
        forge = V.require_object(data["forge"], "forge")
        V.check_fields(
            forge, required={"kind", "owner", "repo"},
            optional={"base_url"}, path="forge",
        )
        if forge["kind"] not in ("github", "forgejo"):
            raise ConfigurationError("forge.kind must be github or forgejo")
        for key in ("owner", "repo"):
            text = V.require_string(forge[key], f"forge.{key}")
            if re.search(r"[/\s]", text):
                raise ConfigurationError(
                    f"forge.{key} cannot contain / or whitespace"
                )
        implementer = V.require_object(data["implementer"], "implementer")
        reviewer = V.require_object(data["reviewer"], "reviewer")
        V.check_fields(
            implementer,
            required={"agent_name", "kind", "forge_account"},
            optional={"token_file"},
            path="implementer",
        )
        V.check_fields(
            reviewer,
            required={"kind", "start_args", "forge_account"},
            optional={"token_file"},
            path="reviewer",
        )
        if forge["kind"] == "forgejo":
            forge = {
                **forge, "base_url": validate_base_url(forge.get("base_url")),
            }
            for label, role in (
                ("implementer", implementer), ("reviewer", reviewer),
            ):
                V.require_absolute_path(
                    role.get("token_file"), f"{label}.token_file",
                )
        else:
            for data_part, key, label in (
                (forge, "base_url", "forge.base_url"),
                (implementer, "token_file", "implementer.token_file"),
                (reviewer, "token_file", "reviewer.token_file"),
            ):
                if key in data_part:
                    raise ConfigurationError(
                        f"{label} is only allowed when forge.kind is forgejo"
                    )
        name = V.require_string(
            implementer["agent_name"], "implementer.agent_name"
        )
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) is None:
            raise ConfigurationError("implementer.agent_name is invalid")
        kinds = [
            V.require_enum(role["kind"], f"{label}.kind", AgentKind)
            for label, role in [
                ("implementer", implementer),
                ("reviewer", reviewer),
            ]
        ]
        accounts = [
            V.require_string(role["forge_account"], f"{label}.forge_account")
            for label, role in [
                ("implementer", implementer),
                ("reviewer", reviewer),
            ]
        ]
        # v0.6.x init wrote both keys into every configuration; only their
        # dual-mode values remain accepted.
        if (
            data.get("identity_mode", "dual") != "dual"
            or data.get("approver_accounts", []) != []
        ):
            raise ConfigurationError(
                "single-identity mode was removed in v0.7.0; move config.json"
                " aside and rerun agent-squad init with two accounts"
            )
        if accounts[0].casefold() == accounts[1].casefold():
            raise ConfigurationError(
                "Implementer and Reviewer accounts must differ"
            )
        args = string_list(reviewer["start_args"], "reviewer.start_args")
        developers = string_list(
            data.get("developer_accounts", []), "developer_accounts"
        )
        if any(
            login.casefold() == accounts[1].casefold() for login in developers
        ):
            raise ConfigurationError(
                "developer_accounts must exclude the Reviewer"
            )
        branch = V.require_string(data["base_branch"], "base_branch")
        if re.search(r"\s", branch) or ".." in branch:
            raise ConfigurationError(
                "base_branch cannot contain whitespace or .."
            )
        budget = V.require_int(data["max_review_passes"], "max_review_passes")
        if budget < 1:
            raise ConfigurationError("max_review_passes must be positive")
        method = V.require_string(data["merge_method"], "merge_method")
        if method not in ("merge", "squash"):
            raise ConfigurationError("merge_method must be merge or squash")
        roots = [
            V.require_string(data[key], key)
            for key in ("worktree_root", "scratch_root")
        ]
        return cls(
            2,
            ForgeConfiguration(**forge),
            ImplementerConfiguration(
                name, kinds[0], accounts[0], implementer.get("token_file"),
            ),
            ReviewerConfiguration(
                kinds[1], args, accounts[1], reviewer.get("token_file"),
            ),
            developers,
            branch,
            budget,
            method,
            *roots,
        )

    def to_dict(self) -> dict[str, object]:
        value = json.loads(json.dumps(asdict(self)))
        for section, field in (
            ("forge", "base_url"), ("implementer", "token_file"),
            ("reviewer", "token_file"),
        ):
            if value[section][field] is None:
                del value[section][field]
        return value

    def account(self, role: str) -> str:
        if role not in ("implementer", "reviewer"):
            raise ConfigurationError(f"unknown role: {role}")
        return getattr(self, role).forge_account


def validate_base_url(value: object) -> str:
    """Validate the explicitly configured API origin and instance prefix."""
    message = "invalid forge.base_url: "
    if not isinstance(value, str) or not value:
        raise ConfigurationError(message + "an absolute URL is required")
    try:
        parts = urlsplit(value)
        valid_port = parts.port is None or parts.port > 0
        host = parts.hostname
    except ValueError:
        raise ConfigurationError(message + "invalid host or port") from None
    if (
        not host or not parts.netloc or not valid_port
        or re.search(r"[\s\x00-\x1f\x7f]", value)
        or "\\" in value
    ):
        raise ConfigurationError(message + "an absolute URL is required")
    if parts.username is not None or parts.password is not None:
        raise ConfigurationError(message + "embedded credentials are refused")
    if "?" in value or "#" in value:
        raise ConfigurationError(message + "query and fragment are refused")
    if parts.scheme != "https" and not (
        parts.scheme == "http" and host in ("127.0.0.1", "::1", "localhost")
    ):
        raise ConfigurationError(
            message + "HTTPS is required except on loopback"
        )
    return value.rstrip("/")


def string_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigurationError(f"{label} must be an array")
    return tuple(
        V.require_string(item, f"{label}[{i}]") for i, item in enumerate(value)
    )


def run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AgentSquadError(f"git {arguments[0]} failed: {error}") from None


def git_output(root: Path, *arguments: str) -> str:
    result = run_git(root, *arguments)
    if result.returncode:
        raise AgentSquadError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.rstrip("\n")


@dataclass(frozen=True)
class Worktree:
    root: Path
    head: str
    branch: str | None


def list_worktrees(root: Path) -> tuple[Worktree, ...]:
    fields: dict[str, str] = {}
    result = []
    for line in git_output(
        root, "worktree", "list", "--porcelain", "-z"
    ).split("\0"):
        if not line:
            if "worktree" in fields:
                result.append(
                    Worktree(
                        Path(fields["worktree"]).resolve(),
                        fields.get("HEAD", ""),
                        fields.get("branch"),
                    )
                )
            fields = {}
        else:
            key, _, value = line.partition(" ")
            fields[key] = value
    return tuple(result)


@dataclass(frozen=True)
class Repository:
    root: Path
    primary: Path
    common: Path
    configuration: Configuration | None = None
    # Startup facts only; later decisions must call list_worktrees afresh.
    discovered_worktrees: tuple[Worktree, ...] = ()

    @property
    def control_root(self) -> Path:
        return self.primary / CONTROL_DIRECTORY_NAME

    @property
    def configuration_path(self) -> Path:
        return self.control_root / "config.json"

    def resolve_root(self, value: str) -> Path:
        return (self.primary / Path(value).expanduser()).resolve()

    def default_role(self) -> str:
        if self.configuration is not None:
            parent = self.resolve_root(self.configuration.worktree_root)
            if self.root.parent == parent and re.fullmatch(
                r"reviewer-pr[1-9][0-9]*-[0-9a-f]{7}", self.root.name
            ):
                return "reviewer"
        return "implementer"


def discover_git_worktree(start: Path) -> Repository:
    result = run_git(
        start, "rev-parse", "--path-format=relative", "--is-bare-repository",
        "--show-toplevel", "--git-common-dir",
    )
    # Git emits the bare flag before --show-toplevel fails in a bare repo.
    if result.stdout.split("\n", 1)[0] == "true":
        raise AgentSquadError("a non-bare Git worktree is required")
    if result.returncode:
        raise AgentSquadError(result.stderr.strip() or result.stdout.strip())
    _, root_text, common_text = result.stdout.rstrip("\n").split("\n", 2)
    # The top-level path is ./ or a sequence of ../ components, so a newline
    # in the directory's name cannot be mistaken for a field separator.
    root = (start / root_text).resolve()
    common = (start / common_text).resolve()
    worktrees = list_worktrees(root)
    if not worktrees:
        raise AgentSquadError("cannot discover the primary worktree")
    return Repository(
        root, worktrees[0].root, common, discovered_worktrees=worktrees,
    )


def validate_roots(
    repository: Repository, *, writable: bool = False,
    worktrees: tuple[Worktree, ...] | None = None,
) -> None:
    config = repository.configuration
    assert config is not None
    roots = [
        repository.resolve_root(config.worktree_root),
        repository.resolve_root(config.scratch_root),
    ]
    if roots[0].is_relative_to(roots[1]) or roots[1].is_relative_to(roots[0]):
        raise ConfigurationError(
            "worktree_root and scratch_root must not overlap"
        )
    for root in roots:
        for worktree in (
            list_worktrees(repository.root) if worktrees is None else worktrees
        ):
            if root.is_relative_to(worktree.root) and not root.is_relative_to(
                worktree.root / CONTROL_DIRECTORY_NAME
            ):
                raise ConfigurationError(
                    "root inside a worktree must be under .agent-squad/:"
                    f" {root}"
                )
        if writable:
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=root) as stream:
                stream.write(b"root write probe")
                stream.flush()


def load_initialized_repository(start: Path) -> Repository:
    repository = discover_git_worktree(start)
    try:
        if (
            repository.control_root.is_symlink()
            or repository.configuration_path.is_symlink()
        ):
            raise ConfigurationError(
                "control root and config.json must not be symlinks"
            )
        config = Configuration.from_dict(
            decode_json(
                repository.configuration_path.read_text(encoding="utf-8")
            )
        )
        repository = replace(repository, configuration=config)
        validate_roots(repository, worktrees=repository.discovered_worktrees)
        validate_token_files(repository)
        return repository
    except (OSError, ValueError, ConfigurationError) as error:
        raise ConfigurationError(f"{error}; run agent-squad init") from None


def validate_token_files(repository: Repository) -> None:
    """Validate both configured files locally before allowing any request."""
    assert repository.configuration is not None
    if repository.configuration.forge.kind == "forgejo":
        from .forge import ForgeError
        from .forgejo import read_token

        for role in ("implementer", "reviewer"):
            try:
                read_token(repository, role)
            except ForgeError as error:
                raise ConfigurationError(str(error)) from None


def remote_coordinates(url: str) -> tuple[str, str]:
    path = urlsplit(url).path if "://" in url else url.split(":", 1)[-1]
    parts = path.rstrip("/").split("/")
    if len(parts) < 2 or not parts[-2] or not parts[-1]:
        raise ConfigurationError(
            "cannot derive owner/repo from origin; use --owner and --repo"
        )
    return parts[-2], parts[-1].removesuffix(".git")


def atomic_config(path: Path, config: Configuration) -> None:
    """Publish configuration only after its temporary write succeeds."""
    descriptor, temporary = tempfile.mkstemp(
        prefix=".config-", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(config.to_dict(), stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists() or path.is_symlink():
            raise ConfigurationError(
                "config.json appeared during init; rerun to validate it"
            )
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def initialize_repository(
    start: Path,
    *,
    implementer_account: str,
    reviewer_account: str,
    owner: str | None = None,
    repo: str | None = None,
    base_branch: str | None = None,
    forge: str = "github",
    base_url: str | None = None,
    implementer_token_file: str | None = None,
    reviewer_token_file: str | None = None,
) -> dict[str, object]:
    from .forge import make_forge

    repository = discover_git_worktree(start)
    url = git_output(repository.root, "remote", "get-url", "origin")
    derived_owner, derived_repo = (
        (owner, repo) if owner and repo else remote_coordinates(url)
    )
    defaults = Configuration.from_dict(
        {
            "schema_version": 2,
            "forge": {
                "kind": forge,
                "owner": owner or derived_owner,
                "repo": (repo or derived_repo).removesuffix(".git"),
                **({"base_url": base_url} if base_url is not None else {}),
            },
            "implementer": {
                "agent_name": "implementer",
                "kind": "codex",
                "forge_account": implementer_account,
                **({"token_file": implementer_token_file}
                   if implementer_token_file is not None else {}),
            },
            "reviewer": {
                "kind": "claude",
                "start_args": [],
                "forge_account": reviewer_account,
                **({"token_file": reviewer_token_file}
                   if reviewer_token_file is not None else {}),
            },
            "developer_accounts": [],
            "base_branch": base_branch or "main",
            "max_review_passes": 3,
            "merge_method": "merge",
            "worktree_root": ".agent-squad/worktrees",
            "scratch_root": ".agent-squad/review-scratch",
        }
    )
    exists = (
        repository.configuration_path.exists()
        or repository.configuration_path.is_symlink()
    )
    if exists:
        repository = load_initialized_repository(start)
    else:
        if repository.control_root.is_symlink():
            raise ConfigurationError("control root must not be a symlink")
        repository = Repository(
            repository.root, repository.primary, repository.common, defaults
        )
    config = repository.configuration
    assert config is not None
    validate_roots(repository)
    validate_token_files(repository)
    # No network operation precedes configuration and token-file validation.
    # Git uses the configured origin; API reads use only the explicit API URL.
    refs = git_output(
        repository.root, "ls-remote", "--symref", "origin", "HEAD"
    )
    match = re.search(r"^ref: refs/heads/(.+)\tHEAD$", refs, re.MULTILINE)
    defaults = replace(
        defaults, base_branch=base_branch or (match[1] if match else "main"),
    )
    if not exists:
        config = defaults
        repository = replace(repository, configuration=config)
    remote_branch = git_output(
        repository.root,
        "ls-remote",
        "--heads",
        "origin",
        f"refs/heads/{config.base_branch}",
    )
    if not remote_branch:
        raise ConfigurationError(
            f"base_branch does not exist on origin: {config.base_branch}"
        )
    make_forge(repository, "implementer").repository_record()
    validate_roots(repository, writable=True)
    repository.control_root.mkdir(parents=True, exist_ok=True)
    exclude = repository.common / "info/exclude"
    if exclude.is_symlink():
        raise ConfigurationError("info/exclude must not be a symlink")
    content = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    missing = [
        p for p in LOCAL_EXCLUDE_PATTERNS if p not in content.splitlines()
    ]
    if missing:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as stream:
            stream.write(
                ("\n" if content and not content.endswith("\n") else "")
                + "\n".join(missing)
                + "\n"
            )
    if not exists:
        atomic_config(repository.configuration_path, config)
    differences = {
        k: {"configured": v, "default": defaults.to_dict()[k]}
        for k, v in config.to_dict().items()
        if v != defaults.to_dict()[k]
    }
    return {
        "configuration_path": str(repository.configuration_path),
        "created": not exists,
        "differences": differences,
    }
