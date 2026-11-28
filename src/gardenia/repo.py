"""Per-repository policy rendering, initialization, and diagnostics (strictly isolated from host state)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import os
from pathlib import Path
import re
import shutil
import tomllib
from typing import Any, Mapping

from gardenia import __version__
from gardenia.models import (
    CHECK_FORMAT,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_TMPFS_INODES,
    ENTRY_NAME_RE,
    MANAGED_BEGIN,
    MANAGED_END,
    PLUGIN_SNIPPET_FILENAME,
    RESERVED_ENTRY_NAMES,
    Finding,
    GardeniaError,
    PlannedAction,
    compute_ok,
    is_finite_number,
    run_cmd,
)

GIB = 1024 * 1024 * 1024

ALLOWED_ROUTING = ("agroute", "howhard", "none")
ALLOWED_AGCOORD = ("auto", "required", "none")

CONFIG_KEYS = frozenset(
    {
        "gate",
        "cpu",
        "memory_gb",
        "tmpfs_gb",
        "no_overflow",
        "routing",
        "agcoord",
        "docs_exemption",
        "claude_symlink",
        "tools",
    }
)

_ANY_BLOCK_MARKER_RE = re.compile(r"<!--\s*[A-Za-z0-9][A-Za-z0-9._-]*:(?:begin|end)\s*-->")


@dataclass(frozen=True)
class RepoProfile:
    """Tracked repository profile controlling managed AGENTS.md instructions."""

    gate: str = "bash scripts/check"
    cpu: int = 4
    memory_gb: float = 4.0
    tmpfs_gb: float | None = None
    no_overflow: bool = True
    routing: str = "agroute"
    agcoord: str = "auto"
    docs_exemption: bool = True
    claude_symlink: bool = True
    tools: tuple[str, ...] = ()

    def validate(self) -> None:
        if not isinstance(self.gate, str) or not self.gate.strip():
            raise GardeniaError("Repo profile 'gate' must be a non-empty string.", exit_code=2)
        if isinstance(self.cpu, bool) or not isinstance(self.cpu, int) or self.cpu < 1:
            raise GardeniaError("Repo profile 'cpu' must be an integer >= 1.", exit_code=2)
        if not is_finite_number(self.memory_gb) or float(self.memory_gb) <= 0.0:
            raise GardeniaError("Repo profile 'memory_gb' must be a positive finite number.", exit_code=2)
        if self.tmpfs_gb is not None and (not is_finite_number(self.tmpfs_gb) or float(self.tmpfs_gb) <= 0.0):
            raise GardeniaError("Repo profile 'tmpfs_gb' must be a positive finite number when set.", exit_code=2)
        if not isinstance(self.no_overflow, bool):
            raise GardeniaError("Repo profile 'no_overflow' must be a boolean.", exit_code=2)
        if self.routing not in ALLOWED_ROUTING:
            raise GardeniaError(
                f"Repo profile 'routing' must be one of: {', '.join(ALLOWED_ROUTING)}.",
                exit_code=2,
            )
        if self.agcoord not in ALLOWED_AGCOORD:
            raise GardeniaError(
                f"Repo profile 'agcoord' must be one of: {', '.join(ALLOWED_AGCOORD)}.",
                exit_code=2,
            )
        if not isinstance(self.docs_exemption, bool):
            raise GardeniaError("Repo profile 'docs_exemption' must be a boolean.", exit_code=2)
        if not isinstance(self.claude_symlink, bool):
            raise GardeniaError("Repo profile 'claude_symlink' must be a boolean.", exit_code=2)
        if not isinstance(self.tools, tuple) or any(
            not isinstance(item, str) or not ENTRY_NAME_RE.fullmatch(item) for item in self.tools
        ):
            raise GardeniaError(
                "Repo profile 'tools' must be a list of tool names (letters, digits, '.', '_', '-').",
                exit_code=2,
            )
        if len(set(self.tools)) != len(self.tools):
            raise GardeniaError("Repo profile 'tools' must not contain duplicate names.", exit_code=2)
        reserved = sorted(set(self.tools) & RESERVED_ENTRY_NAMES)
        if reserved:
            raise GardeniaError(f"Repo profile 'tools' must not list reserved name(s): {', '.join(reserved)}.", exit_code=2)

    def memory_bytes(self) -> int:
        return int(round(float(self.memory_gb) * GIB))

    def tmpfs_bytes(self) -> int | None:
        if self.tmpfs_gb is None:
            return None
        return int(round(float(self.tmpfs_gb) * GIB))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["tools"] = list(self.tools)
        return data


@dataclass(frozen=True)
class GitRepoInfo:
    """Resolved Git repository root and common state directory."""

    root: Path
    git_dir: Path
    git_common_dir: Path
    is_linked_worktree: bool


def _effective_env(env: Mapping[str, str] | None) -> dict[str, str]:
    return dict(os.environ if env is None else env)


def resolve_git_repo(
    repo_path: str | os.PathLike[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> GitRepoInfo:
    """Resolve Git top-level, git-dir, and git-common-dir; refuse non-Git directories."""

    target = Path(repo_path).resolve() if repo_path is not None else Path.cwd().resolve()
    if not target.exists():
        raise GardeniaError(f"Repository path does not exist: {target}", exit_code=2)
    if not target.is_dir():
        raise GardeniaError(f"Repository path is not a directory: {target}", exit_code=2)

    res = run_cmd(
        ["git", "rev-parse", "--show-toplevel", "--git-dir", "--git-common-dir"],
        cwd=target,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        env=_effective_env(env),
    )
    if not res.ok:
        raise GardeniaError(
            f"Not inside a Git repository: {target}",
            exit_code=2,
        )

    lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    if len(lines) < 3:
        raise GardeniaError(
            f"Unexpected output from git rev-parse in {target}: {res.stdout.strip()}",
            exit_code=2,
        )
    root = Path(lines[0]).resolve()
    raw_git_dir = Path(lines[1])
    raw_common_dir = Path(lines[2])
    git_dir = raw_git_dir.resolve() if raw_git_dir.is_absolute() else (target / raw_git_dir).resolve()
    git_common_dir = raw_common_dir.resolve() if raw_common_dir.is_absolute() else (target / raw_common_dir).resolve()
    return GitRepoInfo(
        root=root,
        git_dir=git_dir,
        git_common_dir=git_common_dir,
        is_linked_worktree=(git_dir != git_common_dir),
    )


def try_resolve_git_repo(
    repo_path: str | os.PathLike[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> GitRepoInfo | None:
    """Resolve Git repository info or return None when outside a Git repo."""

    try:
        return resolve_git_repo(repo_path, env=env)
    except GardeniaError:
        return None


def _parse_profile_table(table: dict[str, Any], source_label: str) -> RepoProfile:
    unknown = sorted(set(table.keys()) - CONFIG_KEYS)
    if unknown:
        raise GardeniaError(
            f"Unknown key(s) in {source_label}: {', '.join(unknown)}. "
            f"Allowed keys: {', '.join(sorted(CONFIG_KEYS))}.",
            exit_code=2,
        )
    kwargs: dict[str, Any] = {}
    for key, val in table.items():
        if key in ("memory_gb", "tmpfs_gb") and not isinstance(val, bool) and isinstance(val, (int, float)):
            kwargs[key] = float(val)
        elif key == "tools" and isinstance(val, list):
            kwargs[key] = tuple(val)
        else:
            kwargs[key] = val
    profile = RepoProfile(**kwargs)
    profile.validate()
    return profile


def stealth_profile_path(git_common_dir: Path) -> Path:
    """Return the untracked stealth profile location shared by all worktrees of one clone."""

    return git_common_dir / "gardenia" / "gardenia.toml"


def _load_stealth_config(path: Path) -> dict[str, Any]:
    error: str | None = None
    profile = RepoProfile()
    try:
        profile = _parse_profile_table(tomllib.loads(path.read_text(encoding="utf-8")), str(path))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        error = str(exc)
    except GardeniaError as exc:
        error = exc.message
    return {
        "source": "stealth",
        "path": str(path),
        "valid": error is None,
        "profile": profile,
        "values": profile.to_dict(),
        "error": error,
        "duplicate_sources": False,
        "stealth": True,
        "stealth_ignored": False,
    }


def load_repo_config(repo_root: Path, git_common_dir: Path | None = None) -> dict[str, Any]:
    """Load the repository profile.

    Tracked ``gardenia.toml`` or ``pyproject.toml`` ``[tool.gardenia]`` take precedence. When
    neither exists, an untracked stealth profile under ``git_common_dir`` applies.
    """

    stealth_path = stealth_profile_path(git_common_dir) if git_common_dir is not None else None
    has_stealth = stealth_path is not None and stealth_path.is_file()
    cfg = _load_tracked_config(repo_root)
    tracked_section = cfg["source"] == "gardenia.toml" or (
        cfg["source"] == "pyproject.toml" and _has_pyproject_gardenia_table(repo_root)
    )
    if has_stealth and not tracked_section:
        assert stealth_path is not None
        return _load_stealth_config(stealth_path)
    cfg["stealth"] = False
    cfg["stealth_ignored"] = has_stealth
    return cfg


def _has_pyproject_gardenia_table(repo_root: Path) -> bool:
    try:
        data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    tool_section = data.get("tool")
    return isinstance(tool_section, dict) and "gardenia" in tool_section


def _load_tracked_config(repo_root: Path) -> dict[str, Any]:
    gardenia_toml = repo_root / "gardenia.toml"
    pyproject_toml = repo_root / "pyproject.toml"

    has_gardenia_toml = gardenia_toml.is_file()
    pyproject_table: dict[str, Any] | None = None
    pyproject_parse_error: str | None = None
    has_pyproject_section = False

    if pyproject_toml.is_file():
        try:
            py_data = tomllib.loads(pyproject_toml.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            pyproject_parse_error = str(exc)
        else:
            tool_section = py_data.get("tool")
            if isinstance(tool_section, dict) and "gardenia" in tool_section:
                has_pyproject_section = True
                raw_section = tool_section["gardenia"]
                if isinstance(raw_section, dict):
                    pyproject_table = raw_section
                else:
                    pyproject_parse_error = "[tool.gardenia] must be a TOML table."

    if has_gardenia_toml:
        try:
            raw_data = tomllib.loads(gardenia_toml.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            return {
                "source": "gardenia.toml",
                "path": str(gardenia_toml),
                "valid": False,
                "profile": RepoProfile(),
                "values": RepoProfile().to_dict(),
                "error": str(exc),
                "duplicate_sources": has_pyproject_section,
            }
        try:
            profile = _parse_profile_table(raw_data, str(gardenia_toml))
        except GardeniaError as exc:
            return {
                "source": "gardenia.toml",
                "path": str(gardenia_toml),
                "valid": False,
                "profile": RepoProfile(),
                "values": RepoProfile().to_dict(),
                "error": exc.message,
                "duplicate_sources": has_pyproject_section,
            }
        return {
            "source": "gardenia.toml",
            "path": str(gardenia_toml),
            "valid": True,
            "profile": profile,
            "values": profile.to_dict(),
            "error": None,
            "duplicate_sources": has_pyproject_section,
        }

    if has_pyproject_section or (pyproject_toml.is_file() and pyproject_parse_error is not None and not has_gardenia_toml):
        if pyproject_parse_error is not None:
            return {
                "source": "pyproject.toml",
                "path": str(pyproject_toml),
                "valid": False,
                "profile": RepoProfile(),
                "values": RepoProfile().to_dict(),
                "error": pyproject_parse_error,
                "duplicate_sources": False,
            }
        assert pyproject_table is not None
        try:
            profile = _parse_profile_table(pyproject_table, f"{pyproject_toml} [tool.gardenia]")
        except GardeniaError as exc:
            return {
                "source": "pyproject.toml",
                "path": str(pyproject_toml),
                "valid": False,
                "profile": RepoProfile(),
                "values": RepoProfile().to_dict(),
                "error": exc.message,
                "duplicate_sources": False,
            }
        return {
            "source": "pyproject.toml",
            "path": str(pyproject_toml),
            "valid": True,
            "profile": profile,
            "values": profile.to_dict(),
            "error": None,
            "duplicate_sources": False,
        }

    default_profile = RepoProfile()
    return {
        "source": "defaults",
        "path": None,
        "valid": True,
        "profile": default_profile,
        "values": default_profile.to_dict(),
        "error": None,
        "duplicate_sources": False,
    }


def merge_profile_overrides(
    base: RepoProfile,
    *,
    gate: str | None = None,
    cpu: int | None = None,
    memory_gb: float | None = None,
    tmpfs_gb: float | None = None,
    no_overflow: bool | None = None,
    routing: str | None = None,
    agcoord: str | None = None,
    docs_exemption: bool | None = None,
    claude_symlink: bool | None = None,
    tools: list[str] | tuple[str, ...] | None = None,
) -> RepoProfile:
    """Overlay explicit CLI flags onto a loaded RepoProfile and validate."""

    merged = RepoProfile(
        gate=base.gate if gate is None else gate,
        cpu=base.cpu if cpu is None else cpu,
        memory_gb=base.memory_gb if memory_gb is None else float(memory_gb),
        tmpfs_gb=base.tmpfs_gb if tmpfs_gb is None else float(tmpfs_gb),
        no_overflow=base.no_overflow if no_overflow is None else no_overflow,
        routing=base.routing if routing is None else routing,
        agcoord=base.agcoord if agcoord is None else agcoord,
        docs_exemption=base.docs_exemption if docs_exemption is None else docs_exemption,
        claude_symlink=base.claude_symlink if claude_symlink is None else claude_symlink,
        tools=base.tools if tools is None else tuple(dict.fromkeys(tools)),
    )
    merged.validate()
    return merged


def render_gardenia_toml(profile: RepoProfile, *, stealth: bool = False) -> str:
    """Serialize a RepoProfile to clean, deterministic ``gardenia.toml``."""

    profile.validate()
    lines = [
        (
            "# Untracked stealth repository profile managed by gardenia."
            if stealth
            else "# Tracked repository profile managed by gardenia."
        ),
        f"gate = {json.dumps(profile.gate)}",
        f"cpu = {profile.cpu}",
        f"memory_gb = {float(profile.memory_gb):g}",
    ]
    if profile.tmpfs_gb is not None:
        lines.append(f"tmpfs_gb = {float(profile.tmpfs_gb):g}")
    lines.extend(
        [
            f"no_overflow = {'true' if profile.no_overflow else 'false'}",
            f"routing = {json.dumps(profile.routing)}",
            f"agcoord = {json.dumps(profile.agcoord)}",
            f"docs_exemption = {'true' if profile.docs_exemption else 'false'}",
            f"claude_symlink = {'true' if profile.claude_symlink else 'false'}",
        ]
    )
    if profile.tools:
        lines.append(f"tools = {json.dumps(list(profile.tools))}")
    lines.append("")
    return "\n".join(lines)


def _format_agc_flags(profile: RepoProfile) -> str:
    parts: list[str] = []
    if profile.no_overflow:
        parts.append("--no-overflow")
    parts.append(f"--resource cpu={profile.cpu}")
    parts.append(f"--resource memory={profile.memory_bytes()}")
    tmpfs = profile.tmpfs_bytes()
    if tmpfs is not None:
        parts.append(f"--resource tmpfs={tmpfs}")
        parts.append(f"--resource tmpfs_inodes={DEFAULT_TMPFS_INODES}")
    return " ".join(parts)


def render_managed_block(profile: RepoProfile) -> str:
    """Render the canonical delimited ``<!-- gardenia:begin --> ... <!-- gardenia:end -->`` block."""

    profile.validate()
    agc_flags = _format_agc_flags(profile)
    bullets: list[str] = [
        (
            "- Use the installed `agrein` skills: `ticket-delivery` before changing code, docs,\n"
            "  config, infrastructure, or live state; `epic-queue` when decomposing multi-ticket\n"
            "  work or coordinating subagent workers; `bug-investigation` before fixing defects or\n"
            "  failing checks; and `owner-escalation` for unsettled product/policy rulings, live\n"
            "  operation approvals, and decision handoffs."
        )
    ]

    if profile.routing == "agroute":
        bullets.append(
            "- Coordinate ticket work by delegating each authorized ticket to a worker selected with\n"
            "  the installed `agroute` and `howhard` skills; an assigned leaf worker implements its\n"
            "  ticket directly without recursively delegating it (report a capability gap first if\n"
            "  `agroute` or worker delegation is unavailable on the host):\n"
            "  1. Inspect or refresh live agent quota state with `agr usage`.\n"
            "  2. Emit eligible provider-qualified candidates for this host with\n"
            "     `agr candidates --host <codex|claude|agy> --output /tmp/TASK-candidates.json --json`.\n"
            "     If `price_request` is non-null, look up official first-party provider pricing (or\n"
            "     classify internal harness models as `\"alias\"` or `\"unlisted\"`), run\n"
            "     `agr prices resolve --file /tmp/TASK-prices.json --json`, and re-emit candidates once.\n"
            "  3. Select a route with `howhard` (`context --host <codex|claude|agy> --auto-decide`, or\n"
            "     `context`/`retrieve`/`decide` evaluating `baseline_request` in an independent stateless\n"
            "     context only when non-null, or `pick --host <codex|claude|agy>`), then resolve the\n"
            "     execution plan with\n"
            "     `agr plan --host <codex|claude|agy> --decision /tmp/TASK-decision.json --json`.\n"
            "  4. Honor `needs_review`. Execute native plans (`target_agent == host_agent`) with the\n"
            "     host's native subagent tool and cross-agent plans (`target_agent != host_agent`) with\n"
            "     the returned non-interactive CLI command.\n"
            "  5. Record the actual outcome and measured usage in `howhard` following the plan's\n"
            "     `feedback` block (`matched_receipt` or `raw_native`), then update `howhard` memory."
        )
    elif profile.routing == "howhard":
        bullets.append(
            "- Coordinate ticket work by delegating each authorized ticket to a native worker\n"
            "  selected with the installed `howhard` skill (`context --host <codex|claude|agy> --auto-decide`,\n"
            "  or `context`/`retrieve`/`decide` evaluating `baseline_request` in an independent stateless\n"
            "  context only when non-null, or `pick --host <codex|claude|agy>`), then record the actual\n"
            "  outcome and measured usage in `howhard` (`observe`, `usage attach`) and update `howhard`\n"
            "  memory; an assigned leaf worker implements its ticket directly without recursively\n"
            "  delegating it."
        )
    else:
        bullets.append(
            "- An assigned leaf worker implements its ticket directly without recursively delegating it."
        )

    bullets.append(
        "- Track every code, test, documentation, or packaging change with a descriptive issue\n"
        "  (`#N — one sentence describing the work`). Do not file an issue solely for\n"
        "  machine-local installation, deployment, configuration, service lifecycle, or other\n"
        "  operator state unless it also requires a repository change or the user requests one."
    )
    bullets.append(
        "- Develop and validate only in an issue-specific Git worktree branched from\n"
        "  `origin/main`. Preserve unrelated work and keep the primary checkout read-only during\n"
        "  development."
    )
    bullets.append(
        "- Verify behavior at public boundaries. Start bug fixes with a failing behavioral test;\n"
        "  accompany runtime changes with focused tests, affected canonical documentation, and a\n"
        "  `CHANGELOG.md` entry when the change is user-facing."
    )

    if profile.docs_exemption:
        bullets.append(
            "- A documentation-only change needs no tests or AGCoord gate. It qualifies only when\n"
            "  every changed path is `README.md`, `CHANGELOG.md`, `AGENTS.md`, Markdown below\n"
            "  `docs/`, or a `.png` or `.svg` image below `docs/assets/` whose provenance is\n"
            "  recorded in `docs/assets/README.md`. Verify the exact pull-request file list and\n"
            "  diff, then merge it through the forge; the issue, worktree, branch, pull-request,\n"
            "  and no-direct-target-push rules still apply."
        )

    if profile.agcoord in ("required", "auto"):
        header = (
            "- Route checks and pull-request landing through the local `agcoord` broker with\n"
            "  integer byte memory claims:\n"
            if profile.agcoord == "required"
            else "- On Linux hosts with `agcoord` (`agc`), route checks and pull-request landing through\n"
            "  the local coordinator with integer byte memory claims:\n"
        )
        trailer = (
            ""
            if profile.agcoord == "required"
            else (
                " On hosts without `agc`, run\n"
                f"  `{profile.gate}` directly and merge through the forge after checks pass."
            )
        )
        bullets.append(
            f"{header}"
            f'  - Check: `agc run --label "<short plain-English summary>" {agc_flags} -- {profile.gate}`\n'
            f'  - Land: `agc land <pr> --label "land #<pr> <short summary>" {agc_flags} -- {profile.gate}`\n'
            "  Give every job a short plain-English `--label` for humans in `agc list` and `agc tui`\n"
            '  (lead with what is checked or landed; omit commit SHAs, branch names, `"pre-land"` tags,\n'
            "  and file paths).\n"
            "  Never submit coordinated work recursively inside an admitted job, and never bypass or\n"
            f"  separate the gate verdict from publication.{trailer}"
        )
    else:
        bullets.append(
            f"- Run `{profile.gate}` to validate every change before opening or merging a pull\n"
            "  request, and never push directly to `main`."
        )

    bullets.append(
        "- Tests must own and stop every process, repository, and temporary directory they\n"
        "  create, and must never inspect or mutate real host state (`~/.codex`, `~/.claude`,\n"
        "  `~/.gemini`, `~/.config`, or `~/.local`)."
    )
    bullets.append(
        "- Never upload a distribution to PyPI or modify live host/production state unless the\n"
        "  owner explicitly authorizes that exact operation. Permission to implement, test,\n"
        "  commit, push, merge, tag, or create a GitHub release does not authorize a PyPI upload."
    )

    body = "\n".join(bullets)
    return f"{MANAGED_BEGIN}\n{body}\n{MANAGED_END}"


def block_markers(name: str) -> tuple[str, str]:
    """Return the ``<!-- <name>:begin -->`` / ``<!-- <name>:end -->`` marker pair."""

    return f"<!-- {name}:begin -->", f"<!-- {name}:end -->"


def _extract_delimited_block(text: str, begin: str, end: str) -> tuple[str | None, bool]:
    begin_count = text.count(begin)
    end_count = text.count(end)
    if begin_count == 0 and end_count == 0:
        return None, False
    if begin_count != 1 or end_count != 1:
        return None, True
    begin_idx = text.index(begin)
    end_idx = text.index(end)
    if end_idx < begin_idx:
        return None, True
    return text[begin_idx : end_idx + len(end)], False


def extract_managed_block(text: str) -> tuple[str | None, bool]:
    """Extract the managed block from ``AGENTS.md``.

    Returns ``(block_text_or_none, malformed)``.
    """

    return _extract_delimited_block(text, MANAGED_BEGIN, MANAGED_END)


def extract_sibling_block(text: str, name: str) -> tuple[str | None, bool]:
    """Extract the ``<!-- <name>:begin --> ... <!-- <name>:end -->`` sibling block.

    Returns ``(block_text_or_none, malformed)``. A sibling block is also malformed when it
    is empty, contains any other block marker, or sits inside the gardenia managed block.
    """

    begin, end = block_markers(name)
    block, malformed = _extract_delimited_block(text, begin, end)
    if block is None:
        return None, malformed
    inner = block[len(begin) : -len(end)]
    if not inner.strip() or _ANY_BLOCK_MARKER_RE.search(inner):
        return None, True
    managed, _ = extract_managed_block(text)
    if managed is not None:
        managed_start = text.index(MANAGED_BEGIN)
        if managed_start < text.index(begin) < managed_start + len(managed):
            return None, True
    return block, False


def normalize_snippet(name: str, text: str) -> str:
    """Return the block body for a plugin ``AGENTS.snippet.md``.

    The snippet may be a bare body or may already be wrapped in its own marker pair.
    """

    begin, end = block_markers(name)
    body = text.replace("\r\n", "\n").lstrip("\ufeff").strip()
    if body.startswith(begin) and body.endswith(end):
        body = body[len(begin) : -len(end)].strip()
    if not body:
        raise GardeniaError(f"{PLUGIN_SNIPPET_FILENAME} for '{name}' is empty.", exit_code=2)
    if _ANY_BLOCK_MARKER_RE.search(body):
        raise GardeniaError(
            f"{PLUGIN_SNIPPET_FILENAME} for '{name}' contains stray block markers; "
            f"it may only be wrapped once in '{begin}' / '{end}'.",
            exit_code=2,
        )
    return body


def render_sibling_block(name: str, body: str) -> str:
    """Wrap a normalized snippet body in its sibling marker pair."""

    begin, end = block_markers(name)
    return f"{begin}\n{body}\n{end}"


def _append_block(existing: str, block: str) -> str:
    if not existing:
        return f"# Repository agent instructions\n\n{block}\n"
    sep = "\n\n" if not existing.endswith("\n") else ("\n" if not existing.endswith("\n\n") else "")
    return f"{existing}{sep}{block}\n"


def _malformed_sibling_error(name: str) -> GardeniaError:
    begin, end = block_markers(name)
    return GardeniaError(
        f"Malformed {name} markers in AGENTS.md: ensure exactly one '{begin}' precedes one '{end}', "
        "with non-empty content that does not overlap any other managed block.",
        exit_code=2,
    )


def apply_sibling_block(existing: str, name: str, body: str) -> str:
    """Idempotently insert or replace ONLY the ``<name>`` sibling block."""

    extracted, malformed = extract_sibling_block(existing, name)
    if malformed:
        raise _malformed_sibling_error(name)
    block = render_sibling_block(name, body)
    if extracted is None:
        return _append_block(existing, block)
    begin_idx = existing.index(extracted)
    return existing[:begin_idx] + block + existing[begin_idx + len(extracted) :]


def apply_managed_block(existing: str | None, block: str) -> str:
    """Insert or replace ONLY the managed block while preserving all surrounding lines."""

    if existing is None:
        return f"# Repository agent instructions\n\n{block}\n"

    extracted, malformed = extract_managed_block(existing)
    if malformed:
        raise GardeniaError(
            f"Malformed gardenia markers in AGENTS.md: ensure exactly one '{MANAGED_BEGIN}' "
            f"precedes one '{MANAGED_END}'.",
            exit_code=2,
        )
    if extracted is not None:
        begin_idx = existing.index(MANAGED_BEGIN)
        end_idx = existing.index(MANAGED_END) + len(MANAGED_END)
        return existing[:begin_idx] + block + existing[end_idx:]

    return _append_block(existing, block)


def _inspect_optional_toml(path: Path, component: str, findings: list[Finding]) -> dict[str, Any]:
    if not path.is_file():
        return {"path": str(path), "exists": False, "valid": None, "error": None}
    try:
        tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        findings.append(
            Finding(
                scope="repo",
                severity="error",
                component=component,
                message=f"Tracked repository config {path.name} has invalid TOML: {exc}",
                remediation=f"Fix TOML syntax in {path}",
            )
        )
        return {"path": str(path), "exists": True, "valid": False, "error": str(exc)}
    return {"path": str(path), "exists": True, "valid": True, "error": None}


def _inspect_claude_md(repo_root: Path, profile: RepoProfile, findings: list[Finding]) -> dict[str, Any]:
    claude_path = repo_root / "CLAUDE.md"
    if claude_path.is_symlink():
        target = os.readlink(claude_path)
        if target == "AGENTS.md":
            return {"path": str(claude_path), "status": "symlink_to_agents", "target": target}
        if profile.claude_symlink:
            findings.append(
                Finding(
                    scope="repo",
                    severity="warning",
                    component="claude-md",
                    message=f"CLAUDE.md is a symlink to '{target}' instead of 'AGENTS.md'.",
                    remediation="gardenia repo init --force-claude-symlink",
                )
            )
        return {"path": str(claude_path), "status": "symlink_other", "target": target}

    if claude_path.exists():
        if profile.claude_symlink:
            findings.append(
                Finding(
                    scope="repo",
                    severity="warning",
                    component="claude-md",
                    message="CLAUDE.md is a regular file rather than a symlink to AGENTS.md.",
                    remediation="gardenia repo init --force-claude-symlink  # or set claude_symlink = false in gardenia.toml",
                )
            )
        return {"path": str(claude_path), "status": "regular_file", "target": None}

    if profile.claude_symlink:
        findings.append(
            Finding(
                scope="repo",
                severity="warning",
                component="claude-md",
                message="CLAUDE.md symlink to AGENTS.md is missing.",
                remediation="gardenia repo init",
            )
        )
    return {"path": str(claude_path), "status": "missing", "target": None}


def _inspect_howhard_local_store(git_common_dir: Path, findings: list[Finding]) -> dict[str, Any]:
    store_dir = git_common_dir / "howhard"
    exists = store_dir.is_dir()
    store_json = (store_dir / "store.json").is_file()
    payloads_sqlite = (store_dir / "payloads.sqlite3").is_file()

    requests_dir = store_dir / "requests"
    decisions_dir = store_dir / "decisions"
    legacy_requests = len(list(requests_dir.glob("*.json"))) if requests_dir.is_dir() else 0
    legacy_decisions = len(list(decisions_dir.glob("*.json"))) if decisions_dir.is_dir() else 0
    has_legacy = (legacy_requests + legacy_decisions) > 0

    if has_legacy:
        findings.append(
            Finding(
                scope="repo",
                severity="warning",
                component="howhard-store",
                message=(
                    f"Pre-4.0 loose howhard JSON files detected under {store_dir} "
                    f"(requests/*.json: {legacy_requests}, decisions/*.json: {legacy_decisions}). "
                    "gardenia never deletes or mutates these files automatically."
                ),
                remediation="Run howhard's migrate-payloads.py migration script against this repository's howhard store.",
            )
        )

    return {
        "path": str(store_dir),
        "exists": exists,
        "store_json": store_json,
        "payloads_sqlite": payloads_sqlite,
        "legacy_requests_json_count": legacy_requests,
        "legacy_decisions_json_count": legacy_decisions,
        "has_legacy_loose_json": has_legacy,
    }


def _inspect_agroute_local_store(
    repo_root: Path,
    git_common_dir: Path,
    env: Mapping[str, str],
    findings: list[Finding],
) -> dict[str, Any]:
    store_dir = git_common_dir / "agroute"
    exists = store_dir.is_dir()

    agr_path = shutil.which("agr", path=env.get("PATH"))
    if agr_path is None:
        return {
            "path": str(store_dir),
            "exists": exists,
            "prices_pending_checked": False,
            "pending_price_count": None,
            "pending_models": [],
            "prices_pending_error": "agr-not-on-path",
        }

    result = run_cmd([agr_path, "prices", "pending", "--json"], cwd=repo_root, timeout=DEFAULT_TIMEOUT_SECONDS, env=env)
    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        return {
            "path": str(store_dir),
            "exists": exists,
            "prices_pending_checked": False,
            "pending_price_count": None,
            "pending_models": [],
            "prices_pending_error": detail,
        }

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {
            "path": str(store_dir),
            "exists": exists,
            "prices_pending_checked": False,
            "pending_price_count": None,
            "pending_models": [],
            "prices_pending_error": f"invalid JSON: {exc}",
        }

    pending_count = payload.get("pending_count")
    if not isinstance(pending_count, int):
        pending_count = 0
    pending_models: list[str] = []
    price_request = payload.get("price_request")
    if isinstance(price_request, dict):
        raw_models = price_request.get("models") or price_request.get("unknown_models") or []
        if isinstance(raw_models, list):
            for item in raw_models:
                if isinstance(item, str):
                    pending_models.append(item)
                elif isinstance(item, dict):
                    key = item.get("catalog_key") or item.get("model") or item.get("slug")
                    if key:
                        pending_models.append(str(key))

    if pending_count > 0:
        model_note = f" ({', '.join(pending_models)})" if pending_models else ""
        findings.append(
            Finding(
                scope="repo",
                severity="warning",
                component="agroute-prices",
                message=f"{pending_count} discovered model(s) have pending unresolved prices in local agroute state{model_note}.",
                remediation="Run 'agr prices pending --json', look up official provider pricing, and run 'agr prices resolve --file <path> --json'.",
            )
        )

    return {
        "path": str(store_dir),
        "exists": exists,
        "prices_pending_checked": True,
        "pending_price_count": pending_count,
        "pending_models": pending_models,
        "prices_pending_error": None,
    }


def _check_tracked_agents_md(
    profile: RepoProfile,
    *,
    agents_exists: bool,
    extracted_block: str | None,
    block_malformed: bool,
    findings: list[Finding],
) -> bool | None:
    """Validate the gardenia managed block and return its sync state."""

    if not agents_exists:
        findings.append(
            Finding(
                scope="repo",
                severity="error",
                component="agents-md",
                message="Tracked AGENTS.md file is missing from the repository root.",
                remediation="gardenia repo init",
            )
        )
        return None
    if block_malformed:
        findings.append(
            Finding(
                scope="repo",
                severity="error",
                component="agents-md",
                message=f"AGENTS.md has malformed '{MANAGED_BEGIN}' / '{MANAGED_END}' markers.",
                remediation="Repair the marker pair in AGENTS.md, then run: gardenia repo init",
            )
        )
        return False
    if extracted_block is None:
        findings.append(
            Finding(
                scope="repo",
                severity="warning",
                component="agents-md",
                message="AGENTS.md exists but does not contain a gardenia managed block.",
                remediation="gardenia repo init",
            )
        )
        return False
    if extracted_block != render_managed_block(profile):
        findings.append(
            Finding(
                scope="repo",
                severity="warning",
                component="agents-md",
                message="AGENTS.md managed block has drifted from the repository gardenia profile.",
                remediation="gardenia repo init",
            )
        )
        return False
    return True


def _check_sibling_blocks(sibling_blocks: Mapping[str, Mapping[str, bool]], findings: list[Finding]) -> None:
    """Require one well-formed sibling block per declared tool, using repository files only."""

    for name, state in sibling_blocks.items():
        begin, end = block_markers(name)
        if state["malformed"]:
            message = f"AGENTS.md has malformed '{begin}' / '{end}' markers."
            remediation = "Repair the marker pair in AGENTS.md, then run: gardenia repo init"
        elif not state["present"]:
            message = f"AGENTS.md is missing the '{begin}' / '{end}' block for declared tool '{name}'."
            remediation = "gardenia repo init  # on a host with the plugin installed"
        else:
            continue
        findings.append(
            Finding(
                scope="repo",
                severity="error",
                component=f"agents-md:{name}",
                message=message,
                remediation=remediation,
            )
        )


def _check_stealth_markers(agents_text: str, profile: RepoProfile, findings: list[Finding]) -> None:
    """Warn when a stealth repository's AGENTS.md carries markers that must stay uncommitted."""

    present = [
        name
        for name in ("gardenia", *profile.tools)
        if any(marker in agents_text for marker in block_markers(name))
    ]
    if present:
        findings.append(
            Finding(
                scope="repo",
                severity="warning",
                component="agents-md",
                message=(
                    "A stealth profile is active, but AGENTS.md contains managed markers that must not be "
                    f"committed: {', '.join(present)}."
                ),
                remediation="Remove those marker blocks from AGENTS.md",
            )
        )


