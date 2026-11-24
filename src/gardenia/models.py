"""Shared data models, version parsing, and subprocess helpers for gardenia."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import os
import re
import subprocess
from typing import Any, Literal, Mapping

Scope = Literal["host", "repo"]
Severity = Literal["error", "warning", "info"]

CHECK_FORMAT = "gardenia-check-v1"
REPOS_REGISTRY_FORMAT = "gardenia-repos-v1"
REPOS_LIST_FORMAT = "gardenia-repos-list-v1"
REPOS_CHECK_FORMAT = "gardenia-repos-check-v1"
REPOS_UPGRADE_FORMAT = "gardenia-repos-upgrade-v1"
GITHUB_OWNER = "xu-hao"

MANAGED_BEGIN = "<!-- gardenia:begin -->"
MANAGED_END = "<!-- gardenia:end -->"

SUPPORTED_CLIENTS: tuple[str, ...] = ("codex", "claude", "agy")
PLUGIN_PACKAGES: tuple[str, ...] = ("agrein", "agcoord", "howhard", "agroute")
CLI_WITH_PLUGINS: tuple[str, ...] = ("howhard", "agroute", "agcoord")

DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_TMPFS_INODES = 262144


@dataclass(frozen=True)
class CliSpec:
    """Definition of a required host-level Python CLI tool."""

    package: str
    binary: str
    min_version: str
    linux_only: bool = False


CLI_SPECS: tuple[CliSpec, ...] = (
    CliSpec(package="howhard", binary="howhard", min_version="5.0.0", linux_only=False),
    CliSpec(package="agroute", binary="agr", min_version="0.6.0", linux_only=False),
    CliSpec(package="agcoord", binary="agc", min_version="0.10.0", linux_only=True),
)


class GardeniaError(Exception):
    """Error carrying a deterministic CLI exit code (default: 2 for usage/precondition)."""

    def __init__(self, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


@dataclass(frozen=True)
class Finding:
    """Diagnostic finding tagged explicitly with host or repository scope."""

    scope: Scope
    severity: Severity
    component: str
    message: str
    remediation: str

    def to_dict(self) -> dict[str, str]:
        return {
            "scope": self.scope,
            "severity": self.severity,
            "component": self.component,
            "message": self.message,
            "remediation": self.remediation,
        }


@dataclass
class PlannedAction:
    """Mutating or skipped step planned/executed by ``host setup`` or ``repo init``."""

    kind: Literal["command", "write_file", "symlink", "skip"]
    component: str
    description: str
    command: list[str] | None = None
    path: str | None = None
    status: Literal["planned", "executed", "skipped", "failed"] = "planned"
    returncode: int | None = None
    stdout: str | None = None
    stderr: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CommandResult:
    """Normalized subprocess execution outcome."""

    ok: bool
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    missing: bool = False


_SEMVER_RE = re.compile(r"(?:^|[^0-9A-Za-z._-])v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b")
_CORE_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def extract_version(text: str) -> str | None:
    """Extract the first semantic version string (e.g. ``4.0.0``) from CLI output."""

    match = _SEMVER_RE.search(text.strip())
    if not match:
        return None
    return match.group(1)


def parse_version(raw: str | None) -> tuple[int, int, int, int, str] | None:
    """Parse ``MAJOR.MINOR.PATCH[-prerelease]`` into a comparable tuple.

    Stable releases sort higher than pre-releases with the same core triple.
    """

    if not raw:
        return None
    cleaned = raw.strip().lstrip("v")
    match = _CORE_VERSION_RE.match(cleaned)
    if not match:
        return None
    major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3))
    prerelease = match.group(4)
    is_stable = 1 if prerelease is None else 0
    return (major, minor, patch, is_stable, prerelease or "")


def version_lt(left: str | None, right: str | None) -> bool | None:
    """Return True if ``left < right``, False if ``left >= right``, or None if unparseable."""

    parsed_left = parse_version(left)
    parsed_right = parse_version(right)
    if parsed_left is None or parsed_right is None:
        return None
    return parsed_left < parsed_right


def is_finite_number(value: Any) -> bool:
    """Return True if value is an int/float (excluding bool) and finite."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def compute_ok(findings: list[Finding], *, strict: bool = False) -> bool:
    """Return True when findings satisfy the active strictness threshold."""

    for finding in findings:
        if finding.severity == "error":
            return False
        if strict and finding.severity == "warning":
            return False
    return True


def run_cmd(
    argv: list[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Execute a subprocess with bounded timeout and normalized error capture."""

    effective_env = dict(os.environ if env is None else env)
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=effective_env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return CommandResult(
            ok=False,
            returncode=None,
            stdout="",
            stderr=str(exc),
            timed_out=False,
            missing=True,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout.decode(errors="replace") if exc.stdout else "")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr.decode(errors="replace") if exc.stderr else "")
        timeout_note = f"Command timed out after {timeout}s"
        stderr = f"{stderr.rstrip()}\n{timeout_note}" if stderr.strip() else timeout_note
        return CommandResult(
            ok=False,
            returncode=None,
            stdout=stdout,
            stderr=stderr,
            timed_out=True,
            missing=False,
        )
    return CommandResult(
        ok=completed.returncode == 0,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        timed_out=False,
        missing=False,
    )
