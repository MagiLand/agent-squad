"""Repository discovery, configuration, and safe initialization."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tempfile


SCHEMA_VERSION = 1
CONTROL_DIRECTORY_NAME = ".agent-squad"
REVIEW_DIRECTORY_NAME = ".agent-squad-review"
CONFIGURATION_FILE_NAME = "config.json"
LOCAL_EXCLUDE_PATTERNS = (
    f"{CONTROL_DIRECTORY_NAME}/",
    f"{REVIEW_DIRECTORY_NAME}/",
)


class AgentSquadError(Exception):
    """Base class for actionable user-facing failures."""


class ConfigurationError(AgentSquadError):
    """Raised when repository-local configuration is invalid."""


class RepositoryError(AgentSquadError):
    """Raised when the current directory is not a usable Git worktree."""


class InitializationError(AgentSquadError):
    """Raised when validated initialization cannot be completed safely."""


class AgentKind(StrEnum):
    """Supported installed agent harnesses."""

    CLAUDE = "claude"
    CODEX = "codex"


@dataclass(frozen=True)
class ImplementerConfiguration:
    """Configured identity and harness kind for the Implementer."""

    agent_name: str
    kind: AgentKind


@dataclass(frozen=True)
class ReviewerConfiguration:
    """Configured Reviewer harness and native start arguments."""

    kind: AgentKind
    start_args: tuple[str, ...]


@dataclass(frozen=True)
class Configuration:
    """Validated schema-versioned repository configuration."""

    schema_version: int
    implementer: ImplementerConfiguration
    reviewer: ReviewerConfiguration
    base_ref: str
    review_worktree_root: Path
    max_completed_change_reviews: int
    allowed_generated_paths: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: object) -> "Configuration":
        """Validate and construct configuration from decoded JSON."""

        data = _require_object(value, "configuration")
        _check_fields(
            data,
            required={
                "schema_version",
                "implementer",
                "reviewer",
                "base_ref",
                "review_worktree_root",
                "max_completed_change_reviews",
            },
            optional={"allowed_generated_paths"},
            path="configuration",
        )

        schema_version = _require_int(
            data["schema_version"],
            "configuration.schema_version",
        )
        if schema_version != SCHEMA_VERSION:
            raise ConfigurationError(
                f"configuration.schema_version must be {SCHEMA_VERSION}"
            )

        implementer_data = _require_object(
            data["implementer"], "configuration.implementer"
        )
        _check_fields(
            implementer_data,
            required={"agent_name", "kind"},
            optional=set(),
            path="configuration.implementer",
        )
        implementer_name = _require_string(
            implementer_data["agent_name"],
            "configuration.implementer.agent_name",
        )
        implementer_kind = _require_agent_kind(
            implementer_data["kind"], "configuration.implementer.kind"
        )

        reviewer_data = _require_object(
            data["reviewer"], "configuration.reviewer"
        )
        _check_fields(
            reviewer_data,
            required={"kind"},
            optional={"start_args"},
            path="configuration.reviewer",
        )
        reviewer_kind = _require_agent_kind(
            reviewer_data["kind"], "configuration.reviewer.kind"
        )
        start_args_value = reviewer_data.get("start_args", [])
        start_args = _require_string_list(
            start_args_value,
            "configuration.reviewer.start_args",
        )

        base_ref = _require_string(data["base_ref"], "configuration.base_ref")
        if base_ref != base_ref.strip() or "\x00" in base_ref:
            raise ConfigurationError(
                "configuration.base_ref must not contain surrounding "
                "whitespace or null bytes"
            )

        review_root_text = _require_string(
            data["review_worktree_root"],
            "configuration.review_worktree_root",
        )
        if "\x00" in review_root_text:
            raise ConfigurationError(
                "configuration.review_worktree_root must not contain "
                "null bytes"
            )
        review_root = Path(review_root_text).expanduser()
        if not review_root.is_absolute():
            raise ConfigurationError(
                "configuration.review_worktree_root must be an absolute path"
            )

        review_limit = _require_int(
            data["max_completed_change_reviews"],
            "configuration.max_completed_change_reviews",
        )
        if review_limit < 1:
            raise ConfigurationError(
                "configuration.max_completed_change_reviews must be a "
                "positive integer"
            )

        generated_paths = _require_string_list(
            data.get("allowed_generated_paths", []),
            "configuration.allowed_generated_paths",
        )
        _validate_generated_paths(generated_paths)

        return cls(
            schema_version=schema_version,
            implementer=ImplementerConfiguration(
                agent_name=implementer_name,
                kind=implementer_kind,
            ),
            reviewer=ReviewerConfiguration(
                kind=reviewer_kind,
                start_args=start_args,
            ),
            base_ref=base_ref,
            review_worktree_root=review_root,
            max_completed_change_reviews=review_limit,
            allowed_generated_paths=generated_paths,
        )

    def to_dict(self) -> dict[str, object]:
        """Return the stable JSON representation of this configuration."""

        return {
            "schema_version": self.schema_version,
            "implementer": {
                "agent_name": self.implementer.agent_name,
                "kind": self.implementer.kind,
            },
            "reviewer": {
                "kind": self.reviewer.kind,
                "start_args": list(self.reviewer.start_args),
            },
            "base_ref": self.base_ref,
            "review_worktree_root": str(self.review_worktree_root),
            "max_completed_change_reviews": self.max_completed_change_reviews,
            "allowed_generated_paths": list(self.allowed_generated_paths),
        }


@dataclass(frozen=True)
class GitWorktree:
    """Canonical paths discovered from Git."""

    root: Path
    common_directory: Path
    local_exclude_path: Path


@dataclass(frozen=True)
class InitializationResult:
    """Effective repository state after successful initialization."""

    repository_root: Path
    configuration_path: Path
    git_exclude_path: Path
    configuration_created: bool
    git_exclude_updated: bool


def default_review_worktree_root() -> Path:
    """Return the default root for disposable review worktrees."""

    default_data_home = Path.home() / ".local" / "share"
    configured_data_home = os.environ.get("XDG_DATA_HOME")
    if configured_data_home:
        data_home = Path(configured_data_home).expanduser()
        if not data_home.is_absolute():
            data_home = default_data_home
    else:
        data_home = default_data_home
    review_root = data_home / "agent-squad" / "worktrees"
    try:
        return review_root.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ConfigurationError(
            "the default review worktree root cannot be resolved; check "
            f"XDG_DATA_HOME and HOME: {error}"
        ) from error


def default_configuration(review_worktree_root: Path) -> Configuration:
    """Build the minimal default configuration defined by the specification."""

    return Configuration.from_dict(
        {
            "schema_version": SCHEMA_VERSION,
            "implementer": {
                "agent_name": "codex-main",
                "kind": "codex",
            },
            "reviewer": {
                "kind": "claude",
                "start_args": [],
            },
            "base_ref": "origin/main",
            "review_worktree_root": str(review_worktree_root),
            "max_completed_change_reviews": 4,
            "allowed_generated_paths": [],
        }
    )


def load_configuration(path: Path) -> Configuration:
    """Load and fully validate an existing configuration file."""

    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ConfigurationError(f"{path} must contain UTF-8 JSON") from error
    except OSError as error:
        raise ConfigurationError(f"cannot read {path}: {error}") from error

    try:
        decoded = json.loads(raw, object_pairs_hook=_object_without_duplicates)
    except (json.JSONDecodeError, _DuplicateKeyError) as error:
        raise ConfigurationError(
            f"{path.name} contains invalid JSON: {error}"
        ) from error
    return Configuration.from_dict(decoded)


def discover_git_worktree(start: Path) -> GitWorktree:
    """Resolve the current non-bare Git worktree and its common metadata."""

    working_directory = start.resolve(strict=False)
    if not working_directory.is_dir():
        raise RepositoryError(
            f"current path is not a directory: {working_directory}"
        )

    inside_result = _run_git(
        working_directory,
        "rev-parse",
        "--is-inside-work-tree",
    )
    if inside_result.returncode != 0:
        detail = inside_result.stderr.strip()
        suffix = f" Git reported: {detail}" if detail else ""
        raise RepositoryError(
            "Git could not confirm that the current directory is inside a "
            "Git worktree. Run agent-squad init from a non-bare Git "
            f"checkout.{suffix}"
        )
    if inside_result.stdout.strip() != "true":
        raise RepositoryError(
            "the current directory is not inside a Git worktree. "
            "Run agent-squad init from a non-bare Git checkout."
        )

    root = _git_path(working_directory, "--show-toplevel")
    common_directory = _git_path(working_directory, "--git-common-dir")
    if not root.is_dir() or not common_directory.is_dir():
        raise RepositoryError(
            "Git returned repository paths that do not exist; inspect the "
            "worktree and retry initialization"
        )

    return GitWorktree(
        root=root,
        common_directory=common_directory,
        local_exclude_path=common_directory / "info" / "exclude",
    )


def initialize_repository(
    start: Path,
    *,
    review_worktree_root: Path | None = None,
) -> InitializationResult:
    """Create or validate local configuration and Git exclusions."""

    worktree = discover_git_worktree(start)
    control_root = worktree.root / CONTROL_DIRECTORY_NAME
    configuration_path = control_root / CONFIGURATION_FILE_NAME

    _validate_control_root(control_root)
    configuration_exists = _validate_configuration_path(configuration_path)
    if configuration_exists:
        configuration = load_configuration(configuration_path)
        configuration_bytes = None
    else:
        review_root = review_worktree_root or default_review_worktree_root()
        configuration = default_configuration(review_root)
        configuration_bytes = _encode_configuration(configuration)
    _validate_review_worktree_root(configuration, worktree.root)

    exclude_path = worktree.local_exclude_path
    original_exclude = _read_local_exclude(exclude_path)
    updated_exclude = _add_local_exclude_patterns(original_exclude)
    exclude_needs_update = updated_exclude != original_exclude

    created_paths: list[Path] = []
    configuration_created = False
    try:
        if not control_root.exists():
            control_root.mkdir(mode=0o700)
            created_paths.append(control_root)

        if configuration_bytes is not None:
            _atomic_write(configuration_path, configuration_bytes, mode=0o600)
            configuration_created = True
            created_paths.append(configuration_path)

        if exclude_needs_update:
            info_directory = exclude_path.parent
            if not info_directory.exists():
                info_directory.mkdir(mode=0o755)
                created_paths.append(info_directory)
            _atomic_write(
                exclude_path,
                updated_exclude,
                mode=_file_mode(exclude_path),
            )
    except OSError as error:
        rollback_errors = _rollback_created_paths(created_paths)
        rollback_note = (
            " Rollback also encountered: " + "; ".join(rollback_errors)
            if rollback_errors
            else ""
        )
        raise InitializationError(
            f"could not complete initialization: {error}.{rollback_note}"
        ) from error

    return InitializationResult(
        repository_root=worktree.root,
        configuration_path=configuration_path,
        git_exclude_path=exclude_path,
        configuration_created=configuration_created,
        git_exclude_updated=exclude_needs_update,
    )


def _require_object(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise ConfigurationError(f"{path} must be a JSON object")
    return value


def _check_fields(
    data: dict[str, object],
    *,
    required: set[str],
    optional: set[str],
    path: str,
) -> None:
    missing = sorted(required - data.keys())
    if missing:
        raise ConfigurationError(
            f"{path} is missing required field(s): {', '.join(missing)}"
        )
    unknown = sorted(data.keys() - required - optional)
    if unknown:
        label = "field" if len(unknown) == 1 else "fields"
        raise ConfigurationError(
            f"{path} has unknown {label}: {', '.join(unknown)}"
        )


def _require_string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{path} must be a non-empty string")
    return value


def _require_int(value: object, path: str) -> int:
    if type(value) is not int:
        raise ConfigurationError(f"{path} must be an integer")
    return value


def _require_agent_kind(value: object, path: str) -> AgentKind:
    kind = _require_string(value, path)
    try:
        return AgentKind(kind)
    except ValueError:
        supported = ", ".join(member.value for member in AgentKind)
        raise ConfigurationError(
            f"{path} must be one of: {supported}"
        ) from None


def _require_string_list(
    value: object,
    path: str,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigurationError(f"{path} must be a JSON array of strings")
    strings: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise ConfigurationError(
                f"{path}[{index}] must be a non-empty string"
            )
        if "\x00" in item:
            raise ConfigurationError(
                f"{path}[{index}] must not contain null bytes"
            )
        strings.append(item)
    return tuple(strings)


def _validate_generated_paths(paths: tuple[str, ...]) -> None:
    seen: set[PurePosixPath] = set()
    for index, value in enumerate(paths):
        parsed = PurePosixPath(value)
        unsafe = (
            parsed == PurePosixPath(".")
            or parsed.is_absolute()
            or ".." in parsed.parts
        )
        if unsafe:
            raise ConfigurationError(
                f"configuration.allowed_generated_paths[{index}] must be a "
                "narrow repository-relative path without '..'"
            )
        if parsed in seen:
            raise ConfigurationError(
                "configuration.allowed_generated_paths contains duplicate "
                f"path: {value}"
            )
        seen.add(parsed)


def _validate_review_worktree_root(
    configuration: Configuration,
    implementation_root: Path,
) -> None:
    canonical_implementation_root = implementation_root.resolve(strict=False)
    try:
        canonical_review_root = configuration.review_worktree_root.resolve(
            strict=False
        )
    except (OSError, RuntimeError) as error:
        raise ConfigurationError(
            "configuration.review_worktree_root cannot be resolved: "
            f"{error}"
        ) from error
    review_lineage = (
        canonical_review_root,
        *canonical_review_root.parents,
    )
    is_inside_worktree = canonical_implementation_root in review_lineage
    if not is_inside_worktree:
        try:
            implementation_identity = _existing_path_identity(
                canonical_implementation_root
            )
        except OSError as error:
            raise RepositoryError(
                "cannot inspect the implementation worktree "
                f"{canonical_implementation_root}: {error}"
            ) from error
        if implementation_identity is None:
            raise RepositoryError(
                "the implementation worktree disappeared during "
                f"initialization: {canonical_implementation_root}"
            )

        try:
            is_inside_worktree = any(
                _existing_path_identity(candidate)
                == implementation_identity
                for candidate in review_lineage
            )
        except OSError as error:
            raise ConfigurationError(
                "configuration.review_worktree_root cannot be inspected: "
                f"{error}"
            ) from error

    if is_inside_worktree:
        raise ConfigurationError(
            "configuration.review_worktree_root must be outside the "
            "implementation worktree"
        )


def _existing_path_identity(path: Path) -> tuple[int, int] | None:
    try:
        status = path.stat()
    except FileNotFoundError:
        return None
    return status.st_dev, status.st_ino


class _DuplicateKeyError(ValueError):
    pass


def _object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate object key: {key}")
        result[key] = value
    return result


def _encode_configuration(configuration: Configuration) -> bytes:
    text = json.dumps(configuration.to_dict(), indent=2, ensure_ascii=False)
    return f"{text}\n".encode("utf-8")


def _run_git(
    working_directory: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=working_directory,
            check=False,
            shell=False,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise RepositoryError(
            "Git is not installed or is not available on PATH"
        ) from error
    except OSError as error:
        raise RepositoryError(f"could not run Git: {error}") from error


def _git_path(working_directory: Path, argument: str) -> Path:
    result = _run_git(working_directory, "rev-parse", argument)
    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown Git error"
        raise RepositoryError(f"git rev-parse {argument} failed: {detail}")
    raw_path = result.stdout.rstrip("\r\n")
    if not raw_path:
        raise RepositoryError(
            f"git rev-parse {argument} returned an empty path"
        )
    path = Path(raw_path)
    if not path.is_absolute():
        path = working_directory / path
    return path.resolve(strict=False)


def _validate_control_root(control_root: Path) -> None:
    if control_root.is_symlink():
        raise InitializationError(
            f"{control_root} is a symbolic link; the canonical control root "
            "must be a repository directory"
        )
    if control_root.exists() and not control_root.is_dir():
        raise InitializationError(f"{control_root} must be a directory")


def _validate_configuration_path(configuration_path: Path) -> bool:
    if configuration_path.is_symlink():
        raise InitializationError(
            f"{configuration_path} is a symbolic link; refusing to read or "
            "overwrite it"
        )
    if not configuration_path.exists():
        return False
    if not configuration_path.is_file():
        raise InitializationError(
            f"{configuration_path} must be a regular file"
        )
    return True


def _read_local_exclude(path: Path) -> bytes:
    if path.is_symlink():
        raise InitializationError(
            f"{path} is a symbolic link; refusing to replace Git's local "
            "exclude"
        )
    if path.exists() and not path.is_file():
        raise InitializationError(
            f"Git local exclude path must be a regular file: {path}"
        )
    if path.parent.is_symlink() or (
        path.parent.exists() and not path.parent.is_dir()
    ):
        raise InitializationError(
            f"Git metadata path must be a directory: {path.parent}"
        )
    if not path.exists():
        return b""
    try:
        return path.read_bytes()
    except OSError as error:
        raise InitializationError(
            f"cannot read Git local exclude {path}: {error}"
        ) from error


def _add_local_exclude_patterns(content: bytes) -> bytes:
    existing_lines = {line.rstrip(b"\r") for line in content.splitlines()}
    missing = [
        pattern.encode("utf-8")
        for pattern in LOCAL_EXCLUDE_PATTERNS
        if pattern.encode("utf-8") not in existing_lines
    ]
    if not missing:
        return content

    updated = bytearray(content)
    if updated and not updated.endswith((b"\n", b"\r")):
        updated.extend(b"\n")
    for pattern in missing:
        updated.extend(pattern)
        updated.extend(b"\n")
    return bytes(updated)


def _file_mode(path: Path) -> int:
    if not path.exists():
        return 0o644
    return stat.S_IMODE(path.stat().st_mode)


def _atomic_write(path: Path, content: bytes, *, mode: int) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_path.chmod(mode)
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        temporary_path.replace(path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _rollback_created_paths(created_paths: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in reversed(created_paths):
        try:
            if path.is_symlink() or not path.is_dir():
                path.unlink(missing_ok=True)
            else:
                path.rmdir()
        except OSError as error:
            errors.append(f"could not remove newly created {path}: {error}")
    return errors