def check_repo(
    repo_path: str | os.PathLike[str] | None = None,
    *,
    strict: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Perform read-only inspection of tracked repository settings and local ``.git/`` state."""

    effective_env = _effective_env(env)
    git_info = resolve_git_repo(repo_path, env=effective_env)
    findings: list[Finding] = []

    if git_info.is_linked_worktree:
        findings.append(
            Finding(
                scope="repo",
                severity="info",
                component="worktree",
                message=(
                    f"Target checkout {git_info.root} is a linked Git worktree sharing common Git state "
                    f"at {git_info.git_common_dir}."
                ),
                remediation="Run 'gardenia repo init' in the primary checkout (or pass --allow-worktree explicitly).",
            )
        )

    cfg = load_repo_config(git_info.root, git_info.git_common_dir)
    profile: RepoProfile = cfg["profile"]
    stealth: bool = cfg["stealth"]
    if cfg["stealth_ignored"]:
        findings.append(
            Finding(
                scope="repo",
                severity="warning",
                component="gardenia-config",
                message=(
                    f"Untracked stealth profile {stealth_profile_path(git_info.git_common_dir)} is ignored "
                    "because the repository has a tracked gardenia profile."
                ),
                remediation=f"Remove {stealth_profile_path(git_info.git_common_dir)}",
            )
        )
    if not cfg["valid"]:
        findings.append(
            Finding(
                scope="repo",
                severity="error",
                component="gardenia-config",
                message=f"Invalid repository gardenia configuration ({cfg['path']}): {cfg['error']}",
                remediation=f"Fix configuration errors in {cfg['path']}",
            )
        )
    if cfg.get("duplicate_sources"):
        findings.append(
            Finding(
                scope="repo",
                severity="warning",
                component="gardenia-config",
                message="Both gardenia.toml and [tool.gardenia] in pyproject.toml exist; gardenia.toml takes precedence.",
                remediation="Keep repository profile settings in a single file (gardenia.toml recommended).",
            )
        )

    agents_path = git_info.root / "AGENTS.md"
    agents_exists = agents_path.is_file()
    agents_text = agents_path.read_text(encoding="utf-8") if agents_exists else ""
    extracted_block, block_malformed = extract_managed_block(agents_text) if agents_exists else (None, False)
    in_sync: bool | None = None
    sibling_blocks: dict[str, dict[str, bool]] = {}
    for name in profile.tools:
        sibling, sibling_malformed = extract_sibling_block(agents_text, name)
        sibling_blocks[name] = {"present": sibling is not None, "malformed": sibling_malformed}

    if stealth:
        _check_stealth_markers(agents_text, profile, findings)
    else:
        in_sync = _check_tracked_agents_md(
            profile,
            agents_exists=agents_exists,
            extracted_block=extracted_block,
            block_malformed=block_malformed,
            findings=findings,
        )
        if agents_exists:
            _check_sibling_blocks(sibling_blocks, findings)

    lower_agents = agents_text.lower()
    layers_referenced = {
        "agrein": "agrein" in lower_agents,
        "agroute": ("agroute" in lower_agents) or ("agr " in lower_agents),
        "howhard": "howhard" in lower_agents,
        "agcoord": ("agcoord" in lower_agents) or ("agc " in lower_agents),
    }

    claude_md = _inspect_claude_md(
        git_info.root,
        replace(profile, claude_symlink=False) if stealth else profile,
        findings,
    )
    howhard_toml = _inspect_optional_toml(git_info.root / "howhard.toml", "howhard-toml", findings)
    agroute_toml = _inspect_optional_toml(git_info.root / "agroute.toml", "agroute-toml", findings)

    howhard_store = _inspect_howhard_local_store(git_info.git_common_dir, findings)
    agroute_store = _inspect_agroute_local_store(git_info.root, git_info.git_common_dir, effective_env, findings)

    ok = compute_ok(findings, strict=strict)
    repo_data = {
        "tracked": {
            "root": str(git_info.root),
            "is_linked_worktree": git_info.is_linked_worktree,
            "stealth": stealth,
            "sibling_blocks": sibling_blocks,
            "config": {
                "source": cfg["source"],
                "path": cfg["path"],
                "valid": cfg["valid"],
                "values": cfg["values"],
                "error": cfg["error"],
                "duplicate_sources": cfg["duplicate_sources"],
            },
            "agents_md": {
                "path": str(agents_path),
                "exists": agents_exists,
                "has_managed_block": extracted_block is not None,
                "block_malformed": block_malformed,
                "in_sync": in_sync,
                "layers_referenced": layers_referenced,
            },
            "claude_md": claude_md,
            "howhard_toml": howhard_toml,
            "agroute_toml": agroute_toml,
        },
        "local_state": {
            "git_dir": str(git_info.git_dir),
            "git_common_dir": str(git_info.git_common_dir),
            "howhard_store": howhard_store,
            "agroute_store": agroute_store,
        },
    }

    return {
        "format": CHECK_FORMAT,
        "version": __version__,
        "scope": "repo",
        "ok": ok,
        "strict": strict,
        "repo": repo_data,
        "findings": [f.to_dict() for f in findings],
        "_findings": findings,
    }


def render_repo(
    *,
    repo_path: str | os.PathLike[str] | None = None,
    gate: str | None = None,
    cpu: int | None = None,
    memory_gb: float | None = None,
    tmpfs_gb: float | None = None,
    no_overflow: bool | None = None,
    routing: str | None = None,
    agcoord: str | None = None,
    docs_exemption: bool | None = None,
    claude_symlink: bool | None = None,
    tools: list[str] | tuple[str, ...] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Render the managed ``AGENTS.md`` block using optional repo config + CLI overrides."""

    effective_env = _effective_env(env)
    base_profile = RepoProfile()
    config_source = "defaults"
    if repo_path is not None:
        git_info = resolve_git_repo(repo_path, env=effective_env)
        cfg = load_repo_config(git_info.root, git_info.git_common_dir)
        if not cfg["valid"]:
            raise GardeniaError(f"Invalid repository config ({cfg['path']}): {cfg['error']}", exit_code=2)
        base_profile = cfg["profile"]
        config_source = cfg["source"]
    else:
        git_info = try_resolve_git_repo(None, env=effective_env)
        if git_info is not None:
            cfg = load_repo_config(git_info.root, git_info.git_common_dir)
            if not cfg["valid"]:
                raise GardeniaError(f"Invalid repository config ({cfg['path']}): {cfg['error']}", exit_code=2)
            base_profile = cfg["profile"]
            config_source = cfg["source"]

    profile = merge_profile_overrides(
        base_profile,
        gate=gate,
        cpu=cpu,
        memory_gb=memory_gb,
        tmpfs_gb=tmpfs_gb,
        no_overflow=no_overflow,
        routing=routing,
        agcoord=agcoord,
        docs_exemption=docs_exemption,
        claude_symlink=claude_symlink,
        tools=tools,
    )
    block = render_managed_block(profile)
    return {
        "scope": "repo",
        "config_source": config_source,
        "profile": profile.to_dict(),
        "managed_block": block,
    }


def _plan_agents_md_action(actions: list[PlannedAction], *, changed: bool, created: bool, path: Path) -> None:
    if not changed:
        actions.append(
            PlannedAction(
                kind="skip",
                component="agents-md",
                description="AGENTS.md managed block is already up to date.",
                path=str(path),
                status="skipped",
            )
        )
        return
    desc = "Create AGENTS.md with gardenia managed block." if created else "Update gardenia managed block in AGENTS.md."
    actions.append(PlannedAction(kind="write_file", component="agents-md", description=desc, path=str(path)))


def _plan_sibling_blocks(
    actions: list[PlannedAction],
    warnings: list[str],
    text: str,
    tools: tuple[str, ...],
    *,
    path: Path,
    env: Mapping[str, str],
) -> tuple[str, list[str]]:
    """Stamp sibling blocks from resolved plugin snippets, preserving unresolved ones."""

    # Host state is consulted only when the profile declares tools.
    from gardenia.host import resolve_tool_snippets

    for name in tools:
        if extract_sibling_block(text, name)[1]:
            raise _malformed_sibling_error(name)

    resolutions = resolve_tool_snippets(tools, env=env)
    unresolved: list[str] = []
    for name in tools:
        resolution = resolutions[name]
        component = f"agents-md:{name}"
        if resolution.body is None:
            unresolved.append(name)
            message = (
                f"Could not resolve {PLUGIN_SNIPPET_FILENAME} for '{name}' ({resolution.error}); "
                "its AGENTS.md block was left unchanged."
            )
            warnings.append(message)
            actions.append(
                PlannedAction(kind="skip", component=component, description=message, path=str(path), status="skipped")
            )
            continue
        updated = apply_sibling_block(text, name, resolution.body)
        if updated == text:
            actions.append(
                PlannedAction(
                    kind="skip",
                    component=component,
                    description=f"AGENTS.md '{name}' block is already up to date.",
                    path=str(path),
                    status="skipped",
                )
            )
        else:
            actions.append(
                PlannedAction(
                    kind="write_file",
                    component=component,
                    description=f"Update AGENTS.md '{name}' block from {resolution.path}.",
                    path=str(path),
                )
            )
        text = updated
    return text, unresolved


def _plan_claude_symlink(
    actions: list[PlannedAction],
    warnings: list[str],
    claude_path: Path,
    *,
    enabled: bool,
    force: bool,
) -> tuple[bool, bool]:
    """Plan the ``CLAUDE.md -> AGENTS.md`` step and return ``(create, replace_existing)``."""

    def _skip(description: str) -> tuple[bool, bool]:
        actions.append(
            PlannedAction(
                kind="skip",
                component="claude-md",
                description=description,
                path=str(claude_path),
                status="skipped",
            )
        )
        return False, False

    def _link(description: str, *, replace_existing: bool) -> tuple[bool, bool]:
        actions.append(
            PlannedAction(kind="symlink", component="claude-md", description=description, path=str(claude_path))
        )
        return True, replace_existing

    if not enabled:
        return _skip("CLAUDE.md symlink management disabled by profile.")
    if claude_path.is_symlink() and os.readlink(claude_path) == "AGENTS.md":
        return _skip("CLAUDE.md symlink to AGENTS.md is already in place.")
    if not claude_path.exists() and not claude_path.is_symlink():
        return _link("Create symlink CLAUDE.md -> AGENTS.md.", replace_existing=False)
    if force:
        return _link("Replace existing CLAUDE.md with symlink CLAUDE.md -> AGENTS.md.", replace_existing=True)
    warning_msg = (
        "Skipping CLAUDE.md symlink because a non-matching file or symlink already exists "
        "(pass --force-claude-symlink to replace it)."
    )
    warnings.append(warning_msg)
    return _skip(warning_msg)


def init_repo(
    *,
    repo_path: str | os.PathLike[str] | None = None,
    gate: str | None = None,
    cpu: int | None = None,
    memory_gb: float | None = None,
    tmpfs_gb: float | None = None,
    no_overflow: bool | None = None,
    routing: str | None = None,
    agcoord: str | None = None,
    docs_exemption: bool | None = None,
    claude_symlink: bool | None = None,
    tools: list[str] | tuple[str, ...] | None = None,
    stealth: bool = False,
    force_claude_symlink: bool = False,
    save_config: bool = False,
    allow_worktree: bool = False,
    register: bool | None = None,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Initialize or update tracked repository files (``AGENTS.md``, ``CLAUDE.md``, ``gardenia.toml``).

    In stealth mode (``stealth=True`` or an existing stealth profile) only the untracked
    profile under the Git common directory is written; tracked files are never touched.
    """

    effective_env = _effective_env(env)
    git_info = resolve_git_repo(repo_path, env=effective_env)
    cfg = load_repo_config(git_info.root, git_info.git_common_dir)
    stealth_active = stealth or cfg["stealth"]
    if git_info.is_linked_worktree and not allow_worktree and not stealth_active:
        raise GardeniaError(
            f"Refusing to run 'repo init' inside linked Git worktree {git_info.root}. "
            "Run in the primary checkout or pass --allow-worktree explicitly.",
            exit_code=2,
        )
    if git_info.is_linked_worktree and register is True:
        raise GardeniaError(
            f"Refusing to register linked Git worktree {git_info.root} in the host repository registry. "
            "Register the primary checkout instead or omit --register.",
            exit_code=2,
        )

    if not cfg["valid"]:
        raise GardeniaError(f"Invalid repository config ({cfg['path']}): {cfg['error']}", exit_code=2)
    stealth_path = stealth_profile_path(git_info.git_common_dir)
    if stealth_active:
        if not cfg["stealth"] and cfg["source"] != "defaults":
            raise GardeniaError(
                f"Refusing --stealth: the repository already has a tracked gardenia profile ({cfg['path']}).",
                exit_code=2,
            )
        if save_config or force_claude_symlink:
            raise GardeniaError(
                "--save-config and --force-claude-symlink write tracked files and cannot be used with a "
                f"stealth profile. To switch to a tracked profile, first remove {stealth_path}.",
                exit_code=2,
            )

    profile = merge_profile_overrides(
        cfg["profile"],
        gate=gate,
        cpu=cpu,
        memory_gb=memory_gb,
        tmpfs_gb=tmpfs_gb,
        no_overflow=no_overflow,
        routing=routing,
        agcoord=agcoord,
        docs_exemption=docs_exemption,
        claude_symlink=claude_symlink,
        tools=tools,
    )

    block = render_managed_block(profile)
    agents_path = git_info.root / "AGENTS.md"
    claude_path = git_info.root / "CLAUDE.md"

    actions: list[PlannedAction] = []
    warnings: list[str] = []
    unresolved_tools: list[str] = []
    updated_agents = ""
    create_symlink = False
    replace_symlink = False
    gardenia_toml_path = git_info.root / "gardenia.toml"
    rendered_toml: str | None = None

    if stealth_active:
        rendered_toml = render_gardenia_toml(profile, stealth=True)
        if stealth_path.is_file() and profile == cfg["profile"]:
            actions.append(
                PlannedAction(
                    kind="skip",
                    component="stealth-profile",
                    description="Stealth profile is already up to date; tracked files are never modified.",
                    path=str(stealth_path),
                    status="skipped",
                )
            )
        else:
            actions.append(
                PlannedAction(
                    kind="write_file",
                    component="stealth-profile",
                    description="Write untracked stealth profile; tracked files are never modified.",
                    path=str(stealth_path),
                )
            )
    else:
        existing_agents = agents_path.read_text(encoding="utf-8") if agents_path.is_file() else None
        updated_agents = apply_managed_block(existing_agents, block)
        # 1. AGENTS.md managed block, then one sibling block per declared tool.
        _plan_agents_md_action(
            actions,
            changed=existing_agents != updated_agents,
            created=existing_agents is None,
            path=agents_path,
        )
        if profile.tools:
            updated_agents, unresolved_tools = _plan_sibling_blocks(
                actions,
                warnings,
                updated_agents,
                profile.tools,
                path=agents_path,
                env=effective_env,
            )

        # 2. CLAUDE.md -> AGENTS.md symlink.
        create_symlink, replace_symlink = _plan_claude_symlink(
            actions,
            warnings,
            claude_path,
            enabled=profile.claude_symlink,
            force=force_claude_symlink,
        )

        # 3. Optional gardenia.toml persistence.
        if save_config:
            rendered_toml = render_gardenia_toml(profile)
            existing_toml = gardenia_toml_path.read_text(encoding="utf-8") if gardenia_toml_path.is_file() else None
            if existing_toml == rendered_toml:
                actions.append(
                    PlannedAction(
                        kind="skip",
                        component="gardenia-config",
                        description="gardenia.toml is already up to date.",
                        path=str(gardenia_toml_path),
                        status="skipped",
                    )
                )
            else:
                actions.append(
                    PlannedAction(
                        kind="write_file",
                        component="gardenia-config",
                        description="Write tracked repository profile to gardenia.toml.",
                        path=str(gardenia_toml_path),
                    )
                )

    # 4. Optional host repository registry update.
    from gardenia.repos import effective_repos_config_path, load_repos_registry, save_repos_registry

    registry_path = effective_repos_config_path(effective_env)
    updated_registry_repos: list[str] | None = None
    should_register = (register is True) or (register is None and not git_info.is_linked_worktree)
    if register is False:
        actions.append(
            PlannedAction(
                kind="skip",
                component="repos-registry",
                description="Skipping host repository registry update (--no-register).",
                path=str(registry_path),
                status="skipped",
            )
        )
    elif git_info.is_linked_worktree:
        actions.append(
            PlannedAction(
                kind="skip",
                component="repos-registry",
                description="Skipping host repository registry update inside a linked Git worktree.",
                path=str(registry_path),
                status="skipped",
            )
        )
    elif should_register:
        reg = load_repos_registry(effective_env)
        root_str = str(git_info.root)
        if root_str in reg["repos"]:
            actions.append(
                PlannedAction(
                    kind="skip",
                    component="repos-registry",
                    description="Repository is already registered in the host repository registry.",
                    path=str(registry_path),
                    status="skipped",
                )
            )
        else:
            updated_registry_repos = sorted(set(reg["repos"]) | {root_str})
            actions.append(
                PlannedAction(
                    kind="write_file",
                    component="repos-registry",
                    description=f"Register {root_str} in the host repository registry.",
                    path=str(registry_path),
                )
            )

    if not dry_run:
        for action in actions:
            if action.kind == "skip":
                continue
            if action.component.split(":", 1)[0] == "agents-md" and action.kind == "write_file":
                agents_path.write_text(updated_agents, encoding="utf-8")
                action.status = "executed"
            elif action.component == "stealth-profile" and rendered_toml is not None:
                stealth_path.parent.mkdir(parents=True, exist_ok=True)
                stealth_path.write_text(rendered_toml, encoding="utf-8")
                action.status = "executed"
            elif action.component == "claude-md" and action.kind == "symlink" and create_symlink:
                if replace_symlink and (claude_path.exists() or claude_path.is_symlink()):
                    claude_path.unlink()
                claude_path.symlink_to("AGENTS.md")
                action.status = "executed"
            elif action.component == "gardenia-config" and action.kind == "write_file" and rendered_toml is not None:
                gardenia_toml_path.write_text(rendered_toml, encoding="utf-8")
                action.status = "executed"
            elif (
                action.component == "repos-registry"
                and action.kind == "write_file"
                and updated_registry_repos is not None
            ):
                save_repos_registry(updated_registry_repos, env=effective_env)
                action.status = "executed"

    return {
        "scope": "repo",
        "ok": True,
        "dry_run": dry_run,
        "root": str(git_info.root),
        "is_linked_worktree": git_info.is_linked_worktree,
        "profile": profile.to_dict(),
        "managed_block": block,
        "stealth": stealth_active,
        "unresolved_tools": unresolved_tools,
        "warnings": warnings,
        "actions": [action.to_dict() for action in actions],
    }
